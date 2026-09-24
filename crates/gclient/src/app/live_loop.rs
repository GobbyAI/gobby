//! Interactive Tokio loop for the authenticated live daemon transport.

use std::io::Write as _;
use std::path::Path;
use std::time::Duration;

use futures_util::stream::{FuturesUnordered, StreamExt};
use gobby_terminal::input::KeyboardProtocol;
use gobby_terminal::raw_input::RawInputEvent;
use ratatui::backend::Backend;
use ratatui::Terminal;
use serde_json::Value;
use tokio::sync::mpsc;
use tokio::time::Instant;

use crate::copy_mode::{
    apply_text_read, copy_or_request_selection, route_mouse_selection, PASTE_MAX_BYTES,
};
use crate::daemon::{Daemon, DaemonError, DaemonEvent, EventReceiver, Generation, LiveDaemon};
use crate::frame_source::{FrameError, FrameSource};
use crate::key_input::{key_input, resolve_chord, text_bytes, Resolution};
use crate::teardown::MouseCaptureSwitch;
use crate::ui::status::Toast;
use crate::ui::{Action, Chrome, Mode, WorkspaceView};

use super::attention::route_response_input;
use super::run_loop::{
    shutdown, ReconnectAttempt, ReconnectFuture, ReconnectSupervisor, RENDER_TICK,
};
use super::sidebar_model::SidebarModel;
use super::{PaneId, SidebarFetch, SidebarFetchFuture, Workspace, WorkspaceModel};

mod actions;
mod control;
pub(super) mod menu;
pub(super) mod modal_input;
pub(super) mod mouse;
pub(super) mod orphans;
mod projection;
pub(super) mod projects;
pub use projection::sync_live_chrome;
mod suspend;
mod workspace_actions;
mod workspaces;

use actions::{apply_live_modal_outcome, apply_live_mouse_outcome, handle_live_action};
use control::{apply_control_outcome, apply_live_write_outcome, focus_live_pane, send_live_input};
use modal_input::{route_modal_key, ModalOutcome};
use mouse::{route_mouse, MouseOutcome};
use projects::restore_focused;
use suspend::{suspend_process, SuspendSignal};
use workspace_actions::{send_focus_hints_if_changed, stored_focus};

const SHUTDOWN_DEADLINE: Duration = Duration::from_secs(2);

impl WorkspaceView for Workspace<LiveDaemon> {
    fn project_id(&self) -> Option<&str> {
        Workspace::<LiveDaemon>::project_id(self)
    }

    fn focused_project(&self) -> Option<&str> {
        Workspace::<LiveDaemon>::project_id(self)
    }

    fn sidebar(&self) -> &SidebarModel {
        Workspace::<LiveDaemon>::sidebar(self)
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

    fn gobby_home(&self) -> Option<&Path> {
        self.gobby_home()
    }

    fn workspace_model(&self) -> Option<&WorkspaceModel> {
        Workspace::<LiveDaemon>::workspace_model(self)
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
        loop {
            tokio::select! {
                _ = self.interrupt.recv() => return "SIGINT",
                _ = self.terminate.recv() => return "SIGTERM",
                // Registered so the default disposition (terminate) stays
                // off, then ignored: a hangup is not a reason to drop the
                // window, and a terminal that really went away still ends
                // the loop through input EOF or the failed draw.
                _ = self.hangup.recv() => {
                    tracing::info!(
                        lifecycle_stage = "sighup-ignored",
                        "SIGHUP ignored; the terminal is still attached"
                    );
                }
            }
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
    switch: &mut dyn MouseCaptureSwitch,
) -> Result<(), FrameError> {
    let daemon = workspace.daemon().clone();
    let mut loop_error = None;
    let mut supervisor = ReconnectSupervisor::new();
    // A daemon that is down at launch, or a first reconcile that fails, is
    // the supervisor's to retry: the window opens and waits, and the
    // restore of the focused rows runs after the first handshake instead.
    let launch_error = match reconcile_ready(workspace).await {
        Err(error) => Some(error),
        Ok(()) if daemon.ready() => None,
        Ok(()) => Some(
            daemon
                .last_error()
                .unwrap_or(DaemonError::Unavailable { retry_after: None }),
        ),
    };
    if let Some(error) = launch_error {
        begin_reconnect(workspace, &mut supervisor, &daemon, error);
    }
    sync_live_chrome(workspace, chrome);
    let mut launch_pending = !workspace.daemon_ready();
    if !launch_pending {
        if let Err(error) = restore_focused(workspace, chrome).await {
            chrome.notify(Toast::error(error.to_string()));
        }
    }
    if let Some(pane_id) = chrome.focused_pane() {
        if let Err(error) = focus_live_pane(workspace, pane_id).await {
            chrome.notify(Toast::error(error.to_string()));
        }
    }

    let (_, fallback_events) = Daemon::subscribe(&daemon);
    let mut events = Some(workspace.event_rx.take().unwrap_or(fallback_events));
    let mut exit_signals = install_exit_signals(workspace, &mut loop_error);
    let mut resize_signal = install_resize_signal(workspace, &mut loop_error);
    let mut suspend_signal = install_suspend_signal(workspace, &mut loop_error);
    let mut render_tick = tokio::time::interval(RENDER_TICK);
    render_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut prefix_armed = false;
    let mut reconnect_job = None;
    let mut sidebar_job: Option<SidebarFetchFuture> = None;
    // Control replies come back on a channel rather than a single in-flight
    // slot: a grant still out for one pane must never hold up the grant the
    // pane someone just clicked is waiting for (#22573).
    let (control_tx, mut control_rx) = tokio::sync::mpsc::unbounded_channel();
    let mut sidebar_error_shown = false;
    // The memo starts on the daemon's stored focus: the window opened on it,
    // so the first iteration reports nothing unless it shows otherwise.
    let mut last_focus_hints = stored_focus(workspace);

    // Draw once before the first select: input outranks the render tick, so
    // the earliest event, a click included, would otherwise route against an
    // empty hit map.
    if let Err(error) = render_live_workspace(terminal, workspace, chrome) {
        workspace.latch_exit(error.to_string());
        loop_error = Some(error);
    }
    let mut sent_geometry = Vec::new();
    if workspace.exit_reason().is_none() {
        if let Err(error) =
            resize_live_workspace(terminal, workspace, chrome, &mut sent_geometry).await
        {
            chrome.notify(Toast::error(error.to_string()));
        }
    }

    while workspace.exit_reason().is_none() {
        // The click and the keystroke only record the control request they
        // need; it is started here so neither ever waits on the daemon
        // (#22573).
        workspace.start_control_request(&control_tx);
        tokio::select! {
            biased;
            reason = recv_exit_signal(&mut exit_signals) => {
                workspace.latch_exit(reason);
            }
            _ = recv_suspend_signal(&mut suspend_signal) => {
                switch.suspend()?;
                let suspend_result = suspend_process();
                let resume_result = switch.resume();
                suspend_result?;
                resume_result?;
                if let Err(error) = render_live_workspace(terminal, workspace, chrome) {
                    workspace.latch_exit(error.to_string());
                    loop_error = Some(error);
                }
            }
            // Above input on purpose: `biased` stops at the first ready
            // branch, so a grant sitting below a busy keyboard would never be
            // polled, its request would never leave, and the keys queued for
            // it would wait forever (#22573).
            Some(outcome) = control_rx.recv() => {
                apply_control_outcome(workspace, chrome, outcome).await;
                sync_live_chrome(workspace, chrome);
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
                    Err(error) => chrome.notify(Toast::error(error.to_string())),
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
                if let Some((pane_id, Ok(gobby_terminal::protocol::ServerMessage::TextRead {
                    text,
                    ..
                }))) = &frame
                {
                    let mut output = std::io::stdout();
                    apply_text_read(chrome, *pane_id, text.clone(), &mut output)?;
                    output.flush()?;
                }
                if let Some((pane_id, Err(error))) = frame {
                    let mut deferred_input = Vec::new();
                    let mut probe_prefix = prefix_armed;
                    let recovery_outcome = {
                        let recovery = workspace.recover_live_frame_error(pane_id, &error);
                        tokio::pin!(recovery);
                        loop {
                            tokio::select! {
                                biased;
                                event = input.recv() => {
                                    let Some(event) = event else {
                                        break FrameRecovery::Exit("terminal input closed");
                                    };
                                    let exits = input_requests_exit(
                                        chrome,
                                        &event,
                                        &mut probe_prefix,
                                    );
                                    deferred_input.push(event);
                                    if exits {
                                        break FrameRecovery::Exit("quit");
                                    }
                                }
                                result = &mut recovery => {
                                    break FrameRecovery::Complete(result);
                                }
                            }
                        }
                    };
                    match recovery_outcome {
                        FrameRecovery::Exit(reason) => {
                            prefix_armed = false;
                            workspace.latch_exit(reason);
                        }
                        FrameRecovery::Complete(result) => {
                            if let Err(recovery_error) = result {
                                chrome.notify(Toast::error(recovery_error.to_string()));
                            }
                            for event in deferred_input {
                                match route_live_input(
                                    workspace,
                                    chrome,
                                    &event,
                                    &mut prefix_armed,
                                ).await {
                                    Ok(true) => {
                                        workspace.latch_exit("quit");
                                        break;
                                    }
                                    Ok(false) => {}
                                    Err(error) => {
                                        chrome.notify(Toast::error(error.to_string()));
                                    }
                                }
                            }
                        }
                    }
                }
                if let Err(error) = render_live_workspace(terminal, workspace, chrome) {
                    workspace.latch_exit(error.to_string());
                    loop_error = Some(error);
                }
            }
            result = await_sidebar_job(&mut sidebar_job), if sidebar_job.is_some() => {
                sidebar_job = None;
                let error = match result {
                    Ok(fetch) => {
                        workspace.apply_sidebar_fetch(fetch);
                        None
                    }
                    Err(error) => Some(error),
                };
                settle_sidebar_banner(chrome, &mut sidebar_error_shown, error.as_ref());
            }
            result = await_reconnect_job(&mut reconnect_job), if reconnect_job.is_some() => {
                reconnect_job = None;
                // A refetch begun on the old connection has nothing to add.
                sidebar_job = None;
                let outcome = supervisor.complete_attempt(result);
                handle_reconnect_outcome(
                    workspace,
                    chrome,
                    &mut events,
                    &mut supervisor,
                    &mut launch_pending,
                    outcome,
                ).await;
            }
            _ = wait_for_reconnect(supervisor.next_attempt_at()),
                if reconnect_job.is_none() && supervisor.next_attempt_at().is_some() =>
            {
                reconnect_job = supervisor.start_due_attempt(daemon.clone());
            }
            // A resize redraws at the new size; the geometry pass after the
            // select then reads the rects that draw produced.
            _ = recv_resize_signal(&mut resize_signal) => {
                if let Err(error) = render_live_workspace(terminal, workspace, chrome) {
                    workspace.latch_exit(error.to_string());
                    loop_error = Some(error);
                }
            }
            _ = render_tick.tick() => {
                chrome.ticker = chrome.ticker.wrapping_add(1);
                chrome.expire_toasts(std::time::Instant::now());
                workspace.submit_expired_detaches(&mut supervisor, Instant::now());
                if workspace.attach_retry_due(Instant::now()) {
                    if let Err(error) = workspace.attach_ready_panes().await {
                        chrome.notify(Toast::error(error.to_string()));
                    }
                    sync_live_chrome(workspace, chrome);
                }
                if chrome.sidebar.pinned {
                    workspace.request_git_refresh_if_due();
                }
                workspace.request_roster_refresh_if_due();
                // The refetch runs beside the loop; its branch above applies it.
                if sidebar_job.is_none() {
                    sidebar_job = workspace.start_sidebar_refetch();
                }
                if let Err(error) = render_live_workspace(terminal, workspace, chrome) {
                    workspace.latch_exit(error.to_string());
                    loop_error = Some(error);
                }
            }
        }
        // Again after the event, not only before it: the event just handled is
        // usually the click or key that asked for the grant, and an event that
        // also ends the loop gets no next iteration to start it in (#22573).
        workspace.start_control_request(&control_tx);
        // The settings toggle only records the wish; the terminal flag is
        // flipped here, outside any borrow of the chrome.
        if let Some(on) = chrome.pending_mouse_capture.take() {
            if let Err(error) = switch.set_mouse_capture(on) {
                chrome.notify(Toast::error(error.to_string()));
            }
        }
        if let Err(error) =
            send_focus_hints_if_changed(workspace, chrome, &mut last_focus_hints).await
        {
            chrome.notify(Toast::error(error.to_string()));
        }
        // Every shown live pane carries the geometry of its slot: the pass
        // keys on pane, rect and attachment, so a slot change, an attach
        // that completed, a transport fallback or a new terminal size each
        // send once, and a quiet iteration sends nothing.
        if workspace.exit_reason().is_none() {
            if let Err(error) =
                resize_live_workspace(terminal, workspace, chrome, &mut sent_geometry).await
            {
                chrome.notify(Toast::error(error.to_string()));
            }
        }
    }

    drop(reconnect_job.take());
    drop(sidebar_job.take());
    // A reply still in flight has nowhere to land: the exit latch is set, a
    // latched exit issues no further requests, and `shutdown` releases the
    // lease this client asked for either way. Waiting for it here would hang on
    // a daemon that is already gone, which is the common reason this loop is
    // exiting.
    control_rx.close();
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

enum FrameRecovery {
    Complete(Result<(), FrameError>),
    Exit(&'static str),
}

fn input_requests_exit(chrome: &Chrome, event: &RawInputEvent, prefix_armed: &mut bool) -> bool {
    if chrome.mode == Mode::Respond {
        return false;
    }
    let Some(input) = key_input(event, KeyboardProtocol::Legacy) else {
        return false;
    };
    match resolve_chord(&chrome.keymap, chrome.mode, &input.key, *prefix_armed) {
        Resolution::Prefix => {
            *prefix_armed = true;
            false
        }
        Resolution::Action(Action::Quit) => true,
        Resolution::Action(_) | Resolution::Unbound => {
            *prefix_armed = false;
            false
        }
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

fn install_suspend_signal(
    workspace: &mut Workspace<LiveDaemon>,
    loop_error: &mut Option<FrameError>,
) -> Option<SuspendSignal> {
    match SuspendSignal::new() {
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

async fn recv_suspend_signal(signal: &mut Option<SuspendSignal>) {
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

async fn await_sidebar_job(
    job: &mut Option<SidebarFetchFuture>,
) -> Result<SidebarFetch, DaemonError> {
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
    drop(supervisor.request(generation));
    workspace.observe_daemon_disconnect(generation, error);
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
    launch_pending: &mut bool,
    outcome: ReconnectAttempt,
) {
    match outcome {
        ReconnectAttempt::Reconnected(generation) => match reconcile_ready(workspace).await {
            Ok(()) => {
                supervisor.handshake_complete(generation);
                let (_, fallback) = Daemon::subscribe(workspace.daemon());
                *events = Some(workspace.event_rx.take().unwrap_or(fallback));
                sync_live_chrome(workspace, chrome);
                // The launch that found the daemon down skipped the restore
                // of its focused rows; the first handshake is where it runs.
                if std::mem::take(launch_pending) {
                    if let Err(error) = restore_focused(workspace, chrome).await {
                        chrome.notify(Toast::error(error.to_string()));
                    }
                }
                if let Some(pane_id) = chrome.focused_pane() {
                    if let Err(error) = focus_live_pane(workspace, pane_id).await {
                        chrome.notify(Toast::error(error.to_string()));
                    }
                }
            }
            Err(error) => {
                workspace.observe_daemon_disconnect(generation, error.clone());
                supervisor.handshake_failed(error);
            }
        },
        ReconnectAttempt::RetryScheduled { .. } | ReconnectAttempt::Idle => {}
    }
}

async fn route_live_input(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    event: &RawInputEvent,
    prefix_armed: &mut bool,
) -> Result<bool, FrameError> {
    if let RawInputEvent::Paste(text) = event {
        if let Some(pane_id) = chrome.focused_pane() {
            if text.len() > PASTE_MAX_BYTES {
                workspace
                    .panes
                    .get_mut(&pane_id)
                    .expect("pane exists")
                    .status_message = Some("Paste too large.".into());
            } else if workspace.pane(pane_id).copy_search {
                workspace
                    .panes
                    .get_mut(&pane_id)
                    .expect("pane exists")
                    .search_buffer
                    .push_str(text);
            } else {
                send_live_input(workspace, chrome, pane_id, text.as_bytes(), true).await?;
            }
        }
        return Ok(false);
    }
    if let RawInputEvent::Mouse(mouse) = event {
        let outcome = route_mouse(&*workspace, chrome, mouse);
        if outcome != MouseOutcome::Ignore {
            return apply_live_mouse_outcome(workspace, chrome, outcome).await;
        }
    }
    if route_mouse_selection(workspace, chrome, event) {
        let mut output = std::io::stdout();
        copy_or_request_selection(workspace, chrome, &mut output).await?;
        output.flush()?;
        chrome.mode = Mode::Terminal;
        return Ok(false);
    }
    let protocol = chrome
        .focused_pane()
        .map(|pane_id| workspace.pane(pane_id).keyboard_protocol())
        .unwrap_or(KeyboardProtocol::Legacy);
    if let Some(input) = key_input(event, protocol) {
        // Any keypress clears the toast stack (D3); the alert log keeps them.
        chrome.dismiss_toasts();
        if chrome.mode == Mode::Respond {
            *prefix_armed = false;
            route_response_input(workspace, chrome, &input.key)
                .await
                .map_err(FrameError::from)?;
            return Ok(false);
        }
        let outcome = route_modal_key(&*workspace, chrome, &input);
        if outcome != ModalOutcome::Passthrough {
            *prefix_armed = false;
            return apply_live_modal_outcome(workspace, chrome, outcome).await;
        }
        match resolve_chord(&chrome.keymap, chrome.mode, &input.key, *prefix_armed) {
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
                    send_live_input(workspace, chrome, pane_id, &input.bytes, false).await?;
                }
            }
        }
    } else if let Some(bytes) = text_bytes(event) {
        if let Some(pane_id) = chrome.focused_pane() {
            send_live_input(workspace, chrome, pane_id, &bytes, false).await?;
        }
    }
    Ok(false)
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
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
) -> Result<(), FrameError> {
    workspace.rebuild_sidebar();
    let workspace = &*workspace;
    terminal
        .draw(|frame| {
            chrome.compute_view(workspace, frame.area());
            // Read focus out before the closure exists: capturing `chrome`
            // inside it would borrow across the `apply_hits` below.
            let focused = chrome.focused_pane();
            let mut content = |frame: &mut ratatui::Frame<'_>, area, pane| {
                crate::views::grid::render(
                    frame,
                    area,
                    workspace.pane(pane),
                    focused == Some(pane),
                );
            };
            let hits = crate::ui::render_workspace_with(frame, workspace, chrome, &mut content);
            chrome.apply_hits(hits);
        })
        .map(|_| ())
        .map_err(|error| FrameError::Other(error.to_string()))
}

/// One entry of the geometry pass: a shown pane, its inner rect and the
/// attachment it was sized on (empty while the pane is not live).
type ShownGeometry = (PaneId, u16, u16, String);

/// Send every shown live pane the geometry it does not hold yet. `sent` is
/// what the previous pass sent, so an unchanged pane costs nothing while a
/// new rect, a new attachment or a pane that just went live is sized. The
/// rects come from `chrome.view` as the last draw left it: recomputing the
/// view here would drop the hit areas that draw recorded.
async fn resize_live_workspace<B: Backend>(
    terminal: &mut Terminal<B>,
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &Chrome,
    sent: &mut Vec<ShownGeometry>,
) -> Result<(), FrameError> {
    let area = terminal
        .size()
        .map_err(|error| FrameError::Other(error.to_string()))?;
    if area.width == 0 || area.height == 0 {
        return Ok(());
    }
    let shown: Vec<ShownGeometry> = match chrome.active_tab() {
        Some(tab) => chrome
            .view
            .pane_infos
            .iter()
            .filter_map(|info| {
                let pane_id = *tab.slots.get(&info.id)?;
                let pane = workspace.panes.get(&pane_id)?;
                let attachment = if pane.is_live() {
                    pane.attachment_id().to_string()
                } else {
                    String::new()
                };
                Some((
                    pane_id,
                    info.inner_rect.height,
                    info.inner_rect.width,
                    attachment,
                ))
            })
            .collect(),
        None => Vec::new(),
    };
    let updates: Vec<(PaneId, u16, u16)> = shown
        .iter()
        .filter(|entry| !entry.3.is_empty() && !sent.contains(entry))
        .map(|&(pane_id, rows, cols, _)| (pane_id, rows, cols))
        .collect();
    *sent = shown;
    if updates.is_empty() {
        return Ok(());
    }
    workspace.propagate_geometry(&updates).await
}

/// One toast per sidebar refetch outage: the first failure raises it, later
/// ones stay quiet, and a success re-arms it.
fn settle_sidebar_banner(chrome: &mut Chrome, shown: &mut bool, error: Option<&DaemonError>) {
    match error {
        Some(error) if !*shown => {
            chrome.notify(Toast::error(error.to_string()));
            *shown = true;
        }
        Some(_) => {}
        None => *shown = false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sidebar_banner_raises_one_toast_per_outage() {
        let mut chrome = Chrome::dark();
        let mut shown = false;
        let timeout = DaemonError::timeout("GET /api/terminals");
        settle_sidebar_banner(&mut chrome, &mut shown, Some(&timeout));
        settle_sidebar_banner(&mut chrome, &mut shown, Some(&timeout));
        assert_eq!(chrome.toasts.len(), 1);
        assert_eq!(
            chrome.toasts[0].toast.title,
            "Daemon did not answer GET /api/terminals in time."
        );
        assert!(shown);
        settle_sidebar_banner(&mut chrome, &mut shown, None);
        assert!(!shown);
        settle_sidebar_banner(&mut chrome, &mut shown, Some(&timeout));
        assert_eq!(chrome.alert_log.len(), 2, "the next outage raises again");
    }
}
