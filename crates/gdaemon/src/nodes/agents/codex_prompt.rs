//! Codex prompt delivery. Codex cancels MCP startup when a first turn begins, so the node types
//! the prompt once the composer renders instead of putting it on argv.
use super::NodeAgents;
use crate::{
    node_commands::{CONTRACT_VERSION, SpawnPayload},
    nodes::hooks::drain,
    state::NodeServices,
};
use anyhow::{Context, bail};
use gobby_terminals::control::{ControlError, Inventory, WriteKind, WriteRequest};
use serde_json::json;
use std::{
    io,
    sync::{Arc, Weak},
    time::Duration,
};
use tokio::time::{Instant, sleep, sleep_until, timeout, timeout_at};

const COMPOSER: &str = "›";
const POLL: Duration = Duration::from_secs(1);
const SETTLE: Duration = Duration::from_secs(1);
const ENTER_DELAY: Duration = Duration::from_secs(3);
/// Budget for the composer to render before delivery gives up.
const DEADLINE: Duration = Duration::from_secs(105);
const PROBE_DEADLINE: Duration = Duration::from_secs(5);
const REPORT_DEADLINE: Duration = Duration::from_secs(10);
const FAILURE: &str = "/api/nodes/runs/prompt-delivery-failure";
const PANE_LINES: usize = 40;
const PANE_BYTES: usize = 262_144;

/// How delivery ended before the prompt was fully typed.
enum Stop {
    /// A cancel record ended delivery. The hub owns the run's outcome.
    Cancelled,
    /// A write may have landed, so the prompt is not retried and no failure is posted.
    Indeterminate(String),
    Failed {
        code: &'static str,
        detail: String,
    },
}

#[derive(Default)]
struct Probe {
    captures: u64,
    snapshot_errors: u64,
    liveness_errors: u64,
    /// The last snapshot with text. A failure report sends it raw.
    pane: String,
}

/// Types `prompt` into a launched Codex run once its composer renders, then submits it.
pub async fn deliver(node: Arc<NodeServices>, run_id: String, prompt: String) {
    let run = {
        let state = node.agents.state.lock().await;
        state.runs.get(&run_id).cloned()
    };
    let Some(run) = run else { return };
    let Some(host_terminal_id) = run.host_terminal_id.clone() else {
        return;
    };
    let mut probe = Probe::default();
    let Err(stop) = type_prompt(
        &node,
        &run_id,
        (&run.terminal_id, &host_terminal_id),
        &prompt,
        &mut probe,
    )
    .await
    else {
        return;
    };
    match stop {
        Stop::Cancelled => {}
        Stop::Indeterminate(error) => {
            eprintln!("node codex prompt {run_id}: write outcome unknown, not retried: {error}");
        }
        Stop::Failed { code, detail } => {
            if cancelled(&node, &run_id).await {
                return;
            }
            if let Err(error) = report(&node, &run_id, code, &detail, &probe.pane).await {
                eprintln!("node codex prompt {run_id}: failure report not delivered: {error:#}");
            }
        }
    }
}

/// Starts delivery for a launched run whose Codex prompt is typed rather than passed on argv.
pub fn schedule(agents: &NodeAgents, payload: &SpawnPayload) {
    if payload.provider != "codex" || payload.prompt.is_empty() {
        return;
    }
    let Some(node) = agents.services.get().and_then(Weak::upgrade) else {
        return;
    };
    let run_id = payload.run_id.clone();
    let delivery = tokio::spawn(deliver(node, run_id.clone(), payload.prompt.clone()));
    // Watches the delivery task so a panic or runtime abort is logged rather than dropped.
    tokio::spawn(async move {
        if let Err(error) = delivery.await {
            eprintln!("node codex prompt {run_id}: delivery ended abnormally: {error}");
        }
    });
}

async fn type_prompt(
    node: &NodeServices,
    run_id: &str,
    (terminal_id, host_terminal_id): (&str, &str),
    prompt: &str,
    probe: &mut Probe,
) -> Result<(), Stop> {
    wait_for_composer(node, run_id, terminal_id, host_terminal_id, probe).await?;
    sleep(SETTLE).await;
    let text = WriteRequest {
        host_terminal_id: host_terminal_id.to_owned(),
        kind: WriteKind::Text,
        data: prompt.as_bytes().to_vec(),
        submit: Some(false),
    };
    write(node, run_id, &text).await?;
    sleep(ENTER_DELAY).await;
    let enter = WriteRequest {
        host_terminal_id: host_terminal_id.to_owned(),
        kind: WriteKind::Key,
        data: b"enter".to_vec(),
        submit: None,
    };
    write(node, run_id, &enter).await
}

async fn wait_for_composer(
    node: &NodeServices,
    run_id: &str,
    terminal_id: &str,
    host_terminal_id: &str,
    probe: &mut Probe,
) -> Result<(), Stop> {
    let deadline = Instant::now() + DEADLINE;
    // Polls stay on one cadence from the start. A late probe re-anchors the cadence instead of
    // replaying every missed slot.
    let mut slot = Instant::now();
    loop {
        if cancelled(node, run_id).await {
            return Err(Stop::Cancelled);
        }
        if Instant::now() >= deadline {
            let detail = format!(
                "captures={} snapshot_errors={} liveness_errors={}",
                probe.captures, probe.snapshot_errors, probe.liveness_errors
            );
            return Err(Stop::Failed {
                code: "codex_composer_not_ready",
                detail,
            });
        }
        match snapshot(node, host_terminal_id, probe_limit(deadline)).await {
            Ok(text) => {
                probe.captures += 1;
                // A marker that arrives after the budget is not accepted.
                let ready = text.contains(COMPOSER) && Instant::now() < deadline;
                if !text.is_empty() {
                    probe.pane = text;
                }
                if ready {
                    return Ok(());
                }
            }
            Err(_) => probe.snapshot_errors += 1,
        }
        match inventory(node, probe_limit(deadline)).await {
            Ok(inventory) if Instant::now() < deadline && !is_live(&inventory, terminal_id) => {
                // The host keeps a retired terminal's final text, so one read after the exit
                // carries the process's own output into the failure report.
                if let Ok(text) = snapshot(node, host_terminal_id, probe_limit(deadline)).await
                    && !text.is_empty()
                {
                    probe.pane = text;
                }
                return Err(Stop::Failed {
                    code: "codex_terminal_exited_before_composer",
                    detail: format!("terminal {terminal_id} left the host inventory"),
                });
            }
            Ok(_) => {}
            Err(_) => probe.liveness_errors += 1,
        }
        slot += POLL;
        let now = Instant::now();
        if slot <= now {
            slot = now + POLL;
        }
        sleep_until(slot.min(deadline)).await;
    }
}

async fn write(node: &NodeServices, run_id: &str, request: &WriteRequest) -> Result<(), Stop> {
    if cancelled(node, run_id).await {
        return Err(Stop::Cancelled);
    }
    match node.agents.host.write_once(request).await {
        Ok(_) => Ok(()),
        Err(ControlError::NotSent(error)) => Err(Stop::Failed {
            code: "codex_prompt_delivery_failed",
            detail: format!("write not sent: {error}"),
        }),
        Err(error @ ControlError::Host { .. }) => Err(Stop::Failed {
            code: "codex_prompt_delivery_failed",
            detail: error.to_string(),
        }),
        Err(error) => Err(Stop::Indeterminate(error.to_string())),
    }
}

/// The latest instant one probe may run: its own timeout, never past the composer budget.
fn probe_limit(deadline: Instant) -> Instant {
    deadline.min(Instant::now() + PROBE_DEADLINE)
}

async fn inventory(node: &NodeServices, until: Instant) -> Result<Inventory, ControlError> {
    timeout_at(
        until,
        node.agents
            .host
            .call(|client| Box::pin(async move { client.list().await })),
    )
    .await
    .unwrap_or_else(|_| Err(io::Error::from(io::ErrorKind::TimedOut).into()))
}

async fn snapshot(
    node: &NodeServices,
    host_terminal_id: &str,
    until: Instant,
) -> Result<String, ControlError> {
    let host_terminal_id = host_terminal_id.to_owned();
    timeout_at(
        until,
        node.agents.host.call(move |client| {
            let host_terminal_id = host_terminal_id.clone();
            Box::pin(async move {
                client
                    .snapshot(&host_terminal_id, "text", PANE_BYTES, PANE_LINES)
                    .await
                    .map(|snapshot| snapshot.text)
            })
        }),
    )
    .await
    .unwrap_or_else(|_| Err(io::Error::from(io::ErrorKind::TimedOut).into()))
}

fn is_live(inventory: &Inventory, terminal_id: &str) -> bool {
    inventory
        .terminals
        .iter()
        .any(|terminal| terminal["terminal_id"].as_str() == Some(terminal_id))
}

async fn cancelled(node: &NodeServices, run_id: &str) -> bool {
    node.agents
        .state
        .lock()
        .await
        .cancelled
        .contains_key(run_id)
}

async fn report(
    node: &NodeServices,
    run_id: &str,
    code: &str,
    detail: &str,
    pane: &str,
) -> anyhow::Result<()> {
    let payload = json!({
        "v": CONTRACT_VERSION,
        "run_id": run_id,
        "code": code,
        "detail": detail,
        "pane": pane,
    });
    let request = drain::request(node, FAILURE, &payload, None).await?;
    let response = timeout(REPORT_DEADLINE, node.relay.forward(request))
        .await
        .context("prompt delivery failure report timed out")?;
    if !response.status().is_success() {
        bail!("prompt delivery failure hub returned {}", response.status());
    }
    Ok(())
}
