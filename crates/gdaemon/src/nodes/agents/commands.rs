//! Node command dispatch: spawn, cancel and terminal write, answered over the node channel.
use super::{
    NodeAgents, Preparing,
    launch::{LaunchContext, LaunchFailure, launch_with, now_ms, record_failure},
    repair::RepairEnv,
    sandbox::runtime::require_managed_srt,
    state::{RunPhase, RunRecord},
    workspace::{self, plan_workspace},
};
use crate::node_commands::{
    CONTRACT_VERSION, CancelCommand, CancelPayload, CancelSignal, ExitReason, SpawnCommand,
    SpawnPayload, Stage, WriteCommand, WriteKind, WritePayload,
};
use gobby_terminals::control::{
    ControlError, KillSignal, WriteKind as TerminalWriteKind, WriteRequest,
};
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    sync::{Arc, PoisonError},
    time::Duration,
};
use tokio::sync::{Mutex, OwnedMutexGuard, watch};

/// Accepted or live runs one node holds before it refuses a spawn.
const LIVE_LIMIT: usize = 1_000;
/// Preparation past this bound fails the run as `prepare_timeout`.
const PREPARE_TIMEOUT: Duration = Duration::from_secs(300);
/// TERM grace before the host escalates a cancel to SIGKILL. It stays inside the host call deadline.
const CANCEL_GRACE_MS: u64 = 1_000;
/// Seed bound for the `uv` environment seed.
const SEED_TIMEOUT: Duration = Duration::from_secs(180);
/// Providers a spawn may name. Each maps to a command in `provider_command`.
const PROVIDERS: [&str; 5] = ["claude", "codex", "grok", "droid", "agy"];

/// Answers one command frame. Every outcome is a reply, including refusals of malformed input.
pub async fn dispatch(agents: Arc<NodeAgents>, context: Arc<LaunchContext>, frame: Value) -> Value {
    let request_id = frame["request_id"].as_str().unwrap_or_default().to_owned();
    if frame["v"].as_u64() != Some(CONTRACT_VERSION) {
        return reply_err(
            &request_id,
            "unsupported_version",
            "unsupported command contract version",
            Stage::None,
        );
    }
    let command = frame["command"].as_str().unwrap_or_default().to_owned();
    match command.as_str() {
        "agent.spawn" => match serde_json::from_value::<SpawnCommand>(frame) {
            Ok(command) => spawn(&agents, &context, &request_id, command.payload).await,
            Err(error) => invalid_payload(&request_id, &error),
        },
        "agent.cancel" => match serde_json::from_value::<CancelCommand>(frame) {
            Ok(command) => cancel(&agents, &request_id, command.payload).await,
            Err(error) => invalid_payload(&request_id, &error),
        },
        "terminal.write" => match serde_json::from_value::<WriteCommand>(frame) {
            Ok(command) => write(&agents, &request_id, command.payload).await,
            Err(error) => invalid_payload(&request_id, &error),
        },
        _ => reply_err(
            &request_id,
            "unknown_command",
            "unknown command",
            Stage::None,
        ),
    }
}

/// Accepts a spawn and starts its preparation. The reply is durable before any preparation runs.
async fn spawn(
    agents: &Arc<NodeAgents>,
    context: &Arc<LaunchContext>,
    request_id: &str,
    payload: SpawnPayload,
) -> Value {
    if !PROVIDERS.contains(&payload.provider.as_str()) {
        let message = format!("unsupported provider `{}`", payload.provider);
        return reply_err(request_id, "invalid_payload", message, Stage::None);
    }
    if !Path::new(&payload.source_root).is_dir() {
        let message = format!("source_root `{}` is not a directory", payload.source_root);
        return reply_err(request_id, "invalid_payload", message, Stage::None);
    }
    if let Err(error) = require_managed_srt(&payload.sandbox) {
        return reply_err(
            request_id,
            "invalid_payload",
            error.to_string(),
            Stage::None,
        );
    }
    if context.installation.root.as_os_str().is_empty() {
        let message = "SRT install is not verified; managed spawns are refused";
        return reply_err(request_id, "command_failed", message, Stage::None);
    }
    let run_id = payload.run_id.clone();
    let (cancel, cancelled) = watch::channel(false);
    let busy = Arc::new(Mutex::new(()));
    let mut state = agents.state.lock().await;
    if state.cancelled.contains_key(&run_id) {
        return reply_err(request_id, "cancelled", "run was cancelled", Stage::None);
    }
    if state.runs.contains_key(&run_id) {
        return reply_ok(request_id, json!({"accepted": true}));
    }
    if state.live_and_accepted() >= LIVE_LIMIT {
        let message = format!("node holds {LIVE_LIMIT} live or accepted runs");
        return reply_err(request_id, "command_failed", message, Stage::None);
    }
    let mut next = state.clone();
    next.runs.insert(
        run_id.clone(),
        RunRecord {
            terminal_id: payload.terminal_id.clone(),
            phase: RunPhase::Accepted,
            accepted_at_ms: now_ms(),
            workspace: None,
            host_terminal_id: None,
        },
    );
    if let Err(error) = next.commit(&agents.home, &agents.changed) {
        let message = format!("record accepted run: {error:#}");
        return reply_err(request_id, "command_failed", message, Stage::None);
    }
    *state = next;
    // Taken before the run is visible to cancel, so a cancel waits for this task to finish.
    let guard = Arc::clone(&busy)
        .try_lock_owned()
        .expect("a fresh preparation lock is free");
    agents
        .preparing
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
        .insert(run_id, Preparing { cancel, busy });
    drop(state);
    tokio::spawn(prepare_and_launch(
        Arc::clone(agents),
        Arc::clone(context),
        payload,
        cancelled,
        guard,
    ));
    reply_ok(request_id, json!({"accepted": true}))
}

/// Prepares and launches one accepted run. `_busy` holds the run's preparation lock until the
/// run is launched or settled.
async fn prepare_and_launch(
    agents: Arc<NodeAgents>,
    context: Arc<LaunchContext>,
    payload: SpawnPayload,
    mut cancelled: watch::Receiver<bool>,
    _busy: OwnedMutexGuard<()>,
) {
    let run_id = payload.run_id.clone();
    let checks = cancelled.clone();
    // Dropping `prepare` kills its seed child, so the deadline or a cancel ends the wait. Biased
    // order makes the deadline win a tie with the seed's own timer.
    let prepared = tokio::select! {
        biased;
        () = tokio::time::sleep(PREPARE_TIMEOUT) => {
            Err(LaunchFailure::new(ExitReason::PrepareTimeout, "preparation timed out"))
        }
        _ = cancelled.wait_for(|cancel| *cancel) => {
            Err(LaunchFailure::new(ExitReason::Cancelled, "preparation cancelled"))
        }
        prepared = prepare(&agents, &context, &payload, &checks) => prepared,
    };
    match prepared {
        Ok(cwd) => {
            // A failed launch is recorded as the run's exit by `launch_with`.
            let _ = launch_with(&agents, &context, &run_id, &payload, &cwd).await;
        }
        Err(failure) => {
            record_failure(&agents, &run_id, failure).await;
        }
    }
    agents
        .preparing
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
        .remove(&run_id);
}

/// Prepares the workspace and returns the launch cwd.
async fn prepare(
    agents: &Arc<NodeAgents>,
    context: &LaunchContext,
    payload: &SpawnPayload,
    cancel: &watch::Receiver<bool>,
) -> Result<PathBuf, LaunchFailure> {
    let planned = {
        let (run_id, project_id) = (payload.run_id.clone(), payload.project_id.clone());
        let source_root = PathBuf::from(&payload.source_root);
        let (workspace, home) = (payload.workspace.clone(), agents.home.clone());
        tokio::task::spawn_blocking(move || {
            plan_workspace(&run_id, &project_id, &source_root, &workspace, &home)
        })
        .await
        .map_err(|error| LaunchFailure::new(ExitReason::PrepareFailed, format!("{error}")))?
        .map_err(|error| LaunchFailure::new(ExitReason::PrepareFailed, format!("{error:#}")))?
    };
    let Some(record) = planned else {
        return Ok(PathBuf::from(&payload.source_root));
    };
    let hooks_dir = agents.home.join("hooks");
    let claude_json = context.home.join(".claude.json");
    let uv = super::executable::find_on_path(&context.path_env, "uv");
    let env = RepairEnv {
        home: &agents.home,
        hooks_dir: &hooks_dir,
        claude_json: &claude_json,
        uv: uv.as_deref(),
        seed_timeout: SEED_TIMEOUT,
    };
    let cancelled = || *cancel.borrow();
    let prepared = workspace::prepare(
        agents,
        &payload.run_id,
        &record,
        &payload.provider,
        &env,
        &cancelled,
    )
    .await;
    match prepared {
        Ok(()) => Ok(record.path.clone()),
        Err(error) => Err(prepare_failure(cancel, &error)),
    }
}

/// A cancel that landed during preparation reports `cancelled`; any other failure reports
/// `prepare_failed`.
fn prepare_failure(cancel: &watch::Receiver<bool>, error: &anyhow::Error) -> LaunchFailure {
    if *cancel.borrow() {
        LaunchFailure::new(ExitReason::Cancelled, format!("{error:#}"))
    } else {
        LaunchFailure::new(ExitReason::PrepareFailed, format!("{error:#}"))
    }
}

/// Records the cancel before answering, so a spawn after this reply is refused. A launched run is
/// signalled; an unlaunched run has its preparation stopped and then reads the phase it settled in.
async fn cancel(agents: &Arc<NodeAgents>, request_id: &str, payload: CancelPayload) -> Value {
    let run_id = payload.run_id;
    let signal = match payload.signal {
        CancelSignal::Term => KillSignal::Term,
        CancelSignal::Kill => KillSignal::Kill,
    };
    let known = {
        let mut state = agents.state.lock().await;
        let known = state
            .runs
            .get(&run_id)
            .map(|run| (run.phase, run.host_terminal_id.clone()));
        let mut next = state.clone();
        next.cancelled.insert(run_id.clone(), now_ms());
        if let Err(error) = next.commit(&agents.home, &agents.changed) {
            let message = format!("record cancel: {error:#}");
            return reply_err(request_id, "command_failed", message, Stage::None);
        }
        *state = next;
        known
    };
    let Some((phase, host_terminal_id)) = known else {
        return reply_err(
            request_id,
            "not_found",
            "no run has this run_id",
            Stage::None,
        );
    };
    if let (RunPhase::Launched, Some(host)) = (phase, &host_terminal_id) {
        return stop_launched(agents, request_id, host, signal).await;
    }
    let preparing = {
        let tasks = agents
            .preparing
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        tasks.get(&run_id).map(|entry| {
            entry.cancel.send_replace(true);
            Arc::clone(&entry.busy)
        })
    };
    if let Some(busy) = preparing {
        drop(busy.lock().await);
        let settled = agents
            .state
            .lock()
            .await
            .runs
            .get(&run_id)
            .map(|run| (run.phase, run.host_terminal_id.clone()));
        if let Some((RunPhase::Launched, Some(host))) = settled {
            return stop_launched(agents, request_id, &host, signal).await;
        }
    }
    reply_ok(request_id, json!({"stopped": true}))
}

async fn stop_launched(
    agents: &Arc<NodeAgents>,
    request_id: &str,
    host_terminal_id: &str,
    signal: KillSignal,
) -> Value {
    let killed = agents
        .host
        .call(|client| {
            let host = host_terminal_id.to_owned();
            Box::pin(async move { client.kill_signal(&host, CANCEL_GRACE_MS, signal).await })
        })
        .await;
    reply_ok(request_id, json!({"stopped": killed.is_ok()}))
}

/// Writes to a run's terminal. Writes to one run run in order; a failed write is never replayed.
async fn write(agents: &Arc<NodeAgents>, request_id: &str, payload: WritePayload) -> Value {
    let owner = agents
        .state
        .lock()
        .await
        .runs
        .iter()
        .find(|(_, run)| run.terminal_id == payload.terminal_id)
        .map(|(run_id, _)| run_id.clone());
    let Some(run_id) = owner else {
        return reply_err(
            request_id,
            "not_found",
            "no run owns this terminal",
            Stage::None,
        );
    };
    let gate = agents.gate(&run_id);
    let reply = {
        let _turn = gate.lock().await;
        write_turn(agents, request_id, &run_id, &payload).await
    };
    drop(gate);
    agents.release_gate(&run_id);
    reply
}

async fn write_turn(
    agents: &Arc<NodeAgents>,
    request_id: &str,
    run_id: &str,
    payload: &WritePayload,
) -> Value {
    let host = match agents.state.lock().await.runs.get(run_id) {
        None => return reply_err(request_id, "not_found", "run has settled", Stage::None),
        Some(run) => run.host_terminal_id.clone(),
    };
    let Some(host_terminal_id) = host else {
        let message = "run has no host terminal yet";
        return reply_err(request_id, "command_failed", message, Stage::None);
    };
    let request = WriteRequest {
        host_terminal_id,
        kind: terminal_kind(payload.kind),
        data: payload.data.clone().into_bytes(),
        submit: Some(payload.submit),
    };
    match agents.host.write_once(&request).await {
        Ok(_) => reply_ok(request_id, json!({})),
        Err(ControlError::NotSent(error)) => reply_err(
            request_id,
            "command_failed",
            format!("write not sent: {error}"),
            Stage::None,
        ),
        Err(error) => reply_err(
            request_id,
            "command_failed",
            format!("write failed after send: {error}"),
            Stage::Partial,
        ),
    }
}

fn terminal_kind(kind: WriteKind) -> TerminalWriteKind {
    match kind {
        WriteKind::Text => TerminalWriteKind::Text,
        WriteKind::Key => TerminalWriteKind::Key,
        WriteKind::Input => TerminalWriteKind::Input,
        WriteKind::Paste => TerminalWriteKind::Paste,
    }
}

fn invalid_payload(request_id: &str, error: &serde_json::Error) -> Value {
    reply_err(
        request_id,
        "invalid_payload",
        error.to_string(),
        Stage::None,
    )
}

fn reply_ok(request_id: &str, result: Value) -> Value {
    json!({
        "type": "reply",
        "v": CONTRACT_VERSION,
        "request_id": request_id,
        "ok": true,
        "result": result,
    })
}

fn reply_err(request_id: &str, code: &str, message: impl Into<String>, stage: Stage) -> Value {
    json!({
        "type": "reply",
        "v": CONTRACT_VERSION,
        "request_id": request_id,
        "ok": false,
        "error": {"code": code, "message": message.into(), "stage": stage},
    })
}
