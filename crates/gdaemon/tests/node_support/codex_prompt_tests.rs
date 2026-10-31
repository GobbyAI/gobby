//! #24379: isolated prompt delivery with explicit virtual-time boundaries.
use super::*;
use axum::http::{Method, StatusCode};
use base64::{Engine as _, engine::general_purpose::STANDARD as BASE64};
use gobby_daemon::{
    node_commands::CONTRACT_VERSION,
    nodes::agents::{codex_prompt::deliver, commands::dispatch},
};
use std::time::Duration;
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
    let until = std::time::Instant::now() + Duration::from_millis(5);
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
    assert_other_provider_skips().await?;
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
    tokio::time::timeout(Duration::from_secs(10), async {
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
