//! #24376: the dispatcher owns validation, run ordering and async preparation.
use super::*;
use gobby_daemon::{
    node_commands::{CONTRACT_VERSION, SandboxBackend},
    nodes::agents::commands::dispatch,
};

const WAIT: Duration = Duration::from_secs(30);

struct CommandsFixture {
    launch: LaunchFixture,
    ready: PathBuf,
    release: PathBuf,
}

impl CommandsFixture {
    async fn new(block_preparation: bool) -> Result<Self> {
        let mut launch = LaunchFixture::new().await?;
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
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("preparation must reach the isolated uv barrier");
    }

    fn release(&self) -> Result<()> {
        fs::write(&self.release, "release")?;
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
        assert!(!state.runs.contains_key(id));
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
