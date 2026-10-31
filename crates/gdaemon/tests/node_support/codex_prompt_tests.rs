//! #24379: isolated prompt delivery with explicit virtual-time boundaries.
use super::*;
use axum::http::{Method, StatusCode};
use base64::{Engine as _, engine::general_purpose::STANDARD as BASE64};
use gobby_daemon::{
    front_door::relay::HubRelay,
    node_commands::CONTRACT_VERSION,
    nodes::{
        agents::{
            codex_prompt::deliver,
            commands::{admit, dispatch},
        },
        hooks::ledger::Ledger,
    },
    state::NodeServices,
};
use std::{os::unix::fs::PermissionsExt, time::Duration};
use tokio::task::JoinHandle;

use super::super::checkout_tests::{fake_hub, node_home};
use node_home::{NODE_KEY, NodeHome};

const RUN: &str = "codex-prompt-run";
const TERMINAL: &str = "codex-terminal";
const HOST: &str = "host-codex-terminal";
const FAILURE: &str = "/api/nodes/runs/prompt-delivery-failure";
const PROMPT: &str = "Inspect the startup trace.\nKeep literal $HOME and › in the prompt.";

struct PromptFixture {
    home: NodeHome,
    host: fake_gterm::FakeGterm,
    hub: fake_hub::FakeHub,
}

impl Drop for PromptFixture {
    fn drop(&mut self) {
        self.hub.disconnect();
    }
}

impl PromptFixture {
    async fn new() -> Result<Self> {
        let hub = fake_hub::FakeHub::start().await?;
        let home = NodeHome::new(&hub)?;
        let host = fake_gterm::FakeGterm::start_with_inventory(
            home.home.path(),
            vec![json!({"terminal_id":TERMINAL,"host_terminal_id":HOST})],
        )
        .await?;
        let agents = &home.node.agents;
        let mut state = agents.state.lock().await;
        state.runs.insert(
            RUN.into(),
            RunRecord {
                terminal_id: TERMINAL.into(),
                phase: RunPhase::Launched,
                accepted_at_ms: 1,
                workspace: None,
                host_terminal_id: Some(HOST.into()),
            },
        );
        state.commit(&agents.home, &agents.changed)?;
        drop(state);
        Ok(Self { home, host, hub })
    }

    fn start(&self, prompt: &str) -> JoinHandle<()> {
        tokio::spawn(deliver(
            Arc::clone(&self.home.node),
            RUN.into(),
            prompt.into(),
        ))
    }

    fn requests(&self, method: &str) -> Vec<Value> {
        self.host
            .requests()
            .into_iter()
            .filter(|r| r["method"] == method)
            .collect()
    }

    async fn assert_no_signal(&mut self) -> Result<()> {
        assert!(
            self.requests("kill").is_empty(),
            "the hub owns run termination"
        );
        assert!(
            self.host.alive()?,
            "delivery must never signal the host process"
        );
        let state = self.home.node.agents.state.lock().await;
        assert!(
            state.runs.contains_key(RUN),
            "node must retain the run for hub cancellation"
        );
        assert!(!state.exits.iter().any(|exit| exit.exit.run_id == RUN));
        Ok(())
    }
}

// Keep the runtime runnable while the OS polls the isolated sockets. Thus paused
// time moves only at an explicit advance(), never because a request waits for I/O.
async fn pump() {
    let until = std::time::Instant::now() + Duration::from_millis(50);
    while std::time::Instant::now() < until {
        tokio::task::yield_now().await;
    }
}

async fn advance(millis: u64) {
    tokio::time::advance(Duration::from_millis(millis)).await;
    pump().await;
}

#[tokio::test(start_paused = true)]
async fn codex_prompt_typed_after_composer() -> Result<()> {
    let mut fixture = PromptFixture::new().await?;
    fixture.host.set_pane("MCP servers are starting");
    let delivery = fixture.start(PROMPT);
    pump().await;
    assert_eq!(
        fixture.requests("snapshot").len(),
        1,
        "delivery must probe the composer"
    );
    assert!(fixture.requests("write").is_empty());
    advance(999).await;
    assert_eq!(
        fixture.requests("snapshot").len(),
        1,
        "polls are one second apart"
    );
    fixture.host.set_pane("Ready\n› ");
    advance(1).await;
    assert_eq!(fixture.requests("snapshot").len(), 2);
    assert!(
        fixture.requests("write").is_empty(),
        "marker still needs one settle second"
    );
    advance(999).await;
    assert!(fixture.requests("write").is_empty());
    advance(1).await;
    let writes = fixture.requests("write");
    assert_eq!(writes.len(), 1, "text must land after the settle second");
    assert_eq!(writes[0]["host_terminal_id"], HOST);
    assert_eq!(writes[0]["kind"], "text");
    assert_eq!(writes[0]["encoding"], "utf8-b64");
    assert_eq!(writes[0]["data"], BASE64.encode(PROMPT));
    assert_eq!(writes[0]["submit"], false);
    advance(2999).await;
    assert_eq!(
        fixture.requests("write").len(),
        1,
        "Enter must wait three full seconds"
    );
    advance(1).await;
    let writes = fixture.requests("write");
    assert_eq!(writes.len(), 2);
    assert_eq!(writes[1]["kind"], "key");
    assert_eq!(writes[1]["data"], BASE64.encode("enter"));
    assert!(delivery.is_finished());
    delivery.await?;
    for request in fixture.requests("snapshot") {
        assert_eq!(request["host_terminal_id"], HOST);
        assert_eq!(request["mode"], "text");
        assert_eq!(request["max_lines"], 40);
        assert_eq!(request["max_bytes"], 262_144);
    }
    assert!(
        !fixture.requests("list").is_empty(),
        "check liveness between polls"
    );
    assert!(fixture.hub.requests().is_empty());
    fixture.assert_no_signal().await
}

#[tokio::test(start_paused = true)]
async fn codex_prompt_failures_post_to_hub() -> Result<()> {
    for (cause, code) in [
        ("deadline", "codex_composer_not_ready"),
        ("exited", "codex_terminal_exited_before_composer"),
        ("refused", "codex_prompt_delivery_failed"),
    ] {
        for status in [
            StatusCode::OK,
            StatusCode::CONFLICT,
            StatusCode::SERVICE_UNAVAILABLE,
        ] {
            let mut fixture = PromptFixture::new().await?;
            // Deliberately long and secret-looking: redaction/truncation belongs to the hub.
            let pane = format!("TOKEN=raw-node-pane-{}", "x".repeat(1100));
            fixture.host.set_pane(&pane);
            fixture
                .hub
                .script(FAILURE, vec![(status, json!({"transitioned":true}))]);
            let delivery = fixture.start(PROMPT);
            pump().await;
            match cause {
                "deadline" => {
                    fixture.host.set_pane("");
                    for _ in 0..104 {
                        advance(1000).await;
                    }
                    assert!(
                        fixture.hub.requests().is_empty(),
                        "105-second budget must include retry window"
                    );
                    advance(1000).await;
                }
                "exited" => {
                    fixture.host.set_pane("");
                    fixture.host.clear_inventory();
                    advance(1000).await;
                }
                "refused" => {
                    fixture.host.refuse_writes();
                    fixture.host.set_pane("› ");
                    advance(1000).await;
                    advance(1000).await;
                }
                _ => unreachable!(),
            }
            let requests = fixture.hub.requests();
            assert_eq!(
                requests.len(),
                1,
                "{cause}/{status}: must post one failure, got {requests:?}"
            );
            let request = &requests[0];
            assert_eq!(request.method, Method::POST);
            assert_eq!(request.path, FAILURE);
            assert_eq!(
                request.headers["authorization"],
                format!("Bearer {NODE_KEY}")
            );
            assert_eq!(request.body["v"], CONTRACT_VERSION);
            assert_eq!(request.body["run_id"], RUN);
            assert_eq!(request.body["code"], code);
            let expected_pane = if cause == "refused" { "› " } else { &pane };
            assert_eq!(
                request.body["pane"], expected_pane,
                "send raw last non-empty pane"
            );
            assert!(
                request.body["detail"]
                    .as_str()
                    .is_some_and(|s| !s.is_empty())
            );
            if cause == "deadline" {
                let detail = request.body["detail"].as_str().context("detail")?;
                for counter in ["captures=", "snapshot_errors=", "liveness_errors="] {
                    assert!(detail.contains(counter), "missing {counter} in {detail}");
                }
            }
            pump().await;
            assert!(delivery.is_finished(), "failure response must end delivery");
            delivery.await?;
            fixture.assert_no_signal().await?;
        }
    }
    // A hub transport failure also leaves termination to the hub/initialization timeout.
    let mut fixture = PromptFixture::new().await?;
    fixture.host.set_pane("still starting");
    fixture.hub.disconnect();
    let delivery = fixture.start(PROMPT);
    pump().await;
    fixture.host.clear_inventory();
    advance(1000).await;
    pump().await;
    assert!(delivery.is_finished());
    delivery.await?;
    fixture.assert_no_signal().await
}

#[tokio::test(start_paused = true)]
async fn codex_prompt_skips() -> Result<()> {
    let mut fixture = PromptFixture::new().await?;
    fixture.host.set_pane("› ");
    fixture.host.disconnect_next_write();
    let delivery = fixture.start(PROMPT);
    pump().await;
    advance(1000).await;
    assert_eq!(
        fixture.requests("write").len(),
        1,
        "exercise a write sent before disconnect"
    );
    advance(5000).await;
    assert!(
        delivery.is_finished(),
        "indeterminate write must not be retried"
    );
    delivery.await?;
    assert_eq!(fixture.requests("write").len(), 1);
    assert!(
        fixture.hub.requests().is_empty(),
        "indeterminate write only logs"
    );
    fixture.assert_no_signal().await?;

    let mut fixture = PromptFixture::new().await?;
    fixture.host.set_pane("loading");
    let delivery = fixture.start(PROMPT);
    pump().await;
    assert_eq!(fixture.requests("snapshot").len(), 1);
    {
        let agents = &fixture.home.node.agents;
        let mut state = agents.state.lock().await;
        state.cancelled.insert(RUN.into(), 1);
        state.commit(&agents.home, &agents.changed)?;
    }
    fixture.host.set_pane("› ");
    advance(105_000).await;
    assert!(delivery.is_finished());
    delivery.await?;
    assert!(
        fixture.requests("write").is_empty(),
        "cancel record stops delivery"
    );
    assert!(fixture.hub.requests().is_empty());
    fixture.assert_no_signal().await?;

    assert_other_provider_skips().await?;

    Ok(())
}

/// Freezes the clock, then runs past the settle and enter delays. A delivery that should not
/// exist has time to type, so the caller asserts on the host's record afterwards.
async fn elapse_delivery_window() {
    tokio::time::pause();
    advance_secs(4).await;
}

/// Advances the paused clock one whole probe cadence at a time.
async fn advance_secs(seconds: u64) {
    for _ in 0..seconds {
        advance(1000).await;
    }
}

#[tokio::test(start_paused = true)]
async fn codex_prompt_held_inventory_fails_at_deadline() -> Result<()> {
    let mut fixture = PromptFixture::new().await?;
    fixture.host.set_pane("loading");
    fixture.hub.script(
        FAILURE,
        vec![(StatusCode::OK, json!({"transitioned":true}))],
    );
    let delivery = fixture.start(PROMPT);
    pump().await;
    advance_secs(103).await;
    // The liveness probe begun at 104 s is held. The budget still ends at 105 s.
    fixture.host.pause_next_list_reply();
    advance(1000).await;
    advance(1000).await;
    let requests = fixture.hub.requests();
    assert_eq!(requests.len(), 1, "the budget ends at 105 s: {requests:?}");
    assert_eq!(requests[0].body["code"], "codex_composer_not_ready");
    fixture.host.release_list_reply();
    pump().await;
    assert!(delivery.is_finished(), "the budget must end delivery");
    delivery.await?;
    assert!(fixture.requests("write").is_empty());
    fixture.assert_no_signal().await
}

#[tokio::test(start_paused = true)]
async fn codex_prompt_late_inventory_preserves_deadline_failure() -> Result<()> {
    use std::{future::Future, task::Context};

    let mut fixture = PromptFixture::new().await?;
    fixture.host.set_pane("last startup pane");
    fixture.hub.script(
        FAILURE,
        vec![(StatusCode::OK, json!({"transitioned":true}))],
    );
    // Poll explicitly so delivery can be descheduled while its reply and
    // deadline both become ready. The host and hub keep running normally.
    let mut delivery = Box::pin(deliver(
        Arc::clone(&fixture.home.node),
        RUN.into(),
        PROMPT.into(),
    ));
    let mut context = Context::from_waker(std::task::Waker::noop());
    for _ in 0..20 {
        assert!(delivery.as_mut().poll(&mut context).is_pending());
        pump().await;
        if fixture.requests("list").len() == 1 {
            break;
        }
    }
    assert_eq!(fixture.requests("list").len(), 1);
    // Finish the first inventory, then start a missing-terminal probe at 104 s.
    assert!(delivery.as_mut().poll(&mut context).is_pending());
    fixture.host.clear_inventory();
    fixture.host.pause_next_list_reply();
    tokio::time::advance(Duration::from_secs(104)).await;
    for _ in 0..20 {
        assert!(delivery.as_mut().poll(&mut context).is_pending());
        pump().await;
        if fixture.requests("list").len() == 2 {
            break;
        }
    }
    assert_eq!(fixture.requests("list").len(), 2);
    fixture.host.wait_for_list_snapshot().await;
    fixture.host.release_list_reply();
    pump().await;
    tokio::time::advance(Duration::from_secs(1)).await;
    let mut finished = false;
    for _ in 0..20 {
        if delivery.as_mut().poll(&mut context).is_ready() {
            finished = true;
            break;
        }
        pump().await;
    }
    assert!(finished, "the failure report must finish delivery");

    let requests = fixture.hub.requests();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0].body["code"], "codex_composer_not_ready");
    assert_eq!(requests[0].body["pane"], "last startup pane");
    let detail = requests[0].body["detail"].as_str().context("detail")?;
    for counter in ["captures=2", "snapshot_errors=0", "liveness_errors=0"] {
        assert!(detail.contains(counter), "missing {counter} in {detail}");
    }
    assert!(fixture.requests("write").is_empty());
    fixture.assert_no_signal().await
}

#[tokio::test(start_paused = true)]
async fn codex_prompt_held_snapshot_fails_at_deadline() -> Result<()> {
    let mut fixture = PromptFixture::new().await?;
    fixture.host.set_pane("loading");
    fixture.hub.script(
        FAILURE,
        vec![(StatusCode::OK, json!({"transitioned":true}))],
    );
    let delivery = fixture.start(PROMPT);
    pump().await;
    advance_secs(103).await;
    // The snapshot begun at 104 s is held with the composer marker in its reply. The budget still
    // ends at 105 s, and the late marker must not start a write.
    fixture.host.pause_next_snapshot_reply();
    fixture.host.set_pane("›");
    advance(1000).await;
    advance(1000).await;
    let requests = fixture.hub.requests();
    assert_eq!(requests.len(), 1, "the budget ends at 105 s: {requests:?}");
    assert_eq!(requests[0].body["code"], "codex_composer_not_ready");
    fixture.host.release_snapshot_reply();
    pump().await;
    assert!(delivery.is_finished(), "the budget must end delivery");
    delivery.await?;
    assert!(fixture.requests("write").is_empty());
    fixture.assert_no_signal().await
}

/// Wires a launch's agents to node services as `NodeServices::prepare` does. The agents hold only
/// a weak reference, so the caller keeps the returned node alive for the test.
fn wire_services(launch: &LaunchFixture) -> Result<Arc<NodeServices>> {
    let node = Arc::new(NodeServices {
        bootstrap_path: launch.agents.home.join("bootstrap.yaml"),
        relay: HubRelay::new("http://127.0.0.1:9", None)?,
        hook_ledger: Ledger::new(launch.agents.home.join("hooks/inbox")),
        agents: Arc::clone(&launch.agents),
        launch: Arc::new(launch.context.clone()),
    });
    launch
        .agents
        .services
        .set(Arc::downgrade(&node))
        .map_err(|_| anyhow::anyhow!("node agents already have services"))?;
    Ok(node)
}

/// Admits one frame the way `serve_channel` does: admit, run, reply, then release the run.
async fn admit_and_deliver(launch: &LaunchFixture, frame: Value) -> Value {
    let context = Arc::new(launch.context.clone());
    let dispatched = admit(&launch.agents, &context, frame).await.run().await;
    let reply = dispatched.reply.clone();
    dispatched.delivered();
    reply
}

fn command(name: &str, request_id: &str, payload: Value) -> Value {
    json!({"v":CONTRACT_VERSION,"type":"command","request_id":request_id,
        "command":name,"payload":payload})
}

fn no_workspace() -> SpawnWorkspace {
    SpawnWorkspace {
        mode: SpawnMode::None,
        path: None,
        branch_name: None,
        base_branch: None,
    }
}

async fn clear_runs(launch: &LaunchFixture) -> Result<()> {
    let mut state = launch.agents.state.lock().await;
    state.runs.clear();
    state.commit(&launch.agents.home, &launch.agents.changed)?;
    Ok(())
}

/// A managed Codex launch of the corpus case, with a fake `codex` on the launch's PATH.
async fn codex_launch() -> Result<LaunchFixture> {
    let mut launch = LaunchFixture::build(false).await?;
    let base = launch.payload.clone();
    let mut payload = CommandCase::new("codex-fresh")?.payload;
    payload.run_id = base.run_id;
    payload.source_root = base.source_root;
    payload.project_id = base.project_id;
    payload.session_id = base.session_id;
    payload.sandbox = base.sandbox;
    payload.extra_env.clear();
    payload.workspace = no_workspace();
    payload.prompt = PROMPT.into();
    launch.payload = payload;
    clear_runs(&launch).await?;
    let bin = launch.tree.path("bin");
    std::fs::create_dir_all(&bin)?;
    let codex = bin.join("codex");
    std::fs::write(&codex, "#!/bin/sh\nexit 0\n")?;
    std::fs::set_permissions(&codex, std::fs::Permissions::from_mode(0o755))?;
    Ok(launch)
}

async fn wait_launched(launch: &LaunchFixture) -> Result<()> {
    tokio::time::timeout(Duration::from_secs(60), async {
        loop {
            let launched = launch
                .agents
                .state
                .lock()
                .await
                .runs
                .get(&launch.payload.run_id)
                .is_some_and(|run| run.phase == RunPhase::Launched);
            if launched {
                return;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .context("run did not reach Launched")
}

async fn wait_for_requests(host: &fake_gterm::FakeGterm, method: &str, count: usize) -> Result<()> {
    tokio::time::timeout(Duration::from_secs(60), async {
        while host
            .requests()
            .iter()
            .filter(|r| r["method"] == method)
            .count()
            < count
        {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .with_context(|| format!("{count} {method} requests"))
}

#[tokio::test]
async fn codex_prompt_follows_production_launch() -> Result<()> {
    let launch = codex_launch().await?;
    let _node = wire_services(&launch)?;
    let reply = admit_and_deliver(
        &launch,
        command("agent.spawn", "codex-spawn", json!(launch.payload)),
    )
    .await;
    assert_eq!(reply["ok"], true, "{reply}");
    wait_launched(&launch).await?;
    launch.host.set_pane("› ");
    wait_for_requests(&launch.host, "write", 2).await?;
    let requests = launch.host.requests();
    let position = |method: &str| requests.iter().position(|r| r["method"] == method);
    let commit = position("spawn_commit").context("spawn_commit")?;
    let first_probe = position("snapshot").context("snapshot")?;
    assert!(
        commit < first_probe,
        "the composer is probed only after launch commits"
    );
    let writes: Vec<&Value> = requests.iter().filter(|r| r["method"] == "write").collect();
    assert_eq!(writes.len(), 2);
    assert_eq!(writes[0]["kind"], "text");
    assert_eq!(writes[0]["data"], BASE64.encode(PROMPT));
    assert_eq!(writes[0]["submit"], false);
    assert_eq!(writes[1]["kind"], "key");
    assert_eq!(writes[1]["data"], BASE64.encode("enter"));
    let replay = admit_and_deliver(
        &launch,
        command("agent.spawn", "codex-spawn-replay", json!(launch.payload)),
    )
    .await;
    assert_eq!(replay["ok"], true, "{replay}");
    elapse_delivery_window().await;
    let typed = launch
        .host
        .requests()
        .iter()
        .filter(|r| r["method"] == "write")
        .count();
    assert_eq!(typed, 2, "a replayed spawn must not type the prompt again");
    Ok(())
}

#[tokio::test]
async fn codex_prompt_stops_on_production_cancel() -> Result<()> {
    let launch = codex_launch().await?;
    let _node = wire_services(&launch)?;
    launch.host.set_pane("loading");
    let reply = admit_and_deliver(
        &launch,
        command("agent.spawn", "codex-spawn", json!(launch.payload)),
    )
    .await;
    assert_eq!(reply["ok"], true, "{reply}");
    wait_launched(&launch).await?;
    wait_for_requests(&launch.host, "snapshot", 1).await?;
    launch.host.set_pane("› ");
    // The next probe answers with the marker. The cancel then lands inside the settle delay,
    // so only the write guard can stop the text from being typed.
    wait_for_requests(&launch.host, "snapshot", 2).await?;
    let cancel = command(
        "agent.cancel",
        "codex-cancel",
        json!({"run_id": launch.payload.run_id, "signal": "TERM"}),
    );
    let reply = admit_and_deliver(&launch, cancel).await;
    assert_eq!(reply["ok"], true, "{reply}");
    elapse_delivery_window().await;
    assert!(
        launch
            .host
            .requests()
            .iter()
            .all(|r| r["method"] != "write"),
        "a cancelled run must not be typed into"
    );
    Ok(())
}

#[tokio::test]
async fn codex_prompt_skips_other_providers_in_production() -> Result<()> {
    let mut launch = LaunchFixture::build(false).await?;
    launch.payload.workspace = no_workspace();
    clear_runs(&launch).await?;
    let _node = wire_services(&launch)?;
    let reply = admit_and_deliver(
        &launch,
        command("agent.spawn", "claude-spawn", json!(launch.payload)),
    )
    .await;
    assert_eq!(reply["ok"], true, "{reply}");
    wait_launched(&launch).await?;
    elapse_delivery_window().await;
    assert!(
        launch
            .host
            .requests()
            .iter()
            .all(|r| r["method"] != "snapshot" && r["method"] != "write"),
        "another provider must never start Codex delivery"
    );
    Ok(())
}

async fn assert_other_provider_skips() -> Result<()> {
    // Exercise the actual launch dispatcher for a non-Codex provider.
    tokio::time::resume();
    let mut launch = LaunchFixture::build(false).await?;
    launch.payload.workspace = SpawnWorkspace {
        mode: SpawnMode::None,
        path: None,
        branch_name: None,
        base_branch: None,
    };
    {
        let mut state = launch.agents.state.lock().await;
        state.runs.clear();
        state.commit(&launch.agents.home, &launch.agents.changed)?;
    }
    let reply = dispatch(
        Arc::clone(&launch.agents),
        Arc::new(launch.context.clone()),
        json!({"v":CONTRACT_VERSION,"type":"command","request_id":"claude-skip",
            "command":"agent.spawn","payload":launch.payload}),
    )
    .await;
    assert_eq!(
        reply["ok"], true,
        "non-Codex launch must be admitted: {reply}"
    );
    // Bound the real preparation independently from virtual delivery time.
    tokio::time::timeout(Duration::from_secs(60), async {
        loop {
            if launch
                .agents
                .state
                .lock()
                .await
                .runs
                .get(SRT_RUN_ID)
                .is_some_and(|run| run.phase == RunPhase::Launched)
            {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await?;
    tokio::time::pause();
    advance(105_000).await;
    assert!(
        !launch
            .host
            .requests()
            .iter()
            .any(|r| r["method"] == "snapshot" || r["method"] == "write"),
        "another provider must never start Codex delivery"
    );
    Ok(())
}
