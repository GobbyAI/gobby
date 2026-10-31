//! Provider launch, exits and host reconciliation for node-local agent runs.
use super::{
    NodeAgents,
    agy_gate::ensure_agy_support,
    executable::find_on_path,
    provider_command::build_command,
    provider_env::{EnvInputs, build_env},
    sandbox::{
        PolicyInputs,
        runtime::{SandboxLaunch, SrtInstallation, prepare_launch_with},
    },
    state::{AgentState, ExitRecord, RunPhase, WorkspaceRecord},
    trust::seed_trust,
    workspace,
};
use crate::node_commands::{ExitReason, ReportExit, SpawnPayload};
use gobby_terminals::control::{EventCursor, HostEventKind, SpawnRequest};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, OpenOptions, Permissions},
    io::{ErrorKind, Write},
    os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

/// The node-level inputs a launch takes besides the payload.
#[derive(Debug, Clone)]
pub struct LaunchContext {
    /// The verified SRT install the chokepoint wraps with.
    pub installation: SrtInstallation,
    /// The node's own front door, which becomes the agent's `GOBBY_DAEMON_URL`.
    pub front_door_url: String,
    /// The user's home: provider trust stores and the sandbox home.
    pub home: PathBuf,
    /// The `PATH` the provider and `agy` binaries are found on.
    pub path_env: String,
    /// The node's ambient environment, which the native env policy filters.
    pub ambient: BTreeMap<String, String>,
    /// 2.4's cache variables for this run.
    pub cache_env: BTreeMap<String, String>,
    pub daemon_port: u16,
    pub websocket_port: u16,
    /// `sys.platform` spelling of the host: `darwin` or `linux`.
    pub platform: String,
}

/// Why a launch did not start a child.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LaunchFailure {
    pub reason: ExitReason,
    pub detail: String,
}

impl LaunchFailure {
    pub(super) fn new(reason: ExitReason, detail: impl Into<String>) -> Self {
        Self {
            reason,
            detail: detail.into(),
        }
    }
}

/// The commit deadline Python arms for a spawn.
const COMMIT_DEADLINE_MS: u64 = 30_000;
/// The initial pane size Python's frame client sets.
const INITIAL_COLS: u16 = 80;
const INITIAL_ROWS: u16 = 24;
/// The exit watcher's pause before it reconnects.
const WATCH_RETRY: Duration = Duration::from_secs(1);

/// Prepares, spawns and commits one run's provider terminal and records it as launched. A
/// failure leaves the run in `runs`; the caller settles it as the run's exit.
pub async fn launch_with(
    agents: &NodeAgents,
    context: &LaunchContext,
    run_id: &str,
    payload: &SpawnPayload,
    cwd: &Path,
) -> Result<(), LaunchFailure> {
    match launch_steps(agents, context, run_id, payload, cwd).await {
        Ok(()) => Ok(()),
        Err(failure) => Err(record_failure(agents, run_id, failure).await),
    }
}

async fn launch_steps(
    agents: &NodeAgents,
    context: &LaunchContext,
    run_id: &str,
    payload: &SpawnPayload,
    cwd: &Path,
) -> Result<(), LaunchFailure> {
    let (gobby_home, owned_context, owned_run_id, owned_payload, owned_cwd) = (
        agents.home.clone(),
        context.clone(),
        run_id.to_owned(),
        payload.clone(),
        cwd.to_owned(),
    );
    // The agy probe, the file writes and the chokepoint block, so they run off the async workers.
    let (argv, env) = tokio::task::spawn_blocking(move || {
        prepare(
            &gobby_home,
            &owned_context,
            &owned_run_id,
            &owned_payload,
            &owned_cwd,
        )
    })
    .await
    .map_err(|error| {
        LaunchFailure::new(ExitReason::PrepareFailed, format!("prepare task: {error}"))
    })??;
    let host_terminal_id = start_child(agents, payload, run_id, cwd, argv, env).await?;
    if let Err(error) = record_launched(agents, run_id, &host_terminal_id).await {
        // The child is live but unrecorded, so it is killed rather than left unowned.
        let abort = abort_child(agents, &host_terminal_id).await;
        return Err(LaunchFailure::new(
            ExitReason::LaunchFailed,
            format!("record launch: {error}{abort}"),
        ));
    }
    Ok(())
}

/// The blocking half of a launch: the agy gate, trust, the prompt file, the chokepoint, and the
/// provider's argv and env.
fn prepare(
    gobby_home: &Path,
    context: &LaunchContext,
    run_id: &str,
    payload: &SpawnPayload,
    cwd: &Path,
) -> Result<(Vec<String>, BTreeMap<String, String>), LaunchFailure> {
    if payload.provider == "agy" {
        ensure_agy_support(&context.path_env, cwd)
            .map_err(|refusal| LaunchFailure::new(ExitReason::PrepareFailed, refusal))?;
    }
    if let Err(error) = seed_trust(&payload.provider, &context.home, cwd) {
        // A trust store that cannot be written only warns; the launch goes on.
        eprintln!("warning: {} trust seed failed: {error:#}", payload.provider);
    }
    let prompt_file = write_prompt_file(gobby_home, run_id, &payload.prompt)?;
    let sandbox = prepare_sandbox(gobby_home, context, run_id, payload, cwd, &prompt_file)?;
    let provider_env = build_env(
        payload,
        &EnvInputs {
            cwd,
            gobby_home,
            front_door_url: &context.front_door_url,
            prompt_file: &prompt_file,
            cache_env: &context.cache_env,
            sandbox_launch: &sandbox,
            ambient: &context.ambient,
        },
    )
    .map_err(prepare_error)?;
    let command = build_command(payload, cwd, gobby_home, &sandbox, &provider_env.env)
        .map_err(prepare_error)?;
    let argv = wrap_with_prefix(&sandbox, &provider_env.argv_prefix, &command);
    Ok((argv, provider_env.env))
}

fn prepare_error(error: anyhow::Error) -> LaunchFailure {
    LaunchFailure::new(ExitReason::PrepareFailed, format!("{error:#}"))
}

/// Runs the chokepoint over the prompt file. The file leaves the node as soon as the chokepoint
/// returns, whether it copied the prompt or refused the launch.
fn prepare_sandbox(
    gobby_home: &Path,
    context: &LaunchContext,
    run_id: &str,
    payload: &SpawnPayload,
    cwd: &Path,
    prompt_file: &Path,
) -> Result<SandboxLaunch, LaunchFailure> {
    let inputs = policy_inputs(gobby_home, context, run_id, payload, cwd);
    let prepared = prepare_launch_with(&inputs, prompt_file, &context.installation);
    let removed = remove_prompt_file(prompt_file);
    let sandbox = prepared
        .map_err(|error| LaunchFailure::new(ExitReason::PrepareFailed, error.to_string()))?;
    removed.map_err(|error| {
        LaunchFailure::new(
            ExitReason::PrepareFailed,
            format!("remove prompt file: {error}"),
        )
    })?;
    Ok(sandbox)
}

fn policy_inputs(
    gobby_home: &Path,
    context: &LaunchContext,
    run_id: &str,
    payload: &SpawnPayload,
    cwd: &Path,
) -> PolicyInputs {
    // The cache variables name this run's caches, so they win over the ambient values.
    let mut env = context.ambient.clone();
    env.extend(context.cache_env.clone());
    PolicyInputs {
        provider: payload.provider.clone(),
        sandbox: payload.sandbox.clone(),
        source_root: PathBuf::from(&payload.source_root),
        project_id: payload.project_id.clone(),
        session_id: payload.session_id.clone(),
        workspace: cwd.to_owned(),
        home: context.home.clone(),
        gobby_home: gobby_home.to_owned(),
        env,
        provider_executable: find_on_path(&context.path_env, &payload.provider),
        daemon_port: context.daemon_port,
        websocket_port: context.websocket_port,
        run_id: run_id.to_owned(),
        prewarm_pre_commit_store: payload.prewarm_pre_commit_store,
        platform: context.platform.clone(),
        darwin_user_dirs: None,
        // The payload carries no seat-folder authority, so these grant nothing.
        definition: None,
        session_seq: None,
        epic_seq: None,
        read_all_agent_workspaces: false,
        predecessor_workspace_folders: Vec::new(),
    }
}

/// The sandbox wrapper with the native env prefix between its `--` and the provider. `wrap`
/// replaces the provider's argv[0] with the provider executable, so the prefix goes in after.
fn wrap_with_prefix(sandbox: &SandboxLaunch, prefix: &[String], command: &[String]) -> Vec<String> {
    let wrapped = sandbox.wrap(command);
    let (runner, provider) = wrapped.split_at(wrapped.len() - command.len());
    [runner, prefix, provider].concat()
}

/// Spawns the child and commits it, returning its host terminal id. A refused commit aborts the
/// prepared child.
async fn start_child(
    agents: &NodeAgents,
    payload: &SpawnPayload,
    run_id: &str,
    cwd: &Path,
    argv: Vec<String>,
    env: BTreeMap<String, String>,
) -> Result<String, LaunchFailure> {
    let request = SpawnRequest {
        terminal_id: payload.terminal_id.clone(),
        spawn_key: run_id.to_owned(),
        argv,
        cwd: cwd.display().to_string(),
        env,
        cols: INITIAL_COLS,
        rows: INITIAL_ROWS,
        commit_deadline_ms: COMMIT_DEADLINE_MS,
        reservation_id: None,
        reserve_key: None,
    };
    let prepared = agents
        .host
        .call(|client| {
            let request = request.clone();
            Box::pin(async move { client.spawn(&request).await })
        })
        .await
        .map_err(|error| LaunchFailure::new(ExitReason::LaunchFailed, format!("spawn: {error}")))?;
    let host_terminal_id = prepared.host_terminal_id;
    // The host can report this child's exit during the commit, so the run must name its host
    // terminal before the commit is requested.
    if let Err(error) = record_host(agents, run_id, &host_terminal_id).await {
        let abort = abort_child(agents, &host_terminal_id).await;
        return Err(LaunchFailure::new(
            ExitReason::LaunchFailed,
            format!("record host: {error}{abort}"),
        ));
    }
    let (terminal_id, spawn_key) = (payload.terminal_id.clone(), run_id.to_owned());
    let committed = agents
        .host
        .call(|client| {
            let (terminal_id, spawn_key) = (terminal_id.clone(), spawn_key.clone());
            Box::pin(async move {
                client
                    .spawn_commit(&terminal_id, &spawn_key, Some(COMMIT_DEADLINE_MS))
                    .await
            })
        })
        .await;
    match committed {
        Ok(()) => Ok(host_terminal_id),
        Err(error) => {
            let abort = abort_child(agents, &host_terminal_id).await;
            Err(LaunchFailure::new(
                ExitReason::LaunchFailed,
                format!("spawn commit: {error}{abort}"),
            ))
        }
    }
}

/// Kills a prepared child that will not run. The host has no separate abort, so a kill with
/// zero grace is the abort. The result is a detail suffix, empty when the kill succeeded.
async fn abort_child(agents: &NodeAgents, host_terminal_id: &str) -> String {
    let killed = agents
        .host
        .call(|client| {
            let host = host_terminal_id.to_owned();
            Box::pin(async move { client.kill(&host, 0).await })
        })
        .await;
    match killed {
        Ok(()) => String::new(),
        Err(error) => format!("; abort failed: {error}"),
    }
}

/// Names the host terminal of a spawned child on its run. The run stays `Preparing` until the
/// commit succeeds.
async fn record_host(
    agents: &NodeAgents,
    run_id: &str,
    host_terminal_id: &str,
) -> Result<(), String> {
    let mut state = agents.state.lock().await;
    let mut next = state.clone();
    let run = next
        .runs
        .get_mut(run_id)
        .ok_or_else(|| "run is no longer accepted".to_owned())?;
    run.host_terminal_id = Some(host_terminal_id.to_owned());
    next.commit(&agents.home, &agents.changed)
        .map_err(|error| format!("{error:#}"))?;
    *state = next;
    Ok(())
}

/// Marks the run launched with its host terminal id, durably, before the in-memory state moves.
async fn record_launched(
    agents: &NodeAgents,
    run_id: &str,
    host_terminal_id: &str,
) -> Result<(), String> {
    let mut state = agents.state.lock().await;
    let mut next = state.clone();
    let run = next
        .runs
        .get_mut(run_id)
        .ok_or_else(|| "run is no longer accepted".to_owned())?;
    run.phase = RunPhase::Launched;
    run.host_terminal_id = Some(host_terminal_id.to_owned());
    // An exit the watcher saw during the commit settles now that the run is owned.
    if let Some(exit_code) = next.pending_exits.get(run_id).copied() {
        let reason = next.exit_reason(run_id);
        settle(&mut next, run_id, reason, true, exit_code, now_ms());
    }
    next.commit(&agents.home, &agents.changed)
        .map_err(|error| format!("{error:#}"))?;
    *state = next;
    Ok(())
}

/// Records a failed launch as the run's exit, so a launch that returned never leaves its run
/// owned. The workspace leaves disk before the exit commits, so an observed exit means it is
/// gone; a failed removal stays queued for `retry_removals`. A failed commit reports the
/// unrecorded exit beside the launch failure.
pub(super) async fn record_failure(
    agents: &NodeAgents,
    run_id: &str,
    failure: LaunchFailure,
) -> LaunchFailure {
    let queued = {
        let mut state = agents.state.lock().await;
        let mut next = state.clone();
        let queued = next.to_removals(run_id);
        if let Err(error) = next.commit(&agents.home, &agents.changed) {
            return LaunchFailure {
                detail: format!("{}; record exit: {error:#}", failure.detail),
                ..failure
            };
        }
        *state = next;
        queued
    };
    let removed = match queued {
        Some(record) => match workspace::remove(&record).await {
            Ok(()) => true,
            Err(error) => {
                eprintln!("gdaemon node: keeping removal record for run {run_id}: {error:#}");
                false
            }
        },
        None => false,
    };
    let mut state = agents.state.lock().await;
    let mut next = state.clone();
    settle(&mut next, run_id, failure.reason, false, None, now_ms());
    if removed {
        next.removals.remove(run_id);
    }
    match next.commit(&agents.home, &agents.changed) {
        Ok(()) => {
            *state = next;
            failure
        }
        Err(error) => LaunchFailure {
            detail: format!("{}; record exit: {error:#}", failure.detail),
            ..failure
        },
    }
}

/// Writes the prompt to `<gobby_home>/node/prompts/<run_id>.md`: mode 0600 in a 0700 directory.
fn write_prompt_file(
    gobby_home: &Path,
    run_id: &str,
    prompt: &str,
) -> Result<PathBuf, LaunchFailure> {
    let directory = gobby_home.join("node/prompts");
    let path = directory.join(format!("{run_id}.md"));
    write_private(&directory, &path, prompt).map_err(|error| {
        LaunchFailure::new(
            ExitReason::PrepareFailed,
            format!("write prompt file: {error}"),
        )
    })?;
    Ok(path)
}

fn write_private(directory: &Path, path: &Path, prompt: &str) -> std::io::Result<()> {
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(directory)?;
    fs::set_permissions(directory, Permissions::from_mode(0o700))?;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?;
    file.write_all(prompt.as_bytes())?;
    file.sync_all()
}

fn remove_prompt_file(path: &Path) -> std::io::Result<()> {
    match fs::remove_file(path) {
        Err(error) if error.kind() != ErrorKind::NotFound => Err(error),
        _ => Ok(()),
    }
}

/// Subscribes to host events and commits an exit for each launched run that ends. Every
/// subscription reconciles once before it reads events. A gap, an epoch change or a lost
/// connection restarts it.
pub async fn watch_exits(agents: &NodeAgents) -> anyhow::Result<()> {
    loop {
        if let Err(error) = watch_once(agents).await {
            eprintln!("warning: host exit watch restarting: {error:#}");
        }
        tokio::time::sleep(WATCH_RETRY).await;
    }
}

async fn watch_once(agents: &NodeAgents) -> anyhow::Result<()> {
    // A dedicated connection: the event reads block, so they cannot hold the shared host client.
    let mut events = agents.host.connect().await?;
    let subscription = events.subscribe_events(None).await?;
    let mut cursor = EventCursor::start(&subscription);
    reconcile(agents).await?;
    loop {
        let event = events.next_event().await?;
        cursor.accept(&event)?;
        if let HostEventKind::TerminalExited {
            host_terminal_id,
            exit_code,
            ..
        } = &event.kind
        {
            settle_exit(agents, host_terminal_id, exit_code.map(i64::from)).await?;
        }
        cursor.commit(event.seq)?;
    }
}

/// Settles the run that owns `host_terminal_id`, if one does. A launched run settles now; a run
/// still preparing keeps the exit until its launch records it. A cancelled run reports
/// `cancelled`; any other run reports `exited`.
async fn settle_exit(
    agents: &NodeAgents,
    host_terminal_id: &str,
    exit_code: Option<i64>,
) -> anyhow::Result<()> {
    let mut state = agents.state.lock().await;
    let owner = state
        .runs
        .iter()
        .find(|(_, run)| run.host_terminal_id.as_deref() == Some(host_terminal_id))
        .map(|(run_id, run)| (run_id.clone(), run.phase));
    let Some((run_id, phase)) = owner else {
        return Ok(());
    };
    let mut next = state.clone();
    match phase {
        RunPhase::Launched => {
            let reason = next.exit_reason(&run_id);
            settle(&mut next, &run_id, reason, true, exit_code, now_ms());
        }
        // The launch has not recorded this run as launched yet; `record_launched` settles it.
        RunPhase::Preparing => {
            next.pending_exits.insert(run_id, exit_code);
        }
        RunPhase::Accepted => return Ok(()),
    }
    next.commit(&agents.home, &agents.changed)?;
    *state = next;
    Ok(())
}

/// Settles each launched run whose terminal is absent from the live host's inventory. A present
/// run stays live, including after a host epoch change, because the inventory is the live host.
pub async fn reconcile(agents: &NodeAgents) -> anyhow::Result<()> {
    // A list can prove absence only for runs launched before the snapshot.
    // Remember the full record so a replacement under the same ID also survives.
    let eligible: Vec<_> = agents
        .state
        .lock()
        .await
        .runs
        .iter()
        .filter(|(_, run)| run.phase == RunPhase::Launched)
        .map(|(run_id, run)| (run_id.clone(), run.clone()))
        .collect();
    let inventory = agents
        .host
        .call(|client| Box::pin(async move { client.list().await }))
        .await?;
    let live: BTreeSet<String> = inventory
        .terminals
        .iter()
        .filter_map(|terminal| terminal["terminal_id"].as_str().map(str::to_owned))
        .collect();
    let mut state = agents.state.lock().await;
    let absent: Vec<String> = eligible
        .into_iter()
        .filter(|(run_id, run)| {
            state.runs.get(run_id) == Some(run) && !live.contains(&run.terminal_id)
        })
        .map(|(run_id, _)| run_id)
        .collect();
    if absent.is_empty() {
        return Ok(());
    }
    let mut next = state.clone();
    for run_id in &absent {
        let reason = next.exit_reason(run_id);
        settle(&mut next, run_id, reason, true, None, now_ms());
    }
    next.commit(&agents.home, &agents.changed)?;
    *state = next;
    Ok(())
}

/// Drops a launched run and records its exit. The run's workspace queues for removal.
fn settle(
    state: &mut AgentState,
    run_id: &str,
    reason: ExitReason,
    launched: bool,
    exit_code: Option<i64>,
    ended_at_ms: u64,
) {
    state.pending_exits.remove(run_id);
    let Some(run) = state.runs.get(run_id) else {
        return;
    };
    let exit = ReportExit {
        run_id: run_id.to_owned(),
        terminal_id: run.terminal_id.clone(),
        launched,
        exit_code,
        reason,
        workspace: run.workspace.as_ref().map(WorkspaceRecord::report),
        sandbox: None,
    };
    state.to_removals(run_id);
    state.runs.remove(run_id);
    state.exits.push(ExitRecord { exit, ended_at_ms });
}

pub(super) fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| {
            u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX)
        })
}
