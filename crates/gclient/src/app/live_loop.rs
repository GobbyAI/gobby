//! Interactive Tokio loop for the authenticated live daemon transport.

use std::time::Duration;

use futures_util::stream::{FuturesUnordered, StreamExt};
use gobby_terminal::input::KeyboardProtocol;
use gobby_terminal::raw_input::RawInputEvent;
use ratatui::backend::Backend;
use ratatui::Terminal;
use serde_json::{json, Value};
use tokio::sync::mpsc;
use tokio::time::Instant;

use crate::daemon::{
    Daemon, DaemonError, DaemonEvent, EventReceiver, Generation, KillOutcome, LiveDaemon,
    SpawnOutcome, SpawnRequest,
};
use crate::frame_source::{FrameError, FrameSource};
use crate::key_input::{key_input, resolve_chord, text_bytes, Resolution};
use crate::ui::{Action, Chrome, Mode, WorkspaceView};

use super::run_loop::{
    shutdown, ReconnectAttempt, ReconnectFuture, ReconnectSupervisor, RENDER_TICK,
};
use super::{ControlState, PaneId, Workspace};

const SHUTDOWN_DEADLINE: Duration = Duration::from_secs(2);

impl WorkspaceView for Workspace<LiveDaemon> {
    fn project_id(&self) -> Option<&str> {
        Workspace::<LiveDaemon>::project_id(self)
    }

    fn roster_terminal_ids(&self) -> Vec<String> {
        Workspace::<LiveDaemon>::roster_terminal_ids(self)
    }

    fn attention_entry_ids(&self) -> Vec<String> {
        Workspace::<LiveDaemon>::attention_entry_ids(self)
    }

    fn pane_for_terminal(&self, terminal_id: &str) -> Option<PaneId> {
        Workspace::<LiveDaemon>::pane_for_terminal(self, terminal_id)
    }

    fn pane(&self, id: PaneId) -> &super::Pane {
        Workspace::<LiveDaemon>::pane(self, id)
    }

    fn daemon_ready(&self) -> bool {
        Workspace::<LiveDaemon>::daemon_ready(self)
    }
}

#[cfg(unix)]
struct ExitSignals {
    interrupt: tokio::signal::unix::Signal,
    terminate: tokio::signal::unix::Signal,
    hangup: tokio::signal::unix::Signal,
}

#[cfg(unix)]
impl ExitSignals {
    fn new() -> std::io::Result<Self> {
        use tokio::signal::unix::{signal, SignalKind};

        Ok(Self {
            interrupt: signal(SignalKind::interrupt())?,
            terminate: signal(SignalKind::terminate())?,
            hangup: signal(SignalKind::hangup())?,
        })
    }

    async fn recv(&mut self) -> &'static str {
        tokio::select! {
            _ = self.interrupt.recv() => "SIGINT",
            _ = self.terminate.recv() => "SIGTERM",
            _ = self.hangup.recv() => "SIGHUP",
        }
    }
}

#[cfg(not(unix))]
struct ExitSignals;

#[cfg(not(unix))]
impl ExitSignals {
    fn new() -> std::io::Result<Self> {
        Ok(Self)
    }

    async fn recv(&mut self) -> &'static str {
        let _ = tokio::signal::ctrl_c().await;
        "SIGINT"
    }
}

#[cfg(unix)]
struct ResizeSignal(tokio::signal::unix::Signal);

#[cfg(unix)]
impl ResizeSignal {
    fn new() -> std::io::Result<Self> {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::window_change()).map(Self)
    }

    async fn recv(&mut self) {
        let _ = self.0.recv().await;
    }
}

#[cfg(not(unix))]
struct ResizeSignal;

#[cfg(not(unix))]
impl ResizeSignal {
    fn new() -> std::io::Result<Self> {
        Ok(Self)
    }

    async fn recv(&mut self) {
        std::future::pending::<()>().await;
    }
}

/// Run the interactive client against the authenticated daemon connection.
pub async fn run_live_loop<B: Backend>(
    workspace: &mut Workspace<LiveDaemon>,
    terminal: &mut Terminal<B>,
    chrome: &mut Chrome,
    mut input: mpsc::Receiver<RawInputEvent>,
) -> Result<(), FrameError> {
    let daemon = workspace.daemon().clone();
    let mut loop_error = None;
    if let Err(error) = reconcile_ready(workspace).await {
        workspace.latch_exit(error.to_string());
        loop_error = Some(FrameError::from(error));
    }
    sync_live_chrome(workspace, chrome);
    if let Some(pane_id) = chrome.focused_pane() {
        if let Err(error) = focus_live_pane(workspace, pane_id).await {
            chrome.status_message = Some(error.to_string());
        }
    }

    let (_, fallback_events) = Daemon::subscribe(&daemon);
    let mut events = Some(workspace.event_rx.take().unwrap_or(fallback_events));
    let mut exit_signals = install_exit_signals(workspace, &mut loop_error);
    let mut resize_signal = install_resize_signal(workspace, &mut loop_error);
    let mut render_tick = tokio::time::interval(RENDER_TICK);
    render_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut prefix_armed = false;
    let mut supervisor = ReconnectSupervisor::new();
    let mut reconnect_job = None;

    while workspace.exit_reason().is_none() {
        tokio::select! {
            biased;
            reason = recv_exit_signal(&mut exit_signals) => {
                workspace.latch_exit(reason);
            }
            event = input.recv() => {
                let Some(event) = event else {
                    workspace.latch_exit("terminal input closed");
                    continue;
                };
                match route_live_input(workspace, chrome, &event, &mut prefix_armed).await {
                    Ok(true) => {
                        workspace.latch_exit("quit");
                    }
                    Ok(false) => {}
                    Err(error) => chrome.status_message = Some(error.to_string()),
                }
            }
            event = recv_daemon_event(&mut events) => {
                match event {
                    Ok(event) => {
                        if let Err(error) =
                            handle_live_event(workspace, &mut supervisor, event).await
                        {
                            begin_reconnect(workspace, &mut supervisor, &daemon, error);
                        }
                        sync_live_chrome(workspace, chrome);
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => {
                        if let Err(error) = workspace.apply_live_event(DaemonEvent::Lagged).await {
                            begin_reconnect(workspace, &mut supervisor, &daemon, error);
                        }
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Closed) => {
                        events = None;
                        begin_reconnect(
                            workspace,
                            &mut supervisor,
                            &daemon,
                            DaemonError::Unavailable { retry_after: None },
                        );
                    }
                }
            }
            frame = recv_workspace_frame(workspace) => {
                if let Some((pane_id, Err(error))) = frame {
                    if let Err(recovery_error) =
                        workspace.recover_live_frame_error(pane_id, &error).await
                    {
                        chrome.status_message = Some(recovery_error.to_string());
                    }
                }
                if let Err(error) = render_live_workspace(terminal, workspace, chrome) {
                    workspace.latch_exit(error.to_string());
                    loop_error = Some(error);
                }
            }
            result = await_reconnect_job(&mut reconnect_job), if reconnect_job.is_some() => {
                reconnect_job = None;
                let outcome = supervisor.complete_attempt(result);
                handle_reconnect_outcome(
                    workspace,
                    chrome,
                    &mut events,
                    &mut supervisor,
                    outcome,
                ).await;
            }
            _ = wait_for_reconnect(supervisor.next_attempt_at()),
                if reconnect_job.is_none() && supervisor.next_attempt_at().is_some() =>
            {
                reconnect_job = supervisor.start_due_attempt(daemon.clone());
            }
            _ = recv_resize_signal(&mut resize_signal) => {
                if let Err(error) = resize_live_workspace(terminal, workspace, chrome).await {
                    chrome.status_message = Some(error.to_string());
                }
            }
            _ = render_tick.tick() => {
                workspace.submit_expired_detaches(&mut supervisor, Instant::now());
                if let Err(error) = render_live_workspace(terminal, workspace, chrome) {
                    workspace.latch_exit(error.to_string());
                    loop_error = Some(error);
                }
            }
        }
    }

    drop(reconnect_job.take());
    supervisor.cancel(DaemonError::Protocol {
        detail: workspace
            .exit_reason()
            .unwrap_or("client exiting")
            .to_string(),
    });
    let shutdown_result = shutdown(workspace, daemon, Instant::now() + SHUTDOWN_DEADLINE)
        .await
        .map_err(FrameError::from);
    match (loop_error, shutdown_result) {
        (Some(error), _) => Err(error),
        (None, result) => result,
    }
}

async fn reconcile_ready(workspace: &mut Workspace<LiveDaemon>) -> Result<(), DaemonError> {
    workspace.reconcile_subscribe_first().await?;
    workspace.attach_ready_panes().await
}

fn install_exit_signals(
    workspace: &mut Workspace<LiveDaemon>,
    loop_error: &mut Option<FrameError>,
) -> Option<ExitSignals> {
    match ExitSignals::new() {
        Ok(signals) => Some(signals),
        Err(error) => {
            workspace.latch_exit(error.to_string());
            *loop_error = Some(FrameError::Other(error.to_string()));
            None
        }
    }
}

fn install_resize_signal(
    workspace: &mut Workspace<LiveDaemon>,
    loop_error: &mut Option<FrameError>,
) -> Option<ResizeSignal> {
    match ResizeSignal::new() {
        Ok(signal) => Some(signal),
        Err(error) => {
            workspace.latch_exit(error.to_string());
            *loop_error = Some(FrameError::Other(error.to_string()));
            None
        }
    }
}

async fn recv_exit_signal(signals: &mut Option<ExitSignals>) -> &'static str {
    match signals {
        Some(signals) => signals.recv().await,
        None => std::future::pending().await,
    }
}

async fn recv_resize_signal(signal: &mut Option<ResizeSignal>) {
    match signal {
        Some(signal) => signal.recv().await,
        None => std::future::pending().await,
    }
}

async fn recv_daemon_event(
    events: &mut Option<EventReceiver>,
) -> Result<DaemonEvent, tokio::sync::broadcast::error::RecvError> {
    match events {
        Some(events) => events.recv().await,
        None => std::future::pending().await,
    }
}

async fn await_reconnect_job(job: &mut Option<ReconnectFuture>) -> Result<Generation, DaemonError> {
    match job {
        Some(job) => job.as_mut().await,
        None => std::future::pending().await,
    }
}

async fn wait_for_reconnect(ready_at: Option<Instant>) {
    match ready_at {
        Some(ready_at) => tokio::time::sleep_until(ready_at).await,
        None => std::future::pending().await,
    }
}

fn begin_reconnect(
    workspace: &mut Workspace<LiveDaemon>,
    supervisor: &mut ReconnectSupervisor,
    daemon: &LiveDaemon,
    error: DaemonError,
) {
    let generation = daemon.generation();
    workspace.observe_daemon_disconnect(generation, error);
    drop(supervisor.request(generation));
}

async fn handle_live_event(
    workspace: &mut Workspace<LiveDaemon>,
    supervisor: &mut ReconnectSupervisor,
    event: DaemonEvent,
) -> Result<(), DaemonError> {
    let terminal_created = matches!(
        &event,
        DaemonEvent::Terminal { payload, .. }
            if payload.get("event").and_then(Value::as_str) == Some("created")
    );
    if let DaemonEvent::Disconnected { generation, .. } = &event {
        drop(supervisor.request(*generation));
    }
    if let DaemonEvent::Message(message) = &event {
        apply_live_write_outcome(workspace, message);
    }
    workspace.apply_live_event(event).await?;
    if terminal_created {
        workspace.attach_ready_panes().await?;
    }
    Ok(())
}

async fn handle_reconnect_outcome(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    events: &mut Option<EventReceiver>,
    supervisor: &mut ReconnectSupervisor,
    outcome: ReconnectAttempt,
) {
    match outcome {
        ReconnectAttempt::Reconnected(generation) => match reconcile_ready(workspace).await {
            Ok(()) => {
                supervisor.handshake_complete(generation);
                let (_, fallback) = Daemon::subscribe(workspace.daemon());
                *events = Some(workspace.event_rx.take().unwrap_or(fallback));
                sync_live_chrome(workspace, chrome);
                if let Some(pane_id) = chrome.focused_pane() {
                    if let Err(error) = focus_live_pane(workspace, pane_id).await {
                        chrome.status_message = Some(error.to_string());
                    }
                }
            }
            Err(error) => {
                workspace.observe_daemon_disconnect(generation, error.clone());
                if let ReconnectAttempt::Exhausted(error) = supervisor.handshake_failed(error) {
                    workspace.latch_exit(error.to_string());
                }
            }
        },
        ReconnectAttempt::Exhausted(error) => {
            workspace.latch_exit(error.to_string());
        }
        ReconnectAttempt::RetryScheduled { .. } | ReconnectAttempt::Idle => {}
    }
}

fn sync_live_chrome(workspace: &Workspace<LiveDaemon>, chrome: &mut Chrome) {
    for tab in &mut chrome.tabs {
        let stale: Vec<_> = tab
            .slots
            .iter()
            .filter_map(|(slot, pane_id)| (!workspace.panes.contains_key(pane_id)).then_some(*slot))
            .collect();
        if stale.len() == tab.slots.len() {
            tab.slots.clear();
            continue;
        }
        for slot in stale {
            tab.layout.focus_pane(slot);
            let _ = tab.layout.close_focused();
            tab.slots.remove(&slot);
        }
    }
    chrome.tabs.retain(|tab| !tab.slots.is_empty());
    if chrome.active_tab >= chrome.tabs.len() {
        chrome.active_tab = chrome.tabs.len().saturating_sub(1);
    }

    let shown: Vec<_> = chrome
        .tabs
        .iter()
        .flat_map(|tab| tab.slots.values().copied())
        .collect();
    for terminal_id in workspace.roster_terminal_ids() {
        let Some(pane_id) = workspace.pane_for_terminal(&terminal_id) else {
            continue;
        };
        if !shown.contains(&pane_id) {
            chrome.open_pane(pane_id, &terminal_id);
        }
    }
}

async fn route_live_input(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    event: &RawInputEvent,
    prefix_armed: &mut bool,
) -> Result<bool, FrameError> {
    if let Some(input) = key_input(event, KeyboardProtocol::Legacy) {
        match resolve_chord(&chrome.keymap, &input.key, *prefix_armed) {
            Resolution::Prefix => {
                *prefix_armed = true;
                chrome.mode = Mode::Prefix;
            }
            Resolution::Action(Action::Quit) => return Ok(true),
            Resolution::Action(action) => {
                *prefix_armed = false;
                chrome.mode = Mode::Terminal;
                handle_live_action(workspace, chrome, action).await?;
            }
            Resolution::Unbound => {
                *prefix_armed = false;
                chrome.mode = Mode::Terminal;
                if let Some(pane_id) = chrome.focused_pane() {
                    send_live_input(workspace, pane_id, &input.bytes).await?;
                }
            }
        }
    } else if let Some(bytes) = text_bytes(event) {
        if let Some(pane_id) = chrome.focused_pane() {
            send_live_input(workspace, pane_id, &bytes).await?;
        }
    }
    Ok(false)
}

async fn handle_live_action(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    action: Action,
) -> Result<(), FrameError> {
    match action {
        Action::NewTerminal => spawn_live_terminal(workspace, chrome).await?,
        Action::CloseTerminal | Action::ClosePane => {
            if let Some(pane_id) = chrome.focused_pane() {
                terminate_live_terminal(workspace, pane_id).await?;
            }
        }
        Action::TakeControl | Action::TakeBack => {
            if let Some(pane_id) = chrome.focused_pane() {
                take_live_control(workspace, pane_id).await?;
            }
        }
        Action::ReleaseControl | Action::Detach => {
            if let Some(pane_id) = chrome.focused_pane() {
                release_live_control(workspace, pane_id).await?;
            }
        }
        Action::PreviousTerminal | Action::CyclePanePrevious => {
            focus_relative_live_pane(workspace, chrome, -1).await?;
        }
        Action::NextTerminal | Action::CyclePaneNext => {
            focus_relative_live_pane(workspace, chrome, 1).await?;
        }
        Action::SwitchTerminal(index) if index > 0 => {
            let pane_id = workspace
                .roster_terminal_ids()
                .get(usize::from(index - 1))
                .and_then(|terminal_id| workspace.pane_for_terminal(terminal_id));
            if let Some(pane_id) = pane_id {
                chrome.focus_pane(pane_id);
                focus_live_pane(workspace, pane_id).await?;
            }
        }
        _ => {}
    }
    Ok(())
}

async fn focus_relative_live_pane(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    delta: isize,
) -> Result<(), FrameError> {
    let pane_ids: Vec<_> = workspace
        .roster_terminal_ids()
        .iter()
        .filter_map(|terminal_id| workspace.pane_for_terminal(terminal_id))
        .collect();
    if pane_ids.is_empty() {
        return Ok(());
    }
    let current = chrome
        .focused_pane()
        .and_then(|pane_id| pane_ids.iter().position(|candidate| *candidate == pane_id))
        .unwrap_or(0);
    let next = (current as isize + delta).rem_euclid(pane_ids.len() as isize) as usize;
    chrome.focus_pane(pane_ids[next]);
    focus_live_pane(workspace, pane_ids[next]).await
}

async fn focus_live_pane(
    workspace: &mut Workspace<LiveDaemon>,
    pane_id: PaneId,
) -> Result<(), FrameError> {
    if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return Ok(());
    }
    let previous = workspace.focus.replace(pane_id);
    if let Some(previous) = previous.filter(|previous| *previous != pane_id) {
        release_live_control(workspace, previous).await?;
    }
    if !workspace.pane(pane_id).is_held() {
        take_live_control(workspace, pane_id).await?;
    }
    Ok(())
}

async fn take_live_control(
    workspace: &mut Workspace<LiveDaemon>,
    pane_id: PaneId,
) -> Result<(), FrameError> {
    if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return Ok(());
    }
    let pane = workspace.pane(pane_id);
    if !pane.is_live() {
        return Ok(());
    }
    let attachment_id = pane.attachment_id().to_string();
    let terminal_id = pane.terminal_id.clone();
    let reply = match workspace
        .daemon()
        .send(json!({
            "type": "terminal_take_control",
            "terminal_id": terminal_id,
            "attachment_id": attachment_id,
            "takeover": false,
        }))
        .await
    {
        Ok(reply) => reply,
        Err(error) => {
            retire_live_control(workspace, pane_id).await;
            return Err(FrameError::from(error));
        }
    };
    let generation = reply
        .get("lease_generation")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let granted = reply
        .get("granted")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let pending = {
        let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
        if generation < pane.lease_generation() {
            return Ok(());
        }
        pane.set_lease_generation(generation);
        pane.control = if granted {
            ControlState::Held
        } else {
            ControlState::Observe
        };
        pane.take_back = !granted;
        if granted {
            pane.pending_input.take()
        } else {
            pane.pending_input = None;
            None
        }
    };
    if let Some(data) = pending {
        send_live_write(workspace, pane_id, &data).await?;
    }
    Ok(())
}

async fn retire_live_control(workspace: &mut Workspace<LiveDaemon>, pane_id: PaneId) {
    let Some((terminal_id, attachment_id, _)) =
        workspace.retire_indeterminate_control(pane_id, Instant::now())
    else {
        return;
    };
    let _ = workspace
        .daemon()
        .notify(json!({
            "type": "terminal_detach",
            "request_id": uuid::Uuid::new_v4().to_string(),
            "terminal_id": terminal_id,
            "attachment_id": attachment_id,
        }))
        .await;
}

async fn release_live_control(
    workspace: &mut Workspace<LiveDaemon>,
    pane_id: PaneId,
) -> Result<(), FrameError> {
    if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return Ok(());
    }
    let pane = workspace.pane(pane_id);
    if !pane.is_live() || !pane.is_held() {
        return Ok(());
    }
    let message = json!({
        "type": "terminal_release_control",
        "terminal_id": pane.terminal_id,
        "attachment_id": pane.attachment_id(),
    });
    let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
    pane.control = ControlState::Observe;
    pane.take_back = false;
    workspace
        .daemon()
        .notify(message)
        .await
        .map_err(FrameError::from)
}

async fn send_live_input(
    workspace: &mut Workspace<LiveDaemon>,
    pane_id: PaneId,
    data: &[u8],
) -> Result<(), FrameError> {
    if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return Ok(());
    }
    if workspace.pane(pane_id).writable() {
        return send_live_write(workspace, pane_id, data).await;
    }
    let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
    if !pane.is_live() || pane.pending_input.is_some() {
        return Ok(());
    }
    pane.pending_input = Some(data.to_vec());
    take_live_control(workspace, pane_id).await
}

async fn send_live_write(
    workspace: &mut Workspace<LiveDaemon>,
    pane_id: PaneId,
    data: &[u8],
) -> Result<(), FrameError> {
    let message = {
        let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
        if !pane.writable() {
            return Ok(());
        }
        pane.client_write_seq += 1;
        pane.in_flight_write = Some(pane.client_write_seq);
        json!({
            "type": "terminal_input",
            "terminal_id": pane.terminal_id,
            "attachment_id": pane.attachment_id(),
            "data": String::from_utf8_lossy(data),
            "client_write_seq": pane.client_write_seq,
        })
    };
    match workspace.daemon().send(message).await {
        Ok(reply) => {
            apply_live_write_outcome(workspace, &reply);
            Ok(())
        }
        Err(error) => {
            let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
            pane.in_flight_write = None;
            pane.control = ControlState::UncertainReadOnly;
            Err(FrameError::from(error))
        }
    }
}

fn apply_live_write_outcome(workspace: &mut Workspace<LiveDaemon>, message: &Value) {
    let Some(attachment_id) = message.get("attachment_id").and_then(Value::as_str) else {
        return;
    };
    let outcome = message.get("outcome").and_then(Value::as_str).unwrap_or("");
    let reason = message.get("reason").and_then(Value::as_str).unwrap_or("");
    if let Some(pane) = workspace.pane_for_attachment_mut(attachment_id) {
        pane.in_flight_write = None;
        match outcome {
            "delivered" if pane.control != ControlState::LeaseLost => {
                pane.control = ControlState::Held;
            }
            "indeterminate" => pane.control = ControlState::UncertainReadOnly,
            "refused"
                if !matches!(
                    reason,
                    "write_seq_conflict" | "write_seq_expired" | "write_seq_capacity"
                ) =>
            {
                pane.control = ControlState::Observe;
            }
            _ => {}
        }
    }
}

async fn spawn_live_terminal(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
) -> Result<(), FrameError> {
    if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return Ok(());
    }
    let request = SpawnRequest {
        project_id: workspace.project_id().map(str::to_owned),
        ..SpawnRequest::default()
    };
    match workspace.daemon().spawn(request).await? {
        SpawnOutcome::Created { terminal_id, .. } => {
            workspace.pending_spawns.insert(terminal_id);
            workspace.fetch_roster().await?;
            workspace.attach_ready_panes().await?;
            sync_live_chrome(workspace, chrome);
        }
        SpawnOutcome::Refused { reason } => chrome.status_message = Some(reason),
    }
    Ok(())
}

async fn terminate_live_terminal(
    workspace: &mut Workspace<LiveDaemon>,
    pane_id: PaneId,
) -> Result<(), FrameError> {
    if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return Ok(());
    }
    let terminal_id = workspace.pane(pane_id).terminal_id.clone();
    workspace
        .panes
        .get_mut(&pane_id)
        .expect("pane exists")
        .terminating = true;
    match workspace.daemon().terminate(&terminal_id).await? {
        KillOutcome::Killed { .. } => workspace.fetch_roster().await?,
        KillOutcome::Refused { .. } => {
            workspace
                .panes
                .get_mut(&pane_id)
                .expect("pane exists")
                .terminating = false;
        }
    }
    Ok(())
}

async fn recv_workspace_frame(
    workspace: &mut Workspace<LiveDaemon>,
) -> Option<(
    PaneId,
    Result<gobby_terminal::protocol::ServerMessage, FrameError>,
)> {
    if workspace
        .panes
        .values()
        .all(|pane| pane.frame_source().is_none())
    {
        std::future::pending::<()>().await;
        return None;
    }
    let next = {
        let mut pending = FuturesUnordered::new();
        for (&pane_id, pane) in &mut workspace.panes {
            if let Some(source) = pane.frame_source_mut() {
                pending.push(async move { (pane_id, source.recv().await) });
            }
        }
        pending.next().await
    };
    if let Some((pane_id, Ok(message))) = &next {
        workspace.record_source_message(*pane_id, message);
    }
    next
}

fn render_live_workspace<B: Backend>(
    terminal: &mut Terminal<B>,
    workspace: &Workspace<LiveDaemon>,
    chrome: &mut Chrome,
) -> Result<(), FrameError> {
    terminal
        .draw(|frame| {
            chrome.compute_view(workspace, frame.area());
            let mut content = |frame: &mut ratatui::Frame<'_>, area, pane| {
                crate::views::grid::render(frame, area, workspace.pane(pane));
            };
            crate::ui::render_workspace_with(frame, workspace, chrome, &mut content);
        })
        .map(|_| ())
        .map_err(|error| FrameError::Other(error.to_string()))
}

async fn resize_live_workspace<B: Backend>(
    terminal: &mut Terminal<B>,
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
) -> Result<(), FrameError> {
    let area = terminal
        .size()
        .map_err(|error| FrameError::Other(error.to_string()))?;
    if area.width == 0 || area.height == 0 {
        return Ok(());
    }
    chrome.compute_view(workspace, area.into());
    let updates = match chrome.active_tab() {
        Some(tab) => chrome
            .view
            .pane_infos
            .iter()
            .filter_map(|info| {
                tab.slots
                    .get(&info.id)
                    .map(|pane_id| (*pane_id, info.inner_rect.height, info.inner_rect.width))
            })
            .collect::<Vec<_>>(),
        None => Vec::new(),
    };
    workspace.propagate_geometry(&updates).await
}
