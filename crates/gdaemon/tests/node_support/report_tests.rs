//! #24377: exercise reports over the real relay, with isolated HTTP and terminal hosts.

use super::{fake_gterm, persisted_state};
use anyhow::{Context, Result};
use axum::{
    Router,
    body::Bytes,
    extract::State,
    http::{HeaderMap, StatusCode},
    routing::post,
};
use gobby_core::bootstrap::parse_hub_database_bootstrap;
use gobby_daemon::{
    heartbeat::HeartbeatHost,
    node_commands::{CONTRACT_VERSION, ExitReason, ReportBody, ReportExit, WorkspaceMode},
    nodes::agents::{
        NodeAgents, reports,
        state::{ExitRecord, RunPhase, RunRecord, WorkspaceRecord},
    },
    state::NodeServices,
};
use serde_json::{Value, json};
use std::{fs, path::Path, sync::Arc, time::Duration};
use tokio::{
    sync::{mpsc, oneshot},
    task::JoinHandle,
};

const KEY: &str = "isolated-node-report-test-key";

#[derive(Debug)]
struct Exchange {
    body: ReportBody,
    authorization: Option<String>,
    response: oneshot::Sender<(StatusCode, Value)>,
}

impl Exchange {
    fn answer(self, status: StatusCode, body: Value) {
        self.response
            .send((status, body))
            .expect("report still in flight");
    }
}

async fn capture_report(
    State(sender): State<mpsc::UnboundedSender<Exchange>>,
    headers: HeaderMap,
    bytes: Bytes,
) -> (StatusCode, String) {
    let body = serde_json::from_slice(&bytes).expect("valid report contract");
    let (response, received) = oneshot::channel();
    sender
        .send(Exchange {
            body,
            authorization: headers
                .get("authorization")
                .and_then(|h| h.to_str().ok())
                .map(str::to_owned),
            response,
        })
        .expect("report test receiver is live");
    let (status, body) = received
        .await
        .unwrap_or((StatusCode::SERVICE_UNAVAILABLE, json!({})));
    (status, body.to_string())
}

struct Hub {
    url: String,
    received: mpsc::UnboundedReceiver<Exchange>,
    server: JoinHandle<()>,
}

impl Hub {
    async fn start() -> Result<Self> {
        let (sender, received) = mpsc::unbounded_channel();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let url = format!("http://{}", listener.local_addr()?);
        let app = Router::new()
            .route("/api/nodes/report", post(capture_report))
            .with_state(sender);
        let server = tokio::spawn(async move {
            axum::serve(listener, app).await.expect("fake report hub");
        });
        Ok(Self {
            url,
            received,
            server,
        })
    }

    async fn next(&mut self, running: &mut Running) -> Exchange {
        tokio::select! {
            report = self.received.recv() => report.expect("hub stays live"),
            ended = &mut running.0 => panic!("report task ended before posting: {ended:?}"),
            () = tokio::time::sleep(Duration::from_secs(3)) => panic!("node did not post a report"),
        }
    }

    async fn no_report(&mut self) {
        assert!(
            tokio::time::timeout(Duration::from_millis(80), self.received.recv())
                .await
                .is_err(),
            "a second report arrived while blocked, or an internal commit caused a report storm"
        );
    }
}

impl Drop for Hub {
    fn drop(&mut self) {
        self.server.abort();
    }
}

struct Running(JoinHandle<Result<()>>);

impl Running {
    fn once(node: Arc<NodeServices>) -> Self {
        Self(tokio::spawn(
            async move { reports::report_once(&node).await },
        ))
    }

    fn reports(node: Arc<NodeServices>) -> Self {
        Self(tokio::spawn(async move {
            reports::report_loop(node).await;
            Ok(())
        }))
    }

    async fn finish(&mut self) -> Result<()> {
        tokio::time::timeout(Duration::from_secs(3), &mut self.0)
            .await
            .expect("report response must release the request")
            .expect("report request must not panic")
    }

    async fn stop(&mut self) {
        self.0.abort();
        let _ = (&mut self.0).await;
    }
}

impl Drop for Running {
    fn drop(&mut self) {
        self.0.abort();
    }
}

struct Fixture {
    home: tempfile::TempDir,
    hub: Hub,
    host: fake_gterm::FakeGterm,
    node: Arc<NodeServices>,
}

fn write_bootstrap(home: &Path, url: &str, http: u16, ws: u16) -> Result<()> {
    fs::write(
        home.join("bootstrap.yaml"),
        format!(
            "datastore_mode: remote\nhub_daemon_url: {url}\napi_key: {KEY}\ndaemon_port: {http}\nwebsocket_port: {ws}\nbind_host: 127.0.0.1\n"
        ),
    )?;
    fs::write(home.join("machine_id"), uuid::Uuid::new_v4().to_string())?;
    Ok(())
}

fn prepare_node(home: &Path) -> Result<Arc<NodeServices>> {
    let path = home.join("bootstrap.yaml");
    let bootstrap =
        parse_hub_database_bootstrap(&fs::read_to_string(&path)?)?.context("node bootstrap")?;
    let (node, _claim) =
        NodeServices::prepare(&bootstrap, &path, home)?.context("isolated PID claim")?;
    Ok(node)
}

impl Fixture {
    async fn new(inventory: Vec<Value>) -> Result<Self> {
        let home = tempfile::tempdir()?;
        let hub = Hub::start().await?;
        write_bootstrap(home.path(), &hub.url, 60870, 60871)?;
        let host = fake_gterm::FakeGterm::start_with_inventory(home.path(), inventory).await?;
        let node = prepare_node(home.path())?;
        {
            let mut state = node.agents.state.lock().await;
            state.host_epoch = 7;
            state.host_epoch_id = Some(fake_gterm::EPOCH.into());
            state.commit(home.path(), &node.agents.changed)?;
        }
        Ok(Self {
            home,
            hub,
            host,
            node,
        })
    }

    async fn put_run(
        &self,
        id: &str,
        phase: RunPhase,
        workspace: Option<WorkspaceRecord>,
    ) -> Result<()> {
        let mut state = self.node.agents.state.lock().await;
        state.runs.insert(
            id.into(),
            RunRecord {
                terminal_id: format!("terminal-{id}"),
                phase,
                accepted_at_ms: 1,
                workspace,
                host_terminal_id: (phase == RunPhase::Launched).then(|| format!("host-{id}")),
            },
        );
        state.commit(self.home.path(), &self.node.agents.changed)
    }
}

fn record(home: &Path, id: &str) -> WorkspaceRecord {
    WorkspaceRecord {
        mode: WorkspaceMode::Clone,
        path: home.join("clones/project").join(id),
        branch_name: format!("agent/{id}"),
        base_branch: Some("main".into()),
        source_root: home.join("source"),
        reused: false,
    }
}

fn ended(id: &str, order: u64, workspace: Option<&WorkspaceRecord>) -> ExitRecord {
    ExitRecord {
        ended_at_ms: order,
        exit: ReportExit {
            run_id: id.into(), terminal_id: format!("terminal-{id}"), launched: workspace.is_some(),
            exit_code: workspace.map(|_| 0),
            reason: if workspace.is_some() { ExitReason::Exited } else { ExitReason::PrepareFailed },
            workspace: workspace.map(|w| serde_json::from_value(json!({
                "mode":"clone", "path":w.path, "branch_name":w.branch_name, "base_branch":w.base_branch,
            })).expect("report workspace")), sandbox: None,
        },
    }
}

async fn advance(seconds: u64) {
    tokio::time::pause();
    tokio::time::advance(Duration::from_secs(seconds)).await;
    // Poll timers before resuming real time for local socket I/O.
    tokio::task::yield_now().await;
    tokio::time::resume();
}

#[tokio::test]
async fn reports_are_fresh_and_serial() -> Result<()> {
    let mut f = Fixture::new(vec![]).await?;
    // report_once itself must serialize callers, even outside the background loop.
    let mut first = Running::once(f.node.clone());
    let a = f.hub.next(&mut first).await;
    let first_revision = a.body.revision;
    let mut concurrent = Running::once(f.node.clone());
    f.hub.no_report().await;
    a.answer(
        StatusCode::SERVICE_UNAVAILABLE,
        json!({"code":"unavailable"}),
    );
    let _ = first.finish().await; // HTTP failure may be represented as Err or retained state.
    let b = f.hub.next(&mut concurrent).await;
    let previous = b.body.revision;
    assert!(
        previous > first_revision,
        "the failed report consumes its persisted revision"
    );
    b.answer(StatusCode::OK, json!({}));
    concurrent.finish().await?;

    let mut heartbeat = HeartbeatHost::new();
    reports::register_jobs(&mut heartbeat, f.node.clone())?;
    let mut background = Running::reports(f.node.clone());
    heartbeat.start()?;
    f.node.agents.changed.notify_one();
    let c = f.hub.next(&mut background).await;
    assert!(c.body.revision > previous);
    let revision = c.body.revision;
    f.put_run("queued-a", RunPhase::Accepted, None).await?;
    f.put_run("queued-b", RunPhase::Preparing, None).await?;
    f.hub.no_report().await;
    c.answer(StatusCode::OK, json!({}));
    let d = f.hub.next(&mut background).await;
    assert!(d.body.revision > revision);
    assert_eq!(d.body.accepted, ["queued-a", "queued-b"]);
    let revision = d.body.revision;
    d.answer(StatusCode::OK, json!({}));
    f.hub.no_report().await; // Two in-flight changes coalesce to exactly one follow-up.
    advance(29).await;
    f.hub.no_report().await;
    advance(1).await;
    let tick = f.hub.next(&mut background).await;
    assert!(tick.body.revision > revision);
    assert_eq!(tick.body.accepted, ["queued-a", "queued-b"]);
    let before_restart = tick.body.revision;
    tick.answer(StatusCode::OK, json!({}));
    heartbeat.stop().await?;
    background.stop().await;
    f.node = prepare_node(f.home.path())?;
    let mut restarted = Running::once(f.node.clone());
    let after = f.hub.next(&mut restarted).await;
    assert!(
        after.body.revision > before_restart,
        "restart must never reuse a revision"
    );
    after.answer(StatusCode::OK, json!({}));
    restarted.finish().await?;
    Ok(())
}

#[tokio::test]
async fn stale_report_adopts_epoch() -> Result<()> {
    let mut f = Fixture::new(vec![]).await?;
    let mut background = Running::reports(f.node.clone());
    f.node.agents.changed.notify_one();
    let first = f.hub.next(&mut background).await;
    let revision = first.body.revision;
    first.answer(
        StatusCode::CONFLICT,
        json!({"code":"stale_report", "hub_epoch":73}),
    );
    let fresh = f.hub.next(&mut background).await;
    assert_eq!(fresh.body.hub_epoch, Some(73));
    assert!(fresh.body.revision > revision);
    assert_eq!(persisted_state(f.home.path())?.hub_epoch, Some(73));
    fresh.answer(StatusCode::OK, json!({}));
    f.hub.no_report().await;
    background.stop().await;

    // Other conflicts must neither replace the epoch nor acknowledge pending exits.
    {
        let mut state = f.node.agents.state.lock().await;
        state.exits.push(ended("retain", 1, None));
        state.commit(f.home.path(), &f.node.agents.changed)?;
    }
    let mut once = Running::once(f.node.clone());
    f.hub.next(&mut once).await.answer(
        StatusCode::CONFLICT,
        json!({"code":"other", "hub_epoch":99}),
    );
    let _ = once.finish().await;
    let state = persisted_state(f.home.path())?;
    assert_eq!(state.hub_epoch, Some(73));
    assert_eq!(state.exits[0].exit.run_id, "retain");
    Ok(())
}

#[tokio::test]
async fn exits_page_oldest_first_until_acknowledged() -> Result<()> {
    let mut f = Fixture::new(vec![json!({
        "terminal_id":"fixture-terminal", "host_terminal_id":"fixture-host-terminal",
        "pgid":4242, "start_time":1.0,
    })])
    .await?;
    let workspace = record(f.home.path(), "exit-0000");
    {
        let mut state = f.node.agents.state.lock().await;
        state.runs.insert(
            "exit-0000".into(),
            RunRecord {
                terminal_id: "fixture-terminal".into(),
                phase: RunPhase::Launched,
                accepted_at_ms: 0,
                host_terminal_id: Some("fixture-host-terminal".into()),
                workspace: Some(workspace.clone()),
            },
        );
        state.commit(f.home.path(), &f.node.agents.changed)?;
    }
    let agents = f.node.agents.clone();
    let mut watcher = Running(tokio::spawn(async move {
        gobby_daemon::nodes::agents::launch::watch_exits(&agents).await
    }));
    let subscribed = tokio::time::timeout(Duration::from_secs(3), async {
        while !f
            .host
            .requests()
            .iter()
            .any(|r| r["method"] == "subscribe_events")
        {
            tokio::task::yield_now().await;
        }
    })
    .await;
    assert!(subscribed.is_ok(), "fixture exit watcher must subscribe");
    f.host.terminal_exited()?;
    let exited = tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            if !f.node.agents.state.lock().await.exits.is_empty() {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await;
    assert!(exited.is_ok(), "fixture exit event must be settled");
    watcher.stop().await;
    {
        let mut state = f.node.agents.state.lock().await;
        assert_eq!(
            state
                .runs
                .get("exit-0000")
                .and_then(|r| r.workspace.as_ref()),
            Some(&workspace),
            "launched workspace record must survive the exit until its report gets 200"
        );
        // Storage is normally sorted; reversed input pins oldest-first report selection itself.
        state.exits = (0..1002)
            .rev()
            .map(|i| ended(&format!("exit-{i:04}"), i, (i == 0).then_some(&workspace)))
            .collect();
        state.commit(f.home.path(), &f.node.agents.changed)?;
    }
    let expected: Vec<_> = (0..1000).map(|i| format!("exit-{i:04}")).collect();
    let mut failed = Running::once(f.node.clone());
    let first = f.hub.next(&mut failed).await;
    assert_eq!(
        first
            .body
            .exits
            .iter()
            .map(|e| &e.run_id)
            .collect::<Vec<_>>(),
        expected.iter().collect::<Vec<_>>()
    );
    assert!(first.body.exits[0].launched);
    assert_eq!(
        first.body.exits[0].workspace.as_ref().unwrap().path,
        workspace.path.to_str().unwrap()
    );
    assert!(
        first
            .body
            .exits
            .iter()
            .skip(1)
            .all(|e| !e.launched && e.workspace.is_none())
    );
    let revision = first.body.revision;
    first.answer(StatusCode::INTERNAL_SERVER_ERROR, json!({}));
    let _ = failed.finish().await;
    let retained = persisted_state(f.home.path())?;
    assert_eq!(retained.exits.len(), 1002);
    assert!(retained.runs.contains_key("exit-0000"));

    let mut retry = Running::once(f.node.clone());
    let second = f.hub.next(&mut retry).await;
    assert!(second.body.revision > revision);
    assert_eq!(
        second
            .body
            .exits
            .iter()
            .map(|e| &e.run_id)
            .collect::<Vec<_>>(),
        expected.iter().collect::<Vec<_>>()
    );
    // An exit arriving after this report was built cannot be acknowledged by it.
    {
        let mut state = f.node.agents.state.lock().await;
        state.exits.push(ended("late-exit", 1002, None));
        state.commit(f.home.path(), &f.node.agents.changed)?;
    }
    second.answer(StatusCode::OK, json!({}));
    retry.finish().await?;
    let retained = persisted_state(f.home.path())?;
    assert_eq!(
        retained
            .exits
            .iter()
            .map(|e| e.exit.run_id.as_str())
            .collect::<std::collections::BTreeSet<_>>(),
        ["exit-1000", "exit-1001", "late-exit"]
            .into_iter()
            .collect()
    );
    assert!(
        !retained.runs.contains_key("exit-0000"),
        "acknowledged launched workspace record must leave"
    );
    let mut final_page = Running::once(f.node.clone());
    let third = f.hub.next(&mut final_page).await;
    assert_eq!(
        third
            .body
            .exits
            .iter()
            .map(|e| e.run_id.as_str())
            .collect::<Vec<_>>(),
        ["exit-1000", "exit-1001", "late-exit"]
    );
    third.answer(StatusCode::OK, json!({}));
    final_page.finish().await?;
    assert!(persisted_state(f.home.path())?.exits.is_empty());
    Ok(())
}

#[tokio::test]
async fn report_lists_accepted_and_live_agents() -> Result<()> {
    let inventory = vec![
        json!({"terminal_id":"terminal-live", "host_terminal_id":"host-live", "pgid":4242, "start_time":1.0}),
        json!({"terminal_id":"foreign-terminal", "host_terminal_id":"foreign-host", "pgid":9999, "start_time":1.0}),
    ];
    let mut f = Fixture::new(inventory).await?;
    let workspace = record(f.home.path(), "live");
    f.put_run("live", RunPhase::Launched, Some(workspace.clone()))
        .await?;
    f.put_run("absent-from-host", RunPhase::Launched, None)
        .await?;
    // Reopen persistent launched records while keeping the host alive.
    f.node = prepare_node(f.home.path())?;
    f.put_run("accepted", RunPhase::Accepted, None).await?;
    f.put_run(
        "preparing",
        RunPhase::Preparing,
        Some(record(f.home.path(), "preparing")),
    )
    .await?;
    let lists_before = f
        .host
        .requests()
        .iter()
        .filter(|r| r["method"] == "list")
        .count();
    for _ in 0..2 {
        let mut once = Running::once(f.node.clone());
        let report = f.hub.next(&mut once).await;
        assert_eq!(
            report.authorization.as_deref(),
            Some(format!("Bearer {KEY}").as_str())
        );
        assert_eq!(report.body.v, CONTRACT_VERSION);
        assert_eq!(report.body.host_epoch, 7);
        assert_eq!(report.body.accepted, ["accepted", "preparing"]);
        assert_eq!(
            report.body.agents.len(),
            1,
            "only owned runs present in the live inventory"
        );
        let agent = &report.body.agents[0];
        assert_eq!(agent.run_id, "live");
        assert_eq!(agent.terminal_id, "terminal-live");
        assert_eq!(agent.pid, 4242, "pid comes from the host's pgid");
        assert_eq!(agent.locator, "host-live");
        assert_eq!(
            agent.locator_key,
            format!("native:{}:host-live", fake_gterm::EPOCH)
        );
        let reported_workspace = agent.workspace.as_ref().expect("launched workspace");
        assert_eq!(reported_workspace.path, workspace.path.to_str().unwrap());
        assert_eq!(reported_workspace.branch_name, workspace.branch_name);
        assert_eq!(reported_workspace.base_branch.as_deref(), Some("main"));
        assert_eq!(serde_json::to_value(reported_workspace)?["mode"], "clone");
        report.answer(StatusCode::OK, json!({}));
        once.finish().await?;
    }
    let lists_after = f
        .host
        .requests()
        .iter()
        .filter(|r| r["method"] == "list")
        .count();
    assert!(
        lists_after >= lists_before + 2,
        "each report reads fresh host inventory"
    );
    Ok(())
}

#[tokio::test]
async fn report_tick_retries_removals() -> Result<()> {
    let mut f = Fixture::new(vec![]).await?;
    let workspace = record(f.home.path(), "pending-removal");
    fs::create_dir_all(&workspace.path)?;
    fs::write(workspace.path.join("marker"), "created by report test")?;
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_millis() as u64;
    {
        let mut state = f.node.agents.state.lock().await;
        state
            .removals
            .insert("pending-removal".into(), workspace.clone());
        state
            .cancelled
            .insert("expired".into(), now - 25 * 60 * 60 * 1000);
        state.cancelled.insert("recent".into(), now);
        state.commit(f.home.path(), &f.node.agents.changed)?;
    }
    let mut heartbeat = HeartbeatHost::new();
    reports::register_jobs(&mut heartbeat, f.node.clone())?;
    heartbeat.start()?;
    // Give the host task its initial poll so its timer is anchored before advancing.
    tokio::task::yield_now().await;
    advance(29).await;
    assert!(workspace.path.exists());
    assert!(
        persisted_state(f.home.path())?
            .cancelled
            .contains_key("expired")
    );
    advance(1).await;
    // The report may be queued for report_loop or sent directly by the job; allow both.
    let settled = tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            // The job notifies after each commit it makes and once more when it ends.
            let changed = f.node.agents.changed.notified();
            if let Ok(report) = f.hub.received.try_recv() {
                report.answer(StatusCode::OK, json!({}));
            }
            let state = persisted_state(f.home.path())?;
            if state.removals.is_empty() && !state.cancelled.contains_key("expired") {
                return Ok::<_, anyhow::Error>(state);
            }
            changed.await;
        }
    })
    .await;
    heartbeat.stop().await?;
    assert!(
        settled.is_ok(),
        "30-second report tick did not retry removals and prune cancellation records"
    );
    let state = settled.unwrap()?;
    assert!(!workspace.path.exists());
    assert!(state.cancelled.contains_key("recent"));
    Ok(())
}

#[tokio::test]
async fn node_registers_report_job() -> Result<()> {
    let home = tempfile::tempdir()?;
    let mut hub = Hub::start().await?;
    let first = std::net::TcpListener::bind("127.0.0.1:0")?;
    let second = std::net::TcpListener::bind("127.0.0.1:0")?;
    write_bootstrap(
        home.path(),
        &hub.url,
        first.local_addr()?.port(),
        second.local_addr()?.port(),
    )?;
    let host = fake_gterm::FakeGterm::start_with_inventory(home.path(), vec![json!({
        "terminal_id":"fixture-terminal", "host_terminal_id":"fixture-host-terminal", "pgid":4242, "start_time":1.0,
    })]).await?;
    let agents = NodeAgents::open(home.path())?;
    {
        let mut state = agents.state.lock().await;
        state.runs.insert(
            "survivor".into(),
            RunRecord {
                terminal_id: "fixture-terminal".into(),
                phase: RunPhase::Launched,
                accepted_at_ms: 1,
                workspace: None,
                host_terminal_id: Some("fixture-host-terminal".into()),
            },
        );
        state.commit(home.path(), &agents.changed)?;
    }
    drop((agents, first, second));
    let log = home.path().join("serve.log");
    let started = std::time::Instant::now();
    let mut child = tokio::process::Command::new(env!("CARGO_BIN_EXE_gdaemon"))
        .arg("serve")
        .env("GOBBY_HOME", home.path())
        .env_remove("GOBBY_PARENT_FD")
        .env_remove("GOBBY_SERVICE_LAUNCH")
        .env_remove("GOBBY_SERVICE_NONCE")
        .kill_on_drop(true)
        .stderr(fs::File::create(&log)?)
        .spawn()?;
    let mut saw_report = false;
    let mut saw_subscription = false;
    // Observe the actual serve entrypoint, not a source-text registration assertion.
    let startup = tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            tokio::select! {
                report = hub.received.recv() => {
                    let report = report.context("hub open")?;
                    saw_report = true;
                    report.answer(StatusCode::OK, json!({}));
                }
                () = host.wait_for_subscription(), if !saw_subscription => {
                    saw_subscription = true;
                }
                status = child.wait() => {
                    anyhow::bail!(
                        "isolated node exited {status:?}: {}",
                        fs::read_to_string(&log)?
                    );
                }
            }
            if saw_report && saw_subscription {
                break;
            }
        }
        Ok::<_, anyhow::Error>(())
    })
    .await;
    assert!(
        saw_report,
        "serve never started the report loop; log={}",
        fs::read_to_string(&log)?
    );
    assert!(
        saw_subscription,
        "serve never started watch_exits; requests={:?}",
        host.requests()
    );
    startup??;
    host.terminal_exited()?;
    let exit = tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let report = hub.received.recv().await.expect("hub open");
            let found = report
                .body
                .exits
                .iter()
                .any(|e| e.run_id == "survivor" && e.launched && e.exit_code == Some(0));
            report.answer(StatusCode::OK, json!({}));
            if found {
                break;
            }
        }
    })
    .await;
    assert!(
        exit.is_ok(),
        "exit watcher did not publish the surviving agent's exit"
    );
    // A second report without a change proves the job was actually registered in serve.
    let tick = tokio::time::timeout(Duration::from_secs(35), hub.received.recv()).await;
    assert!(
        tick.is_ok(),
        "serve failed to register the 30-second report job"
    );
    assert!(
        started.elapsed() >= Duration::from_secs(28),
        "an immediate follow-up report cannot stand in for the periodic job"
    );
    tick?.context("hub open")?.answer(StatusCode::OK, json!({}));
    child.kill().await?;
    child.wait().await?;
    Ok(())
}

#[tokio::test]
async fn non_200_success_keeps_exits_until_200() -> Result<()> {
    let mut f = Fixture::new(Vec::new()).await?;
    let workspace = record(f.home.path(), "exit-0000");
    {
        let mut state = f.node.agents.state.lock().await;
        state.runs.insert(
            "exit-0000".into(),
            RunRecord {
                terminal_id: "fixture-terminal".into(),
                phase: RunPhase::Exited,
                accepted_at_ms: 0,
                host_terminal_id: Some("fixture-host-terminal".into()),
                workspace: Some(workspace.clone()),
            },
        );
        state.exits.push(ended("exit-0000", 1, Some(&workspace)));
        state.commit(f.home.path(), &f.node.agents.changed)?;
    }
    for status in [StatusCode::ACCEPTED, StatusCode::NO_CONTENT] {
        let mut report = Running::once(f.node.clone());
        let exchange = f.hub.next(&mut report).await;
        assert_eq!(exchange.body.exits.len(), 1);
        exchange.answer(status, json!({}));
        assert!(
            report.finish().await.is_err(),
            "{status} is not an acknowledgement"
        );
        let retained = persisted_state(f.home.path())?;
        assert_eq!(retained.exits.len(), 1, "{status} must keep the exit");
        assert!(
            retained.runs.contains_key("exit-0000"),
            "{status} must keep the launched workspace"
        );
    }
    let mut report = Running::once(f.node.clone());
    let exchange = f.hub.next(&mut report).await;
    assert_eq!(exchange.body.exits.len(), 1);
    exchange.answer(StatusCode::OK, json!({}));
    report.finish().await?;
    let retained = persisted_state(f.home.path())?;
    assert!(retained.exits.is_empty());
    assert!(!retained.runs.contains_key("exit-0000"));
    assert_eq!(retained.removals.get("exit-0000"), Some(&workspace));
    Ok(())
}

#[tokio::test]
async fn unlaunched_recovery_reports_no_workspace() -> Result<()> {
    let mut f = Fixture::new(Vec::new()).await?;
    let workspace = record(f.home.path(), "prepare-0000");
    f.put_run("prepare-0000", RunPhase::Preparing, Some(workspace.clone()))
        .await?;
    {
        let mut state = f.node.agents.state.lock().await;
        state.recover_after_restart();
        state.commit(f.home.path(), &f.node.agents.changed)?;
    }
    let mut failed = Running::once(f.node.clone());
    let first = f.hub.next(&mut failed).await;
    assert_eq!(first.body.exits.len(), 1);
    assert!(!first.body.exits[0].launched);
    assert!(
        first.body.exits[0].workspace.is_none(),
        "a launched:false exit names no workspace"
    );
    first.answer(StatusCode::INTERNAL_SERVER_ERROR, json!({}));
    let _ = failed.finish().await;
    assert_eq!(persisted_state(f.home.path())?.exits.len(), 1);

    let mut retry = Running::once(f.node.clone());
    let second = f.hub.next(&mut retry).await;
    assert_eq!(second.body.exits.len(), 1);
    second.answer(StatusCode::OK, json!({}));
    retry.finish().await?;
    let retained = persisted_state(f.home.path())?;
    assert!(retained.exits.is_empty());
    assert_eq!(
        retained.removals.get("prepare-0000"),
        Some(&workspace),
        "the preparation record survives the acknowledged exit"
    );
    Ok(())
}

#[tokio::test]
async fn pending_exit_recovery_keeps_launched_workspace() -> Result<()> {
    let mut f = Fixture::new(Vec::new()).await?;
    let workspace = record(f.home.path(), "prepare-0001");
    f.put_run("prepare-0001", RunPhase::Preparing, Some(workspace.clone()))
        .await?;
    {
        let mut state = f.node.agents.state.lock().await;
        state.pending_exits.insert("prepare-0001".into(), Some(0));
        state.recover_after_restart();
        assert!(
            state.removals.is_empty(),
            "the launched workspace waits for its exit's acknowledgement"
        );
        state.commit(f.home.path(), &f.node.agents.changed)?;
    }
    let mut failed = Running::once(f.node.clone());
    let first = f.hub.next(&mut failed).await;
    assert_eq!(first.body.exits.len(), 1);
    assert!(first.body.exits[0].launched);
    assert_eq!(first.body.exits[0].exit_code, Some(0));
    assert_eq!(
        first.body.exits[0].workspace.as_ref().unwrap().path,
        workspace.path.to_str().unwrap()
    );
    first.answer(StatusCode::INTERNAL_SERVER_ERROR, json!({}));
    let _ = failed.finish().await;
    let retained = persisted_state(f.home.path())?;
    assert_eq!(
        retained.runs.get("prepare-0001").map(|run| run.phase),
        Some(RunPhase::Exited)
    );
    assert!(retained.removals.is_empty());

    let mut retry = Running::once(f.node.clone());
    let second = f.hub.next(&mut retry).await;
    assert_eq!(second.body.exits.len(), 1);
    second.answer(StatusCode::OK, json!({}));
    retry.finish().await?;
    let retained = persisted_state(f.home.path())?;
    assert!(retained.exits.is_empty());
    assert!(!retained.runs.contains_key("prepare-0001"));
    assert_eq!(retained.removals.get("prepare-0001"), Some(&workspace));
    Ok(())
}
