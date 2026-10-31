//! Node command dispatch: spawn, cancel and terminal write, answered over the node channel.
//!
//! Admission runs in arrival order and commits each command's state effects. Every reply that
//! names a run then waits for that run's earlier turns. A spawn starts its preparation only once
//! its acceptance is delivered; an undelivered acceptance cancels the run.
use super::{
    NodeAgents, Preparing,
    lanes::Turn,
    launch::{
        LaunchContext, LaunchFailure, cancelled_failure, now_ms, record_failure, spawn_plan,
        start_planned,
    },
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

/// One admitted command. A reply decided at admission still waits for its run's earlier turns
/// when it names a run; a cancel or write waits for them before it acts.
pub enum Admitted {
    Answered(Dispatched),
    Cancel {
        agents: Arc<NodeAgents>,
        request_id: String,
        run_id: String,
        signal: KillSignal,
        turn: Turn,
    },
    Write {
        agents: Arc<NodeAgents>,
        request_id: String,
        run_id: String,
        payload: WritePayload,
        turn: Turn,
    },
}

impl Admitted {
    /// Waits for this command's turn, then runs it.
    pub async fn run(self) -> Dispatched {
        match self {
            Self::Answered(dispatched) => {
                if let Some(turn) = &dispatched.turn {
                    turn.wait().await;
                }
                dispatched
            }
            Self::Cancel {
                agents,
                request_id,
                run_id,
                signal,
                turn,
            } => {
                turn.wait().await;
                let reply = cancel_turn(&agents, &request_id, &run_id, signal).await;
                Dispatched::held(reply, turn)
            }
            Self::Write {
                agents,
                request_id,
                run_id,
                payload,
                turn,
            } => {
                turn.wait().await;
                let reply = write_turn(&agents, &request_id, &run_id, &payload).await;
                Dispatched::held(reply, turn)
            }
        }
    }
}

/// A command's reply. It holds the run's turn until the reply is delivered or dropped, so a
/// later command on the run waits for the reply to leave. A spawn's preparation starts only
/// when [`Dispatched::delivered`] reports the acceptance sent.
pub struct Dispatched {
    pub reply: Value,
    turn: Option<Turn>,
    release: Release,
}

impl Dispatched {
    fn answer(reply: Value) -> Self {
        Self {
            reply,
            turn: None,
            release: Release::default(),
        }
    }

    fn held(reply: Value, turn: Turn) -> Self {
        Self {
            reply,
            turn: Some(turn),
            release: Release::default(),
        }
    }

    /// Records the reply as delivered: starts any preparation, then ends the turn. Dropping
    /// the reply without this call cancels a run it accepted.
    pub fn delivered(self) {
        let Self { turn, release, .. } = self;
        release.start();
        drop(turn);
    }

    /// Takes the reply for a caller that sends nothing, which counts as delivered.
    pub fn into_reply(self) -> Value {
        let Self {
            reply,
            turn,
            release,
        } = self;
        release.start();
        drop(turn);
        reply
    }
}

/// A spawned run's preparation, held until its acceptance is delivered. Dropped before then, it
/// cancels the run.
#[derive(Default)]
pub struct Release(Option<Box<Prepare>>);

impl Release {
    fn start(mut self) {
        if let Some(prepare) = self.0.take() {
            tokio::spawn(prepare.run());
        }
    }
}

impl Drop for Release {
    fn drop(&mut self) {
        if let Some(prepare) = self.0.take() {
            tokio::spawn(prepare.abandon());
        }
    }
}

/// A spawned run's preparation. It holds the run's `busy` lock until the run settles.
struct Prepare {
    agents: Arc<NodeAgents>,
    context: Arc<LaunchContext>,
    payload: SpawnPayload,
    cancelled: watch::Receiver<bool>,
    busy: OwnedMutexGuard<()>,
}

impl Prepare {
    /// Prepares and launches the run, settles it on failure, and clears its preparation entry.
    async fn run(self) {
        let Self {
            agents,
            context,
            payload,
            cancelled,
            busy,
        } = self;
        let run_id = payload.run_id.clone();
        if let Err(failure) = prelaunch(&agents, &context, &payload, cancelled).await {
            record_failure(&agents, &run_id, failure).await;
        }
        finish(&agents, &run_id, busy);
    }

    /// Cancels a run whose acceptance never reached the hub: commits its cancel record, so a
    /// retried spawn answers `cancelled`, and settles it unlaunched without preparing anything.
    async fn abandon(self) {
        let Self {
            agents,
            payload,
            busy,
            ..
        } = self;
        let run_id = payload.run_id;
        {
            let mut state = agents.state.lock().await;
            let mut next = state.clone();
            next.cancelled.entry(run_id.clone()).or_insert_with(now_ms);
            match next.commit(&agents.home, &agents.changed) {
                Ok(()) => *state = next,
                Err(error) => {
                    eprintln!("gdaemon node: record cancel of undelivered run {run_id}: {error:#}");
                }
            }
        }
        record_failure(&agents, &run_id, cancelled_failure()).await;
        finish(&agents, &run_id, busy);
    }
}

/// Clears a settled run's preparation entry, then releases its `busy` lock.
fn finish(agents: &NodeAgents, run_id: &str, busy: OwnedMutexGuard<()>) {
    agents
        .preparing
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
        .remove(run_id);
    // Last, so a cancel that waits on the lock reads the settled phase.
    drop(busy);
}

/// Admits one command frame in arrival order.
pub async fn admit(
    agents: &Arc<NodeAgents>,
    context: &Arc<LaunchContext>,
    frame: Value,
) -> Admitted {
    let request_id = frame["request_id"].as_str().unwrap_or_default().to_owned();
    if frame["v"].as_u64() != Some(CONTRACT_VERSION) {
        return answered(reply_err(
            &request_id,
            "unsupported_version",
            "unsupported command contract version",
            Stage::None,
        ));
    }
    let command = frame["command"].as_str().unwrap_or_default().to_owned();
    match command.as_str() {
        "agent.spawn" => match serde_json::from_value::<SpawnCommand>(frame) {
            Ok(command) => {
                Admitted::Answered(spawn(agents, context, &request_id, command.payload).await)
            }
            Err(error) => answered(invalid_payload(&request_id, &error)),
        },
        "agent.cancel" => match serde_json::from_value::<CancelCommand>(frame) {
            Ok(command) => cancel(agents, request_id, command.payload).await,
            Err(error) => answered(invalid_payload(&request_id, &error)),
        },
        "terminal.write" => match serde_json::from_value::<WriteCommand>(frame) {
            Ok(command) => write(agents, request_id, command.payload).await,
            Err(error) => answered(invalid_payload(&request_id, &error)),
        },
        _ => answered(reply_err(
            &request_id,
            "unknown_command",
            "unknown command",
            Stage::None,
        )),
    }
}

/// Answers one command frame in full: admission, the run's turn, then the reply.
pub async fn dispatch(agents: Arc<NodeAgents>, context: Arc<LaunchContext>, frame: Value) -> Value {
    admit(&agents, &context, frame)
        .await
        .run()
        .await
        .into_reply()
}

fn answered(reply: Value) -> Admitted {
    Admitted::Answered(Dispatched::answer(reply))
}

/// Accepts a spawn and commits the acceptance. Preparation starts when the reply is delivered.
async fn spawn(
    agents: &Arc<NodeAgents>,
    context: &Arc<LaunchContext>,
    request_id: &str,
    payload: SpawnPayload,
) -> Dispatched {
    if !PROVIDERS.contains(&payload.provider.as_str()) {
        let message = format!("unsupported provider `{}`", payload.provider);
        return Dispatched::answer(reply_err(
            request_id,
            "invalid_payload",
            message,
            Stage::None,
        ));
    }
    if !Path::new(&payload.source_root).is_dir() {
        let message = format!("source_root `{}` is not a directory", payload.source_root);
        return Dispatched::answer(reply_err(
            request_id,
            "invalid_payload",
            message,
            Stage::None,
        ));
    }
    if let Err(error) = require_managed_srt(&payload.sandbox) {
        return Dispatched::answer(reply_err(
            request_id,
            "invalid_payload",
            error.to_string(),
            Stage::None,
        ));
    }
    if context.installation.root.as_os_str().is_empty() {
        let message = "SRT install is not verified; managed spawns are refused";
        return Dispatched::answer(reply_err(
            request_id,
            "command_failed",
            message,
            Stage::None,
        ));
    }
    let run_id = payload.run_id.clone();
    let mut state = agents.state.lock().await;
    // Taken under the state lock, so the run's turns follow the order its admissions commit.
    let turn = agents.lanes.enter(&run_id);
    if state.cancelled.contains_key(&run_id) {
        return Dispatched::held(
            reply_err(request_id, "cancelled", "run was cancelled", Stage::None),
            turn,
        );
    }
    if state.runs.contains_key(&run_id) {
        return Dispatched::held(reply_ok(request_id, json!({"accepted": true})), turn);
    }
    if state.live_and_accepted() >= LIVE_LIMIT {
        let message = format!("node holds {LIVE_LIMIT} live or accepted runs");
        return Dispatched::held(
            reply_err(request_id, "command_failed", message, Stage::None),
            turn,
        );
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
        return Dispatched::held(
            reply_err(request_id, "command_failed", message, Stage::None),
            turn,
        );
    }
    *state = next;
    let (cancel, cancelled) = watch::channel(false);
    let busy = Arc::new(Mutex::new(()));
    // Taken before the run is visible to cancel, so a cancel waits for this preparation to end.
    let guard = Arc::clone(&busy)
        .try_lock_owned()
        .expect("a fresh preparation lock is free");
    agents
        .preparing
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
        .insert(run_id, Preparing { cancel, busy });
    drop(state);
    Dispatched {
        reply: reply_ok(request_id, json!({"accepted": true})),
        turn: Some(turn),
        release: Release(Some(Box::new(Prepare {
            agents: Arc::clone(agents),
            context: Arc::clone(context),
            payload,
            cancelled,
            busy: guard,
        }))),
    }
}

/// Runs a spawn's preparation and launch under one deadline. The deadline and a cancel each stop
/// the wait. An abandoned blocking plan is joined before the failure, so its files settle first.
async fn prelaunch(
    agents: &Arc<NodeAgents>,
    context: &Arc<LaunchContext>,
    payload: &SpawnPayload,
    mut cancelled: watch::Receiver<bool>,
) -> Result<(), LaunchFailure> {
    // Biased order makes the deadline win a tie with the stage's own timers.
    let deadline = tokio::time::sleep(PREPARE_TIMEOUT);
    tokio::pin!(deadline);
    let checks = cancelled.clone();
    let cwd = tokio::select! {
        biased;
        () = &mut deadline => return Err(timeout_failure()),
        () = cancel_recorded(&mut cancelled) => return Err(cancelled_failure()),
        cwd = prepare(agents, context, payload, &checks) => cwd?,
    };
    if *cancelled.borrow() {
        return Err(cancelled_failure());
    }
    let mut plan = spawn_plan(agents, context, &payload.run_id, payload, &cwd);
    let (argv, env) = tokio::select! {
        biased;
        () = &mut deadline => {
            let _ = (&mut plan).await;
            return Err(timeout_failure());
        }
        () = cancel_recorded(&mut cancelled) => {
            let _ = (&mut plan).await;
            return Err(cancelled_failure());
        }
        joined = &mut plan => joined.map_err(|error| {
            LaunchFailure::new(ExitReason::PrepareFailed, format!("prepare task: {error}"))
        })??,
    };
    start_planned(agents, payload, &payload.run_id, &cwd, argv, env).await?;
    #[cfg(unix)]
    super::codex_prompt::schedule(agents, payload);
    Ok(())
}

fn timeout_failure() -> LaunchFailure {
    LaunchFailure::new(ExitReason::PrepareTimeout, "preparation timed out")
}

/// Resolves once a cancel is recorded. The watch guard drops here, so no select output holds it
/// across an await; that output would make the preparation future unsendable.
async fn cancel_recorded(cancelled: &mut watch::Receiver<bool>) {
    let _ = cancelled.wait_for(|cancel| *cancel).await;
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

/// Records the cancel fence in arrival order, so a spawn after this point is refused. A cancel for
/// a known run takes that run's turn.
async fn cancel(agents: &Arc<NodeAgents>, request_id: String, payload: CancelPayload) -> Admitted {
    let run_id = payload.run_id;
    let signal = match payload.signal {
        CancelSignal::Term => KillSignal::Term,
        CancelSignal::Kill => KillSignal::Kill,
    };
    let (known, turn) = {
        let mut state = agents.state.lock().await;
        // Taken under the state lock, so the run's turns follow the order its admissions commit.
        let turn = agents.lanes.enter(&run_id);
        let known = state.runs.contains_key(&run_id);
        let mut next = state.clone();
        next.cancelled.insert(run_id.clone(), now_ms());
        if let Err(error) = next.commit(&agents.home, &agents.changed) {
            let message = format!("record cancel: {error:#}");
            let reply = reply_err(&request_id, "command_failed", message, Stage::None);
            return Admitted::Answered(Dispatched::held(reply, turn));
        }
        *state = next;
        (known, turn)
    };
    if !known {
        let reply = reply_err(
            &request_id,
            "not_found",
            "no run has this run_id",
            Stage::None,
        );
        return Admitted::Answered(Dispatched::held(reply, turn));
    }
    Admitted::Cancel {
        agents: Arc::clone(agents),
        request_id,
        run_id,
        signal,
        turn,
    }
}

/// Stops a run. A launched run is signalled. A run still preparing has its preparation stopped,
/// and the reply reads the phase the run settled in.
async fn cancel_turn(
    agents: &Arc<NodeAgents>,
    request_id: &str,
    run_id: &str,
    signal: KillSignal,
) -> Value {
    if let Some(host) = launched_host(agents, run_id).await {
        return stop_launched(agents, request_id, &host, signal).await;
    }
    let preparing = {
        let tasks = agents
            .preparing
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        tasks.get(run_id).map(|entry| {
            entry.cancel.send_replace(true);
            Arc::clone(&entry.busy)
        })
    };
    if let Some(busy) = preparing {
        drop(busy.lock().await);
    }
    match launched_host(agents, run_id).await {
        Some(host) => stop_launched(agents, request_id, &host, signal).await,
        None => reply_ok(request_id, json!({"stopped": true})),
    }
}

/// The host terminal of a run that has launched.
async fn launched_host(agents: &NodeAgents, run_id: &str) -> Option<String> {
    let state = agents.state.lock().await;
    state
        .runs
        .get(run_id)
        .filter(|run| matches!(run.phase, RunPhase::Launched))
        .and_then(|run| run.host_terminal_id.clone())
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

/// Admits a terminal write to its run's lane. Writes to one run run in arrival order; a failed
/// write is never replayed.
async fn write(agents: &Arc<NodeAgents>, request_id: String, payload: WritePayload) -> Admitted {
    let owner = agents
        .state
        .lock()
        .await
        .runs
        .iter()
        .find(|(_, run)| run.terminal_id == payload.terminal_id)
        .map(|(run_id, _)| run_id.clone());
    let Some(run_id) = owner else {
        return answered(reply_err(
            &request_id,
            "not_found",
            "no run owns this terminal",
            Stage::None,
        ));
    };
    let turn = agents.lanes.enter(&run_id);
    Admitted::Write {
        agents: Arc::clone(agents),
        request_id,
        run_id,
        payload,
        turn,
    }
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
