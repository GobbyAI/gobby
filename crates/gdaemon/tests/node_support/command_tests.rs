//! #24376: the dispatcher owns validation, run ordering and async preparation.
use super::*;
use gobby_daemon::{
    node_commands::{CONTRACT_VERSION, SandboxBackend},
    nodes::{
        agents::commands::dispatch,
        channel::{ChannelTiming, serve_channel},
    },
};
use tokio_tungstenite::{WebSocketStream, tungstenite::protocol::Role};

/// Real-time hang guard for the preparation barrier. The chain before the barrier runs git,
/// uv and sandbox compiles as child processes, so its wall time scales with host load:
/// about 6 s idle and 27 s with 36 extra CPU-bound processes on the test host. The guard
/// only has to outlast that; the 300 s preparation deadline itself runs on a virtual clock.
const WAIT: Duration = Duration::from_secs(120);

struct CommandsFixture {
    launch: LaunchFixture,
    ready: PathBuf,
    release: PathBuf,
}

impl CommandsFixture {
    async fn new(block_preparation: bool) -> Result<Self> {
        Self::build(block_preparation, false).await
    }

    /// Holds the sandbox policy compile, not the workspace seed, until the test releases it.
    async fn blocking_policy() -> Result<Self> {
        Self::build(false, true).await
    }

    async fn build(block_preparation: bool, block_policy: bool) -> Result<Self> {
        let mut launch = LaunchFixture::build(block_policy).await?;
        let source = source_repo(launch._root.path())?;
        // A real, tiny repository exercises workspace creation/removal. Only the
        // uv seed is scripted; it never invokes Python or the user's daemon.
        fs::write(
            source.join("pyproject.toml"),
            "[project]\nname='fixture'\nversion='0'\n",
        )?;
        fs::write(source.join("uv.lock"), "version = 1\n")?;
        git(&source, &["add", "pyproject.toml", "uv.lock"])?;
        git(&source, &["commit", "-q", "-m", "seed inputs"])?;
        launch.payload.source_root = source.display().to_string();
        launch.payload.project_id = PROJECT_ID.into();
        launch.payload.workspace = worktree_spawn("agent/commands");
        let ready = launch._root.path().join("seed-entered");
        let release = launch._root.path().join("seed-release");
        if !block_preparation {
            fs::write(&release, "release")?;
        }
        let cache = launch._root.path().join("seed-cache");
        fs::create_dir_all(&cache)?;
        fake_uv(
            &launch.tree.path("bin"),
            &cache,
            &format!(
                ": > '{}'\nwhile [ ! -f '{}' ]; do /bin/sleep 0.01; done\nexit 0\n",
                ready.display(),
                release.display()
            ),
        )?;
        {
            let mut state = launch.agents.state.lock().await;
            state.runs.clear();
            state.commit(&launch.agents.home, &launch.agents.changed)?;
        }
        Ok(Self {
            launch,
            ready,
            release,
        })
    }

    fn spawn(&self, request: &str) -> Value {
        command(request, "agent.spawn", json!(self.launch.payload))
    }

    fn cancel(&self, request: &str, run_id: &str, signal: &str) -> Value {
        command(
            request,
            "agent.cancel",
            json!({"run_id":run_id,"signal":signal}),
        )
    }

    async fn call(&self, frame: Value) -> Value {
        tokio::time::timeout(
            WAIT,
            dispatch(
                self.launch.agents.clone(),
                Arc::new(self.launch.context.clone()),
                frame,
            ),
        )
        .await
        .expect("dispatcher must answer without waiting for preparation")
    }

    async fn seed_entered(&self) {
        tokio::time::timeout(WAIT, async {
            while !self.ready.is_file() {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .expect("preparation must reach the isolated uv barrier");
    }

    fn release(&self) -> Result<()> {
        fs::write(&self.release, "release")?;
        Ok(())
    }

    /// Runs the dispatcher on a task so a test can observe it while it waits.
    fn spawn_dispatch(&self, frame: Value) -> AbortTask<Value> {
        AbortTask(tokio::spawn(dispatch(
            self.launch.agents.clone(),
            Arc::new(self.launch.context.clone()),
            frame,
        )))
    }

    async fn policy_entered(&self) {
        let (entered, _) = self
            .launch
            .policy
            .as_ref()
            .expect("blocking policy fixture");
        tokio::time::timeout(WAIT, async {
            while !entered.is_file() {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .expect("policy compile must reach its barrier");
    }

    fn release_policy(&self) -> Result<()> {
        let (_, release) = self
            .launch
            .policy
            .as_ref()
            .context("blocking policy fixture")?;
        fs::write(release, "release")?;
        Ok(())
    }

    async fn wait_state(&self, predicate: impl Fn(&AgentState) -> bool) {
        tokio::time::timeout(WAIT, async {
            loop {
                let changed = self.launch.agents.changed.notified();
                tokio::pin!(changed);
                changed.as_mut().enable();
                if predicate(&*self.launch.agents.state.lock().await) {
                    break;
                }
                changed.await;
            }
        })
        .await
        .expect("dispatcher must settle durable state");
    }

    async fn exit(&self, reason: ExitReason, launched: bool) -> Result<ReportExit> {
        let id = &self.launch.payload.run_id;
        self.wait_state(|state| state.exits.iter().any(|entry| &entry.exit.run_id == id))
            .await;
        let state = persisted_state(&self.launch.agents.home)?;
        let exit = state
            .exits
            .iter()
            .find(|entry| &entry.exit.run_id == id)
            .context("durable exit")?
            .exit
            .clone();
        assert_eq!(exit.reason, reason);
        assert_eq!(exit.launched, launched);
        // A launched exit keeps its record as Exited until a report carrying it gets 200.
        let retained = state.runs.get(id).map(|run| run.phase);
        assert_eq!(retained, launched.then_some(RunPhase::Exited));
        Ok(exit)
    }

    async fn known(&self, run_id: &str, terminal: &str) -> Result<()> {
        let mut state = self.launch.agents.state.lock().await;
        let mut record = run(RunPhase::Launched, None);
        record.terminal_id = terminal.into();
        record.host_terminal_id = Some(format!("host-{terminal}"));
        state.runs.insert(run_id.into(), record);
        state.commit(&self.launch.agents.home, &self.launch.agents.changed)?;
        Ok(())
    }
}

fn command(request: &str, name: &str, payload: Value) -> Value {
    json!({"type":"command","v":CONTRACT_VERSION,"request_id":request,
        "command":name,"payload":payload})
}

fn ok(reply: &Value, request: &str, result: Value) {
    assert_eq!(reply["type"], "reply");
    assert_eq!(reply["request_id"], request);
    assert_eq!(reply["v"], CONTRACT_VERSION);
    assert_eq!(reply["ok"], true, "{reply}");
    assert_eq!(reply["result"], result);
}

fn error(reply: &Value, request: &str, code: &str, stage: &str) {
    assert_eq!(reply["type"], "reply");
    assert_eq!(reply["request_id"], request);
    assert_eq!(reply["ok"], false, "{reply}");
    assert_eq!(reply["error"]["code"], code);
    assert_eq!(reply["error"]["stage"], stage);
}

fn write(request: &str, terminal: &str, data: &str) -> Value {
    command(
        request,
        "terminal.write",
        json!({"terminal_id":terminal,"kind":"text","data":data,"submit":true}),
    )
}

/// The host takes write data as base64 under `encoding` `utf8-b64`; decodes it to the text.
fn written_text(request: &Value) -> String {
    assert_eq!(request["encoding"], "utf8-b64");
    let data = request["data"].as_str().unwrap_or_default();
    let bytes = base64::Engine::decode(&base64::engine::general_purpose::STANDARD, data)
        .expect("write data is base64");
    String::from_utf8(bytes).expect("write data is utf-8")
}

/// Serves the node channel over an in-memory socket of `capacity` bytes; returns the peer end.
async fn serve_duplex(
    fixture: &CommandsFixture,
    capacity: usize,
) -> WebSocketStream<tokio::io::DuplexStream> {
    serve_duplex_for(fixture, capacity, WAIT).await
}

async fn serve_duplex_for(
    fixture: &CommandsFixture,
    capacity: usize,
    idle_timeout: Duration,
) -> WebSocketStream<tokio::io::DuplexStream> {
    let (peer, server) = tokio::io::duplex(capacity);
    let timing = ChannelTiming {
        idle_timeout,
        ping_interval: Duration::from_secs(3600),
        ..ChannelTiming::default()
    };
    let socket = WebSocketStream::from_raw_socket(server, Role::Server, None).await;
    tokio::spawn(serve_channel(
        socket,
        Arc::clone(&fixture.launch.agents),
        Arc::new(fixture.launch.context.clone()),
        timing,
    ));
    WebSocketStream::from_raw_socket(peer, Role::Client, None).await
}

async fn next_reply<S>(socket: &mut WebSocketStream<S>) -> Result<Value>
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
{
    use futures_util::StreamExt;
    let frame = tokio::time::timeout(WAIT, socket.next())
        .await?
        .context("channel closed")??;
    Ok(serde_json::from_str(frame.to_text()?)?)
}

/// The write and kill methods the host received, in arrival order.
fn host_methods(fixture: &CommandsFixture) -> Vec<String> {
    fixture
        .launch
        .host
        .requests()
        .iter()
        .filter(|r| r["method"] == "write" || r["method"] == "kill")
        .map(|r| r["method"].as_str().unwrap_or_default().to_owned())
        .collect()
}

/// The launch lifecycle requests the fake host received, in order.
fn launch_methods(fixture: &CommandsFixture) -> Vec<String> {
    fixture
        .launch
        .host
        .requests()
        .iter()
        .filter_map(|r| r["method"].as_str())
        .filter(|method| matches!(*method, "spawn" | "spawn_commit" | "kill"))
        .map(str::to_owned)
        .collect()
}

fn workspace_path(fixture: &CommandsFixture, run_id: &str) -> Result<PathBuf> {
    let state = persisted_state(&fixture.launch.agents.home)?;
    Ok(state.runs[run_id]
        .workspace
        .as_ref()
        .context("prepared workspace")?
        .path
        .clone())
}

#[tokio::test]
async fn node_channel_dispatches_command_frames() -> Result<()> {
    use futures_util::{SinkExt, StreamExt};
    use gobby_daemon::nodes::channel::{ChannelTiming, run_client};
    use tokio_tungstenite::tungstenite::Message;

    let home = tempfile::tempdir()?;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let path = home.path().join("bootstrap.yaml");
    fs::write(
        &path,
        format!(
            "datastore_mode: remote\nhub_daemon_url: http://{}\napi_key: command-fixture-key\nbind_host: 127.0.0.1\n",
            listener.local_addr()?
        ),
    )?;
    let bootstrap =
        parse_hub_database_bootstrap(&fs::read_to_string(&path)?)?.context("bootstrap")?;
    let (node, _claim) = NodeServices::prepare(&bootstrap, &path, home.path())?.context("node")?;
    let client = AbortTask(tokio::spawn(run_client(node, ChannelTiming::default())));
    let (stream, _) = tokio::time::timeout(WAIT, listener.accept()).await??;
    let mut socket = tokio_tungstenite::accept_async(stream).await?;
    let hello = tokio::time::timeout(WAIT, socket.next())
        .await?
        .context("hello")??;
    assert_eq!(
        serde_json::from_str::<Value>(hello.to_text()?)?["type"],
        "hello"
    );
    socket
        .send(Message::Text(
            json!({"type":"ack","machine_id":"00000000-0000-0000-0000-000000000001"})
                .to_string()
                .into(),
        ))
        .await?;
    // Non-command traffic must leave the writer available for the next reply.
    socket.send(Message::Ping(vec![1, 2, 3].into())).await?;
    let mut frame = command("wire-request", "agent.spawn", json!({}));
    frame["v"] = json!(CONTRACT_VERSION + 1);
    socket.send(Message::Text(frame.to_string().into())).await?;
    let reply = tokio::time::timeout(WAIT, async {
        while let Some(frame) = socket.next().await {
            if let Message::Text(text) = frame? {
                return serde_json::from_str::<Value>(&text).map_err(anyhow::Error::from);
            }
        }
        anyhow::bail!("channel closed before command reply")
    })
    .await;
    assert!(
        reply.is_ok(),
        "run_client ignored a command frame instead of replying"
    );
    error(&reply??, "wire-request", "unsupported_version", "none");
    assert!(persisted_state(home.path())?.runs.is_empty());
    drop(client);
    Ok(())
}

#[tokio::test]
async fn dispatch_rejects_bad_frames() -> Result<()> {
    let fixture = CommandsFixture::new(false).await?;
    let before = persisted_state(&fixture.launch.agents.home)?;
    let mut cases = Vec::new();
    let mut version = fixture.spawn("version");
    version["v"] = json!(CONTRACT_VERSION + 1);
    cases.push((version, "unsupported_version"));
    cases.push((
        command("unknown", "agent.dance", json!({})),
        "unknown_command",
    ));
    cases.push((
        command("malformed", "agent.spawn", json!({"run_id":7})),
        "invalid_payload",
    ));
    for (request, field, value) in [
        ("provider", "provider", json!("unregistered-provider")),
        (
            "missing-root",
            "source_root",
            json!(fixture.launch._root.path().join("missing")),
        ),
        (
            "file-root",
            "source_root",
            json!(fixture.launch._root.path().join("seed-release")),
        ),
    ] {
        let mut frame = fixture.spawn(request);
        frame["payload"][field] = value;
        cases.push((frame, "invalid_payload"));
    }
    for (frame, code) in cases {
        let request = frame["request_id"]
            .as_str()
            .context("request id")?
            .to_owned();
        error(&fixture.call(frame).await, &request, code, "none");
        assert_eq!(persisted_state(&fixture.launch.agents.home)?, before);
        assert!(!fixture.ready.exists());
        assert!(fixture.launch.host.requests().is_empty());
    }
    Ok(())
}

#[tokio::test]
async fn spawn_accepts_before_preparing() -> Result<()> {
    let fixture = CommandsFixture::new(true).await?;
    ok(
        &fixture.call(fixture.spawn("spawn")).await,
        "spawn",
        json!({"accepted":true}),
    );
    assert!(
        persisted_state(&fixture.launch.agents.home)?
            .runs
            .contains_key(&fixture.launch.payload.run_id),
        "acceptance must be durable before reply"
    );
    fixture.seed_entered().await;
    assert!(
        !fixture
            .launch
            .host
            .requests()
            .iter()
            .any(|r| r["method"] == "spawn")
    );
    let before = persisted_state(&fixture.launch.agents.home)?;
    ok(
        &fixture.call(fixture.spawn("duplicate")).await,
        "duplicate",
        json!({"accepted":true}),
    );
    assert_eq!(persisted_state(&fixture.launch.agents.home)?, before);
    fixture.release()?;
    let id = &fixture.launch.payload.run_id;
    fixture
        .wait_state(|state| {
            state
                .runs
                .get(id)
                .is_some_and(|r| r.phase == RunPhase::Launched)
        })
        .await;
    let requests = fixture.launch.host.requests();
    assert_eq!(
        requests.iter().filter(|r| r["method"] == "spawn").count(),
        1
    );
    assert_eq!(
        requests
            .iter()
            .filter(|r| r["method"] == "spawn_commit")
            .count(),
        1
    );
    let before = persisted_state(&fixture.launch.agents.home)?;
    ok(
        &fixture.call(fixture.spawn("live-duplicate")).await,
        "live-duplicate",
        json!({"accepted":true}),
    );
    assert_eq!(persisted_state(&fixture.launch.agents.home)?, before);
    Ok(())
}

#[tokio::test]
async fn spawn_refused_at_capacity() -> Result<()> {
    let fixture = CommandsFixture::new(false).await?;
    {
        let mut state = fixture.launch.agents.state.lock().await;
        for index in 0..1000 {
            let phase = if index % 2 == 0 {
                RunPhase::Accepted
            } else {
                RunPhase::Launched
            };
            state
                .runs
                .insert(format!("occupied-{index}"), run(phase, None));
        }
        state.commit(&fixture.launch.agents.home, &fixture.launch.agents.changed)?;
    }
    let before = persisted_state(&fixture.launch.agents.home)?;
    error(
        &fixture.call(fixture.spawn("capacity")).await,
        "capacity",
        "command_failed",
        "none",
    );
    assert_eq!(persisted_state(&fixture.launch.agents.home)?, before);
    assert!(!fixture.ready.exists());
    assert!(fixture.launch.host.requests().is_empty());
    Ok(())
}

#[tokio::test]
async fn spawn_accepted_beside_exited_records() -> Result<()> {
    let fixture = CommandsFixture::new(true).await?;
    {
        let mut state = fixture.launch.agents.state.lock().await;
        for index in 0..1000 {
            state
                .runs
                .insert(format!("exited-{index}"), run(RunPhase::Exited, None));
        }
        state.commit(&fixture.launch.agents.home, &fixture.launch.agents.changed)?;
    }
    ok(
        &fixture.call(fixture.spawn("spawn")).await,
        "spawn",
        json!({"accepted":true}),
    );
    assert!(
        persisted_state(&fixture.launch.agents.home)?
            .runs
            .contains_key(&fixture.launch.payload.run_id),
        "exited records must not refuse a new run"
    );
    Ok(())
}

#[tokio::test]
async fn spawn_counts_only_live_runs_beside_exited() -> Result<()> {
    let fixture = CommandsFixture::new(true).await?;
    {
        let mut state = fixture.launch.agents.state.lock().await;
        for index in 0..1000 {
            state
                .runs
                .insert(format!("exited-{index}"), run(RunPhase::Exited, None));
        }
        for index in 0..999 {
            state
                .runs
                .insert(format!("live-{index}"), run(RunPhase::Launched, None));
        }
        state.commit(&fixture.launch.agents.home, &fixture.launch.agents.changed)?;
    }
    ok(
        &fixture.call(fixture.spawn("spawn")).await,
        "spawn",
        json!({"accepted":true}),
    );
    Ok(())
}

#[tokio::test]
async fn cancel_before_launch() -> Result<()> {
    let fixture = CommandsFixture::new(true).await?;
    let id = &fixture.launch.payload.run_id;
    ok(
        &fixture.call(fixture.spawn("spawn")).await,
        "spawn",
        json!({"accepted":true}),
    );
    fixture.seed_entered().await;
    let state = persisted_state(&fixture.launch.agents.home)?;
    let workspace = state.runs[id]
        .workspace
        .as_ref()
        .context("prepared workspace")?
        .path
        .clone();
    assert!(workspace.is_dir());
    ok(
        &fixture.call(fixture.cancel("cancel", id, "TERM")).await,
        "cancel",
        json!({"stopped":true}),
    );
    assert!(
        persisted_state(&fixture.launch.agents.home)?
            .cancelled
            .contains_key(id),
        "cancel record must be durable before reply"
    );
    fixture.exit(ExitReason::Cancelled, false).await?;
    assert!(!workspace.exists());
    assert!(
        !fixture
            .launch
            .host
            .requests()
            .iter()
            .any(|r| r["method"] == "spawn")
    );
    let before = persisted_state(&fixture.launch.agents.home)?;
    error(
        &fixture.call(fixture.spawn("late")).await,
        "late",
        "cancelled",
        "none",
    );
    assert_eq!(persisted_state(&fixture.launch.agents.home)?, before);
    error(
        &fixture
            .call(fixture.cancel("unknown", "never-accepted", "TERM"))
            .await,
        "unknown",
        "not_found",
        "none",
    );
    assert!(
        persisted_state(&fixture.launch.agents.home)?
            .cancelled
            .contains_key("never-accepted"),
        "unknown cancels still fence later spawns"
    );
    let mut late = fixture.spawn("unknown-late");
    late["payload"]["run_id"] = json!("never-accepted");
    error(
        &fixture.call(late).await,
        "unknown-late",
        "cancelled",
        "none",
    );
    Ok(())
}

#[tokio::test]
async fn cancel_launched_run() -> Result<()> {
    for signal in ["TERM", "KILL"] {
        let fixture = CommandsFixture::new(false).await?;
        let id = &fixture.launch.payload.run_id;
        fixture.known(id, "fixture-terminal").await?;
        // The existing watcher fixture emits this host identity.
        {
            let mut state = fixture.launch.agents.state.lock().await;
            state
                .runs
                .get_mut(id)
                .context("known run")?
                .host_terminal_id = Some("fixture-host-terminal".into());
            state.commit(&fixture.launch.agents.home, &fixture.launch.agents.changed)?;
        }
        let watcher = tokio::spawn({
            let agents = fixture.launch.agents.clone();
            async move { watch_exits(&agents).await }
        });
        let watcher = AbortTask(watcher);
        tokio::time::timeout(WAIT, async {
            while !fixture
                .launch
                .host
                .requests()
                .iter()
                .any(|r| r["method"] == "subscribe_events")
            {
                tokio::task::yield_now().await;
            }
        })
        .await?;
        ok(
            &fixture.call(fixture.cancel("kill", id, signal)).await,
            "kill",
            json!({"stopped":true}),
        );
        let state = persisted_state(&fixture.launch.agents.home)?;
        assert!(state.cancelled.contains_key(id));
        assert!(state.runs.contains_key(id), "watcher owns exit settlement");
        let requests = fixture.launch.host.requests();
        let kill = requests
            .iter()
            .find(|r| r["method"] == "kill")
            .context("host kill")?;
        assert_eq!(kill["host_terminal_id"], "fixture-host-terminal");
        assert_eq!(kill["signal"], signal);
        error(
            &fixture.call(fixture.spawn("cancelled-live")).await,
            "cancelled-live",
            "cancelled",
            "none",
        );
        fixture.launch.host.terminal_exited()?;
        fixture.exit(ExitReason::Cancelled, true).await?;
        drop(watcher);
    }
    Ok(())
}

struct AbortTask<T>(tokio::task::JoinHandle<T>);
impl<T> Drop for AbortTask<T> {
    fn drop(&mut self) {
        self.0.abort();
    }
}

#[tokio::test]
async fn preparation_failures_report_exits() -> Result<()> {
    // Missing git base: workspace preparation fails before any host launch.
    let mut fixture = CommandsFixture::new(false).await?;
    fixture.launch.payload.workspace.base_branch = Some("no-such-base".into());
    ok(
        &fixture.call(fixture.spawn("prepare-failure")).await,
        "prepare-failure",
        json!({"accepted":true}),
    );
    fixture.exit(ExitReason::PrepareFailed, false).await?;
    assert!(
        !fixture
            .launch
            .host
            .requests()
            .iter()
            .any(|r| r["method"] == "spawn")
    );
    // Hold real workspace preparation, then advance the production 300s deadline.
    let fixture = CommandsFixture::new(true).await?;
    ok(
        &fixture.call(fixture.spawn("deadline")).await,
        "deadline",
        json!({"accepted":true}),
    );
    fixture.seed_entered().await;
    let state = persisted_state(&fixture.launch.agents.home)?;
    let workspace = state.runs[&fixture.launch.payload.run_id]
        .workspace
        .as_ref()
        .context("workspace")?
        .path
        .clone();
    tokio::time::pause();
    tokio::time::advance(Duration::from_secs(301)).await;
    tokio::time::resume();
    fixture.exit(ExitReason::PrepareTimeout, false).await?;
    assert!(
        !workspace.exists(),
        "deadline removes the workspace it created"
    );
    // Reject host commit after preparation; created isolation must be removed,
    // while an existing/resumed workspace must remain with its owner.
    for reused in [false, true] {
        let mut fixture = CommandsFixture::new(false).await?;
        let expected = if reused {
            let path = fixture
                .launch
                .agents
                .home
                .join("worktrees")
                .join(PROJECT_ID)
                .join("existing");
            let source = PathBuf::from(&fixture.launch.payload.source_root);
            fs::create_dir_all(path.parent().context("worktree parent")?)?;
            // Existing-mode paths must be inside this project's owned worktrees.
            git(
                &source,
                &[
                    "worktree",
                    "add",
                    "-b",
                    "existing",
                    path.to_str().context("workspace path")?,
                    "main",
                ],
            )?;
            fixture.launch.payload.workspace = existing_spawn(&path, "existing");
            path
        } else {
            fixture
                .launch
                .agents
                .home
                .join("worktrees")
                .join(PROJECT_ID)
                .join(&fixture.launch.payload.run_id)
        };
        fixture.launch.host.reject_next_commit();
        ok(
            &fixture.call(fixture.spawn("launch-failure")).await,
            "launch-failure",
            json!({"accepted":true}),
        );
        fixture.exit(ExitReason::LaunchFailed, false).await?;
        assert_eq!(
            expected.exists(),
            reused,
            "reused workspace ownership must survive failure"
        );
    }
    Ok(())
}

#[tokio::test]
async fn terminal_write_dispositions() -> Result<()> {
    let fixture = CommandsFixture::new(false).await?;
    fixture.known("write-run", "terminal").await?;
    ok(
        &fixture.call(write("write", "terminal", "hello")).await,
        "write",
        json!({}),
    );
    let requests = fixture.launch.host.requests();
    let sent = requests
        .iter()
        .find(|r| r["method"] == "write")
        .context("host write")?;
    assert_eq!(sent["host_terminal_id"], "host-terminal");
    assert_eq!(sent["kind"], "text");
    assert_eq!(written_text(sent), "hello");
    assert_eq!(sent["submit"], true);
    let count = requests.len();
    error(
        &fixture.call(write("unknown", "missing", "x")).await,
        "unknown",
        "not_found",
        "none",
    );
    assert_eq!(fixture.launch.host.requests().len(), count);
    fixture.launch.host.disconnect_next_write();
    error(
        &fixture
            .call(write("partial", "terminal", "may-have-arrived"))
            .await,
        "partial",
        "command_failed",
        "partial",
    );
    assert_eq!(
        fixture
            .launch
            .host
            .requests()
            .iter()
            .filter(|r| r["method"] == "write")
            .count(),
        2,
        "uncertain writes must not be replayed"
    );
    let CommandsFixture { launch, .. } = fixture;
    drop(launch.host);
    let reply = dispatch(
        launch.agents,
        Arc::new(launch.context),
        write("unavailable", "terminal", "x"),
    )
    .await;
    error(&reply, "unavailable", "command_failed", "none");
    Ok(())
}

#[tokio::test]
async fn same_run_commands_serialize() -> Result<()> {
    let fixture = CommandsFixture::new(false).await?;
    fixture.known("run-a", "a").await?;
    fixture.known("run-b", "b").await?;
    fixture.launch.host.pause_next_write();
    let first = tokio::spawn(dispatch(
        fixture.launch.agents.clone(),
        Arc::new(fixture.launch.context.clone()),
        write("first", "a", "first"),
    ));
    let mut first = AbortTask(first);
    tokio::select! {
        outcome = &mut first.0 => panic!("first command returned before reaching the host: {outcome:?}"),
        ready = tokio::time::timeout(WAIT, fixture.launch.host.wait_for_write()) => { ready?; }
    }
    let mut second = Box::pin(dispatch(
        fixture.launch.agents.clone(),
        Arc::new(fixture.launch.context.clone()),
        write("second", "a", "second"),
    ));
    assert!(
        futures_util::poll!(second.as_mut()).is_pending(),
        "same-run write must wait for first reply"
    );
    // A different run must complete while run-a's host call is deliberately held.
    ok(
        &fixture.call(write("other", "b", "other")).await,
        "other",
        json!({}),
    );
    assert!(
        !fixture
            .launch
            .host
            .requests()
            .iter()
            .any(|r| r["method"] == "write" && written_text(r) == "second")
    );
    fixture.launch.host.release_write();
    ok(
        &tokio::time::timeout(WAIT, &mut first.0).await??,
        "first",
        json!({}),
    );
    ok(
        &tokio::time::timeout(WAIT, second).await?,
        "second",
        json!({}),
    );
    let writes: Vec<_> = fixture
        .launch
        .host
        .requests()
        .into_iter()
        .filter(|r| r["method"] == "write")
        .map(|r| written_text(&r))
        .collect();
    assert_eq!(writes, vec!["first", "other", "second"]);
    Ok(())
}

#[tokio::test]
async fn same_run_cancel_waits_for_write() -> Result<()> {
    // test-quality: allow SLEEP_IN_TEST -- the held cancel must stay unfinished for a bounded window; absence of a reply cannot be awaited, and paused time would advance over the fake host's real socket I/O
    let fixture = CommandsFixture::new(false).await?;
    fixture.known("run-a", "a").await?;
    fixture.launch.host.pause_next_write();
    let mut first = fixture.spawn_dispatch(write("first", "a", "first"));
    tokio::time::timeout(WAIT, fixture.launch.host.wait_for_write()).await?;
    let mut kill = fixture.spawn_dispatch(fixture.cancel("kill", "run-a", "KILL"));
    tokio::select! {
        outcome = &mut kill.0 => panic!("cancel returned before the same-run write: {outcome:?}"),
        () = tokio::time::sleep(Duration::from_millis(500)) => {}
    }
    assert!(
        !fixture
            .launch
            .host
            .requests()
            .iter()
            .any(|r| r["method"] == "kill"),
        "kill must wait for the held write"
    );
    fixture.launch.host.release_write();
    ok(
        &tokio::time::timeout(WAIT, &mut first.0).await??,
        "first",
        json!({}),
    );
    ok(
        &tokio::time::timeout(WAIT, &mut kill.0).await??,
        "kill",
        json!({"stopped": true}),
    );
    assert_eq!(host_methods(&fixture), ["write", "kill"]);
    Ok(())
}

#[tokio::test]
async fn channel_orders_same_run_commands() -> Result<()> {
    use futures_util::SinkExt;
    use tokio_tungstenite::tungstenite::Message;

    let fixture = CommandsFixture::new(false).await?;
    fixture.known("run-a", "a").await?;
    fixture.launch.host.pause_next_write();
    let mut socket = serve_duplex(&fixture, 64 * 1024).await;
    socket
        .send(Message::Text(
            write("first", "a", "first").to_string().into(),
        ))
        .await?;
    tokio::time::timeout(WAIT, fixture.launch.host.wait_for_write()).await?;
    socket
        .send(Message::Text(
            fixture.cancel("kill", "run-a", "KILL").to_string().into(),
        ))
        .await?;
    // Neither a reply nor a kill may leave while the earlier write is held.
    assert!(
        tokio::time::timeout(Duration::from_millis(500), next_reply(&mut socket))
            .await
            .is_err(),
        "no reply may pass the held write"
    );
    assert!(
        !host_methods(&fixture).iter().any(|method| method == "kill"),
        "kill must not pass the held write"
    );
    fixture.launch.host.release_write();
    ok(&next_reply(&mut socket).await?, "first", json!({}));
    ok(
        &next_reply(&mut socket).await?,
        "kill",
        json!({"stopped": true}),
    );
    assert_eq!(host_methods(&fixture), ["write", "kill"]);
    Ok(())
}

#[tokio::test]
async fn preparation_waits_for_acceptance_send() -> Result<()> {
    // test-quality: allow SLEEP_IN_TEST -- preparation must stay unstarted for a bounded window while the acceptance is unread; absence cannot be awaited, and paused time would advance over the real duplex and seed child I/O
    use futures_util::SinkExt;
    use tokio_tungstenite::tungstenite::Message;

    let fixture = CommandsFixture::new(true).await?;
    let id = fixture.launch.payload.run_id.clone();
    // A 16-byte channel holds the acceptance reply until this peer reads it.
    let mut socket = serve_duplex(&fixture, 16).await;
    socket
        .send(Message::Text(fixture.spawn("spawn").to_string().into()))
        .await?;
    tokio::time::sleep(Duration::from_secs(2)).await;
    assert!(
        !fixture.ready.exists(),
        "preparation must not start before the acceptance is sent"
    );
    ok(
        &next_reply(&mut socket).await?,
        "spawn",
        json!({"accepted": true}),
    );
    fixture.seed_entered().await;
    fixture.release()?;
    fixture
        .wait_state(|state| {
            state
                .runs
                .get(&id)
                .is_some_and(|r| r.phase == RunPhase::Launched)
        })
        .await;
    Ok(())
}

#[tokio::test]
async fn blocked_policy_cancel_never_launches() -> Result<()> {
    let fixture = CommandsFixture::blocking_policy().await?;
    let id = fixture.launch.payload.run_id.clone();
    ok(
        &fixture.call(fixture.spawn("spawn")).await,
        "spawn",
        json!({"accepted": true}),
    );
    fixture.policy_entered().await;
    let workspace = workspace_path(&fixture, &id)?;
    let mut cancel = fixture.spawn_dispatch(fixture.cancel("cancel", &id, "TERM"));
    fixture
        .wait_state(|state| state.cancelled.contains_key(&id))
        .await;
    fixture.release_policy()?;
    ok(
        &tokio::time::timeout(WAIT, &mut cancel.0).await??,
        "cancel",
        json!({"stopped": true}),
    );
    fixture.exit(ExitReason::Cancelled, false).await?;
    assert!(!workspace.exists());
    assert!(
        !fixture
            .launch
            .host
            .requests()
            .iter()
            .any(|r| r["method"] == "spawn"),
        "a run cancelled during policy compile must never reach the host"
    );
    Ok(())
}

#[tokio::test]
async fn policy_stage_is_bounded_by_the_deadline() -> Result<()> {
    let fixture = CommandsFixture::blocking_policy().await?;
    let id = fixture.launch.payload.run_id.clone();
    ok(
        &fixture.call(fixture.spawn("spawn")).await,
        "spawn",
        json!({"accepted": true}),
    );
    fixture.policy_entered().await;
    let workspace = workspace_path(&fixture, &id)?;
    // The policy compile is still held when the 300 s preparation deadline passes.
    tokio::time::pause();
    tokio::time::advance(Duration::from_secs(301)).await;
    tokio::time::resume();
    fixture.release_policy()?;
    fixture.exit(ExitReason::PrepareTimeout, false).await?;
    assert!(!workspace.exists());
    assert!(
        !fixture
            .launch
            .host
            .requests()
            .iter()
            .any(|r| r["method"] == "spawn"),
        "a timed-out run must never reach the host"
    );
    Ok(())
}

#[tokio::test]
async fn spawn_refuses_unmanaged_sandbox() -> Result<()> {
    for (enabled, backend, backend_name) in [
        (false, SandboxBackend::Srt, "srt"),
        (true, SandboxBackend::ProviderNative, "provider-native"),
    ] {
        let mut fixture = CommandsFixture::new(false).await?;
        fixture.launch.payload.sandbox.enabled = enabled;
        fixture.launch.payload.sandbox.backend = backend;
        // Managed-SRT refusal must take precedence over the cancel-record check.
        {
            let mut state = fixture.launch.agents.state.lock().await;
            state
                .cancelled
                .insert(fixture.launch.payload.run_id.clone(), u64::MAX);
            state.commit(&fixture.launch.agents.home, &fixture.launch.agents.changed)?;
        }
        let before = persisted_state(&fixture.launch.agents.home)?;
        let reply = fixture.call(fixture.spawn("sandbox")).await;
        error(&reply, "sandbox", "invalid_payload", "none");
        assert_eq!(
            reply["error"]["message"],
            format!(
                "spawn_agent requires managed SRT (enabled={}, backend={backend_name})",
                if enabled { "True" } else { "False" }
            )
        );
        assert_eq!(persisted_state(&fixture.launch.agents.home)?, before);
        assert!(!fixture.ready.exists());
        assert!(fixture.launch.host.requests().is_empty());
    }
    Ok(())
}

/// 2.7.8: a spawn waits for its run's earlier commands like any other command, while another
/// run's spawn is answered at once.
#[tokio::test]
async fn channel_orders_spawns_with_same_run_commands() -> Result<()> {
    use futures_util::SinkExt;
    use tokio_tungstenite::tungstenite::Message;

    let fixture = CommandsFixture::new(true).await?;
    fixture.known("run-a", "a").await?;
    let same_run = |request: &str| {
        let mut frame = fixture.spawn(request);
        frame["payload"]["run_id"] = json!("run-a");
        frame
    };
    fixture.launch.host.pause_next_write();
    let mut socket = serve_duplex(&fixture, 64 * 1024).await;
    socket
        .send(Message::Text(
            write("first", "a", "first").to_string().into(),
        ))
        .await?;
    tokio::time::timeout(WAIT, fixture.launch.host.wait_for_write()).await?;
    for frame in [
        same_run("duplicate"),
        fixture.cancel("kill", "run-a", "KILL"),
        same_run("late"),
        fixture.spawn("other"),
    ] {
        socket.send(Message::Text(frame.to_string().into())).await?;
    }
    ok(
        &next_reply(&mut socket).await?,
        "other",
        json!({"accepted": true}),
    );
    assert!(
        tokio::time::timeout(Duration::from_millis(500), next_reply(&mut socket))
            .await
            .is_err(),
        "no run-a reply may pass the held write"
    );
    fixture.launch.host.release_write();
    ok(&next_reply(&mut socket).await?, "first", json!({}));
    ok(
        &next_reply(&mut socket).await?,
        "duplicate",
        json!({"accepted": true}),
    );
    ok(
        &next_reply(&mut socket).await?,
        "kill",
        json!({"stopped": true}),
    );
    error(&next_reply(&mut socket).await?, "late", "cancelled", "none");
    assert_eq!(host_methods(&fixture), ["write", "kill"]);
    Ok(())
}

/// 2.7.4: a cancel committed while the host's spawn reply is in flight aborts the prepared
/// child. The run never reaches `spawn_commit` and settles unlaunched.
#[tokio::test]
async fn cancel_during_host_spawn_never_commits() -> Result<()> {
    let fixture = CommandsFixture::new(false).await?;
    let id = fixture.launch.payload.run_id.clone();
    fixture.launch.host.hold_next_reply("spawn");
    ok(
        &fixture.call(fixture.spawn("spawn")).await,
        "spawn",
        json!({"accepted": true}),
    );
    tokio::time::timeout(WAIT, fixture.launch.host.wait_for_held_reply("spawn")).await?;
    let workspace = workspace_path(&fixture, &id)?;
    let mut cancel = fixture.spawn_dispatch(fixture.cancel("cancel", &id, "TERM"));
    fixture
        .wait_state(|state| state.cancelled.contains_key(&id))
        .await;
    fixture.launch.host.release_held_reply("spawn");
    ok(
        &tokio::time::timeout(WAIT, &mut cancel.0).await??,
        "cancel",
        json!({"stopped": true}),
    );
    assert_eq!(
        launch_methods(&fixture),
        ["spawn", "kill"],
        "the prepared child is aborted, never committed"
    );
    fixture.exit(ExitReason::Cancelled, false).await?;
    assert!(!workspace.exists());
    Ok(())
}

/// 2.7.4: a cancel committed while the host's commit reply is in flight still settles the run
/// unlaunched. The child is aborted and the run is never recorded as launched.
#[tokio::test]
async fn cancel_during_host_commit_settles_unlaunched() -> Result<()> {
    let fixture = CommandsFixture::new(false).await?;
    let id = fixture.launch.payload.run_id.clone();
    fixture.launch.host.hold_next_reply("spawn_commit");
    ok(
        &fixture.call(fixture.spawn("spawn")).await,
        "spawn",
        json!({"accepted": true}),
    );
    tokio::time::timeout(
        WAIT,
        fixture.launch.host.wait_for_held_reply("spawn_commit"),
    )
    .await?;
    let workspace = workspace_path(&fixture, &id)?;
    let mut cancel = fixture.spawn_dispatch(fixture.cancel("cancel", &id, "TERM"));
    fixture
        .wait_state(|state| state.cancelled.contains_key(&id))
        .await;
    fixture.launch.host.release_held_reply("spawn_commit");
    ok(
        &tokio::time::timeout(WAIT, &mut cancel.0).await??,
        "cancel",
        json!({"stopped": true}),
    );
    assert!(
        !persisted_state(&fixture.launch.agents.home)?
            .runs
            .contains_key(&id),
        "a run cancelled before its launch record must not be recorded as launched"
    );
    fixture.exit(ExitReason::Cancelled, false).await?;
    assert!(!workspace.exists());
    assert_eq!(launch_methods(&fixture), ["spawn", "spawn_commit", "kill"]);
    Ok(())
}

/// 2.7.2: an acceptance the hub never receives starts no preparation. The run is cancelled
/// and settles unlaunched, so a later spawn for it is refused.
#[tokio::test]
async fn undelivered_acceptance_cancels_the_run() -> Result<()> {
    use futures_util::SinkExt;
    use tokio_tungstenite::tungstenite::Message;

    let fixture = CommandsFixture::new(true).await?;
    let id = fixture.launch.payload.run_id.clone();
    // A 16-byte channel holds the acceptance until this peer reads it, and it never does.
    let mut socket = serve_duplex(&fixture, 16).await;
    socket
        .send(Message::Text(fixture.spawn("spawn").to_string().into()))
        .await?;
    fixture
        .wait_state(|state| state.runs.contains_key(&id))
        .await;
    drop(socket);
    assert_unprepared_cancel(&fixture).await?;
    error(
        &fixture.call(fixture.spawn("retry")).await,
        "retry",
        "cancelled",
        "none",
    );
    Ok(())
}

/// 2.7.2: an acceptance still unread when the channel's idle bound expires is undelivered.
#[tokio::test]
async fn unread_acceptance_times_out_and_cancels_the_run() -> Result<()> {
    use futures_util::SinkExt;
    use tokio_tungstenite::tungstenite::Message;

    let fixture = CommandsFixture::new(true).await?;
    let mut socket = serve_duplex_for(&fixture, 16, Duration::from_secs(1)).await;
    socket
        .send(Message::Text(fixture.spawn("spawn").to_string().into()))
        .await?;
    // The peer stays connected but never reads, so the send outlives the idle bound.
    assert_unprepared_cancel(&fixture).await?;
    drop(socket);
    Ok(())
}

/// Waits until the run either settles or starts preparing, then requires the settled outcome:
/// a durable cancel, an unlaunched `cancelled` exit, and no preparation or host request.
async fn assert_unprepared_cancel(fixture: &CommandsFixture) -> Result<()> {
    let id = &fixture.launch.payload.run_id;
    fixture
        .wait_state(|state| {
            state.exits.iter().any(|entry| &entry.exit.run_id == id)
                || state
                    .runs
                    .get(id)
                    .is_some_and(|run| run.workspace.is_some())
        })
        .await;
    let state = persisted_state(&fixture.launch.agents.home)?;
    assert!(
        !state.runs.contains_key(id),
        "an undelivered acceptance must not leave the run preparing"
    );
    assert!(state.cancelled.contains_key(id));
    fixture.exit(ExitReason::Cancelled, false).await?;
    assert!(!fixture.ready.exists());
    assert!(fixture.launch.host.requests().is_empty());
    Ok(())
}
