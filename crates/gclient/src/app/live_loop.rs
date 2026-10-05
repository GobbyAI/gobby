//! Interactive Tokio loop for the authenticated live daemon transport.

use std::io::Write as _;
use std::path::Path;
use std::time::Duration;

use futures_util::stream::{FuturesUnordered, StreamExt};
use gobby_terminal::input::KeyboardProtocol;
use gobby_terminal::raw_input::RawInputEvent;
use ratatui::backend::Backend;
use ratatui::Terminal;
use tokio::sync::mpsc;
use tokio::time::Instant;

use crate::app::startup_stages::StartupStage;
use crate::copy_mode::{
    apply_text_read, copy_or_request_selection, route_mouse_selection, PASTE_MAX_BYTES,
};
use crate::daemon::{Daemon, DaemonError, DaemonEvent, EventReceiver, Generation, LiveDaemon};
use crate::frame_source::{FrameError, FrameSource};
use crate::key_input::{key_input, resolve_chord, text_bytes, Resolution};
use crate::startup::initial_project;
use crate::teardown::MouseCaptureSwitch;
use crate::ui::status::Toast;
use crate::ui::{Action, Chrome, Mode, WorkspaceView};

use super::attention::route_response_input;
use super::live::relist::{Relist, RelistFuture};
use super::live_attach::RecoveryFuture;
use super::run_loop::{
    shutdown, ReconnectAttempt, ReconnectFuture, ReconnectSupervisor, RENDER_TICK,
};
use super::sidebar_model::SidebarModel;
use super::{PaneId, SidebarFetch, SidebarFetchFuture, Workspace, WorkspaceModel};
use host_recovery::HostRecoveries;

mod actions;
pub(super) mod arrange;
mod control;
mod focus_hints;
pub(super) mod host_recovery;
pub(super) mod jobs;
mod jobs_apply;
pub(super) mod menu;
mod menu_bar;
pub(super) mod menu_dispatch;
pub(super) mod modal_input;
pub(super) mod mouse;
pub(super) mod orphans;
mod projection;
pub(super) mod projects;
mod reconnect;
mod render;
pub use projection::sync_live_chrome;
mod signals;
mod startup;
mod suspend;
mod terminal_location;
mod workspace_actions;
mod workspaces;

use actions::{apply_live_modal_outcome, apply_live_mouse_outcome, handle_live_action};
use control::{apply_control_outcome, apply_live_write_outcome, focus_live_pane, send_live_input};
use focus_hints::{offer_focus_hints, FocusMemo};
use jobs::{stage_live_geometry, LoopJobs};
use jobs_apply::apply_job_outcome;
use modal_input::{route_modal_key, ModalOutcome};
use mouse::{route_mouse, MouseOutcome};
use reconnect::{
    await_reconnect_job, begin_reconnect, handle_live_event, handle_reconnect_outcome,
    recv_daemon_event, settle_sidebar_banner, wait_for_reconnect,
};
use render::{cap_sidebar_to_window, pane_hit_map_stale, render_live_workspace};
use signals::{recv_exit_signal, recv_resize_signal, recv_suspend_signal};
use suspend::suspend_process;

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

    fn daemon_error(&self) -> Option<&DaemonError> {
        Workspace::<LiveDaemon>::daemon_error(self)
    }

    fn gobby_home(&self) -> Option<&Path> {
        self.gobby_home()
    }

    fn workspace_model(&self) -> Option<&WorkspaceModel> {
        Workspace::<LiveDaemon>::workspace_model(self)
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
    // Before any spawn this launch requests; the render tick keeps it current.
    daemon.set_terminal_theme(&(&chrome.terminal_theme()).into());
    let mut loop_error = None;
    let mut supervisor = ReconnectSupervisor::new();
    sync_live_chrome(workspace, chrome);
    let mut launch_pending = !workspace.daemon_ready() || workspace.workspace_model().is_none();

    let (_, fallback_events) = Daemon::subscribe(&daemon);
    let mut events = Some(workspace.event_rx.take().unwrap_or(fallback_events));
    let signals = signals::install(workspace, &mut loop_error);
    let mut exit_signals = signals.exit;
    let mut resize_signal = signals.resize;
    let mut suspend_signal = signals.suspend;
    let mut render_tick = tokio::time::interval(RENDER_TICK);
    render_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut next_frame_render_at = Instant::now();
    let mut system_theme_watcher = None;
    let mut system_theme_watch_attempted = false;
    let mut host_colors_due = true;
    let mut host_colors_asked_at = None;
    let mut prefix_armed = false;
    let mut reconnect_job = None;
    let mut startup_job = if launch_pending {
        startup::mark_running(chrome, StartupStage::DaemonHealth);
        Some(startup::connect(daemon.clone()))
    } else {
        None
    };
    let mut reconnect_stage: Option<(Generation, EventReceiver)> = None;
    let mut first_frame_pending = false;
    let mut sidebar_job: Option<SidebarFetchFuture> = None;
    let mut relist_job: Option<RelistFuture> = None;
    let mut recoveries: FuturesUnordered<RecoveryFuture> = FuturesUnordered::new();
    let mut host_recoveries = HostRecoveries::default();
    // Control replies come back on a channel rather than a single in-flight
    // slot: a grant still out for one pane must never hold up the grant the
    // pane someone just clicked is waiting for (#22573).
    let (control_tx, mut control_rx) = tokio::sync::mpsc::unbounded_channel();
    let mut sidebar_error_shown = false;
    // Focus hints and geometry run as jobs beside the loop (plan A1).
    let mut jobs = LoopJobs::new(workspace);

    // Draw once before the first select: input outranks the render tick, so
    // the earliest event, a click included, would otherwise route against an
    // empty hit map.
    cap_sidebar_to_window(terminal, chrome);
    if let Err(error) = render_live_workspace(terminal, workspace, chrome) {
        workspace.latch_exit(error.to_string());
        loop_error = Some(error);
    }
    if workspace.exit_reason().is_none() {
        stage_live_geometry(terminal, workspace, chrome, &mut jobs);
    }
    if !launch_pending {
        if let Some(pane_id) = chrome.focused_pane() {
            if let Err(error) = focus_live_pane(workspace, pane_id).await {
                chrome.notify(Toast::error(error.to_string()));
            }
        }
    }

    while workspace.exit_reason().is_none() {
        // The click and the keystroke only record the control request they
        // need; it is started here so neither ever waits on the daemon
        // (#22573).
        workspace.start_control_request(&control_tx);
        if !launch_pending && reconnect_stage.is_none() && relist_job.is_none() {
            workspace.refresh_relist();
            relist_job = workspace.start_relist();
        }
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
            Some(outcome) = jobs.rx.recv() => {
                apply_job_outcome(workspace, chrome, &mut jobs, outcome);
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
            result = async { startup_job.as_mut().expect("startup job").await }, if startup_job.is_some() => {
                startup_job = None;
                match result {
                    Ok(startup::StartupAnswer::Health(version)) => {
                        chrome.connection.daemon_version = version;
                        startup::mark_done(chrome, StartupStage::DaemonHealth);
                        startup::mark_running(chrome, StartupStage::WorkspaceAttach);
                        workspace.daemon_ready = true;
                        workspace.daemon_error = None;
                        startup_job = Some(startup::attach(daemon.clone(), workspace.attach_target().clone()));
                    }
                    Ok(startup::StartupAnswer::Attached(snapshot)) => {
                        workspace.apply_workspace_snapshot(snapshot);
                        // The daemon's snapshot is the baseline, not a local focus change.
                        jobs.focus = FocusMemo::new(workspace);
                        if reconnect_stage.is_none() {
                            startup::mark_done(chrome, StartupStage::WorkspaceAttach);
                            if workspace.project_id().is_none() {
                                let focused = workspace.workspace_model()
                                    .and_then(|model| model.workspace.focused_project_id.clone());
                                let project = initial_project(
                                    chrome.connection.launch_project.clone(),
                                    focused.as_deref(),
                                );
                                workspace.select_project(project);
                            }
                            startup::mark_running(chrome, StartupStage::Roster);
                        }
                        startup_job = Some(startup::roster(workspace));
                    }
                    Ok(startup::StartupAnswer::Roster(relist, attention, unresolved)) => {
                        workspace.apply_relist(relist);
                        workspace.install_unresolved_rows(unresolved);
                        workspace.install_attention(attention);
                        if let Some((generation, receiver)) = reconnect_stage.take() {
                            workspace.daemon_ready = true;
                            workspace.daemon_error = None;
                            workspace.queue_initial_sidebar_fetch();
                            sync_live_chrome(workspace, chrome);
                            recoveries.extend(workspace.start_due_attaches(Instant::now(), true));
                            if let Some(pane_id) = chrome.focused_pane() {
                                workspace.focus = Some(pane_id);
                                if !workspace.pane(pane_id).is_held()
                                    && !workspace.pane(pane_id).take_back
                                {
                                    // The loop sends this once the pane's new
                                    // attachment becomes live.
                                    workspace.request_control(pane_id, false);
                                }
                            }
                            events = Some(receiver);
                            supervisor.handshake_complete(generation);
                            chrome.connection.retry_at = None;
                        } else {
                            startup::mark_done(chrome, StartupStage::Roster);
                            startup::mark_running(chrome, StartupStage::FirstFrame);
                            first_frame_pending = true;
                            sync_live_chrome(workspace, chrome);
                            recoveries.extend(workspace.start_due_attaches(Instant::now(), true));
                            if let Some(pane_id) = chrome.focused_pane() {
                                if let Err(error) = focus_live_pane(workspace, pane_id).await {
                                    chrome.notify(Toast::error(error.to_string()));
                                }
                            }
                            if let Err(error) = render_live_workspace(terminal, workspace, chrome) {
                                workspace.latch_exit(error.to_string());
                                loop_error = Some(error);
                            }
                        }
                    }
                    Err(error) => {
                        if let Some((generation, _)) = reconnect_stage.take() {
                            workspace.observe_daemon_disconnect(generation, error.clone());
                            supervisor.handshake_failed(error);
                            chrome.connection.retry_at =
                                supervisor.next_attempt_at().map(Instant::into_std);
                        } else if matches!(
                            &error,
                            DaemonError::Unavailable { .. }
                                | DaemonError::GoingAway
                                | DaemonError::Timeout { .. }
                        ) {
                            begin_reconnect(workspace, chrome, &mut supervisor, &daemon, error);
                        } else {
                            let failure = FrameError::from(error);
                            workspace.latch_exit(failure.to_string());
                            loop_error = Some(failure);
                        }
                    }
                }
            }
            event = recv_daemon_event(&mut events) => {
                match event {
                    Ok(event) => {
                        if let Err(error) =
                            handle_live_event(workspace, chrome, &mut supervisor, event).await
                        {
                            begin_reconnect(workspace, chrome, &mut supervisor, &daemon, error);
                        }
                        if reconnect_job.is_none()
                            && reconnect_stage.is_none()
                            && supervisor.next_attempt_at().is_none()
                        {
                            recoveries.extend(workspace.start_due_attaches(Instant::now(), true));
                        }
                        sync_live_chrome(workspace, chrome);
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => {
                        if let Err(error) = workspace.apply_live_event(DaemonEvent::Lagged).await {
                            begin_reconnect(workspace, chrome, &mut supervisor, &daemon, error);
                        }
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Closed) => {
                        events = None;
                        begin_reconnect(
                            workspace,
                            chrome,
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
                    // A direct pane reconnects straight to its host first; its
                    // result is applied by pane identity and epoch, so a daemon
                    // reconnect cannot cancel it (#23076). Everything else, and
                    // every host-local failure, goes down the daemon path. The
                    // recovery's waits run beside the loop; its branch below
                    // applies each step (#22747).
                    if let Some((cancel, future)) =
                        workspace.begin_host_frame_recovery(pane_id, &error)
                    {
                        host_recoveries.push(pane_id, cancel, future);
                    } else {
                        match workspace.begin_frame_recovery(pane_id, &error) {
                            Ok(Some(recovery)) => recoveries.push(recovery),
                            Ok(None) => {}
                            Err(error) => chrome.notify(Toast::error(error.to_string())),
                        }
                    }
                }
                let now = Instant::now();
                if now >= next_frame_render_at {
                    next_frame_render_at = now + RENDER_TICK * 4;
                    if let Err(error) = render_live_workspace(terminal, workspace, chrome) {
                        workspace.latch_exit(error.to_string());
                        loop_error = Some(error);
                    }
                }
            }
            result = await_sidebar_job(&mut sidebar_job), if sidebar_job.is_some() => {
                sidebar_job = None;
                let error = match result {
                    Ok(fetch) => {
                        workspace.apply_sidebar_fetch(fetch);
                        None
                    }
                    Err(error) => {
                        workspace.requeue_failed_sidebar_fetch();
                        Some(error)
                    }
                };
                settle_sidebar_banner(chrome, &mut sidebar_error_shown, error.as_ref());
            }
            result = await_relist_job(&mut relist_job), if relist_job.is_some() => {
                relist_job = None;
                match result {
                    Ok(relist) => workspace.apply_relist(relist),
                    Err(error) => begin_reconnect(workspace, chrome, &mut supervisor, &daemon, error),
                }
                sync_live_chrome(workspace, chrome);
            }
            // `next` on an empty set is ready with nothing, not pending.
            Some(recovery) = recoveries.next(), if !recoveries.is_empty() => {
                match workspace.apply_frame_recovery(recovery) {
                    Ok(Some(next)) => recoveries.push(next),
                    Ok(None) => {}
                    Err(error) => chrome.notify(Toast::error(error.to_string())),
                }
                sync_live_chrome(workspace, chrome);
            }
            Some(recovery) = host_recoveries.next(), if !host_recoveries.is_empty() => {
                if let Some(next) = workspace.apply_host_recovery(recovery) {
                    recoveries.push(next);
                }
                sync_live_chrome(workspace, chrome);
            }
            result = await_reconnect_job(&mut reconnect_job), if reconnect_job.is_some() => {
                reconnect_job = None;
                // A refetch, relist or frame recovery begun on the old
                // connection has nothing to add; the reconcile re-attaches.
                sidebar_job = None;
                relist_job = None;
                recoveries.clear();
                workspace.abandon_frame_recoveries();
                let outcome = supervisor.complete_attempt(result);
                if launch_pending {
                    match outcome {
                        ReconnectAttempt::Reconnected(generation) => {
                            supervisor.handshake_complete(generation);
                            chrome.connection.retry_at = None;
                            workspace.daemon_ready = true;
                            workspace.daemon_error = None;
                            startup::mark_done(chrome, StartupStage::DaemonHealth);
                            startup::mark_running(chrome, StartupStage::WorkspaceAttach);
                            startup_job = Some(startup::attach(daemon.clone(), workspace.attach_target().clone()));
                        }
                        ReconnectAttempt::RetryScheduled { .. } | ReconnectAttempt::Idle => {}
                    }
                    chrome.connection.retry_at = supervisor.next_attempt_at().map(Instant::into_std);
                } else {
                    if let Some(stage) = handle_reconnect_outcome(
                        workspace,
                        chrome,
                        &mut events,
                        &mut supervisor,
                        outcome,
                    ) {
                        reconnect_stage = Some(stage);
                        startup_job = Some(startup::attach(
                            daemon.clone(),
                            workspace.attach_target().clone(),
                        ));
                    }
                }
                chrome.connection.retry_at = supervisor.next_attempt_at().map(Instant::into_std);
            }
            _ = wait_for_reconnect(supervisor.next_attempt_at()),
                if reconnect_job.is_none() && supervisor.next_attempt_at().is_some() =>
            {
                reconnect_job = supervisor.start_due_attempt(daemon.clone());
                chrome.connection.retry_at = supervisor.next_attempt_at().map(Instant::into_std);
            }
            // A resize redraws at the new size; the geometry pass after the
            // select then reads the rects that draw produced.
            _ = recv_resize_signal(&mut resize_signal) => {
                cap_sidebar_to_window(terminal, chrome);
                if let Err(error) = render_live_workspace(terminal, workspace, chrome) {
                    workspace.latch_exit(error.to_string());
                    loop_error = Some(error);
                }
            }
            _ = render_tick.tick() => {
                // System's ground is the hosting terminal's own colours: ask
                // for them on entering System, after each appearance flip,
                // and again whenever the last answer has aged out.
                host_colors_due |= !chrome.prefs.follows_system();
                if chrome.prefs.follows_system() {
                    if !system_theme_watch_attempted {
                        system_theme_watcher = dark_light::subscribe().ok();
                        system_theme_watch_attempted = true;
                    }
                    if let Some(watcher) = &system_theme_watcher {
                        for mode in watcher.try_iter() {
                            chrome.set_theme(if mode == dark_light::Mode::Light {
                                crate::theme::ThemeKind::Light
                            } else {
                                crate::theme::ThemeKind::Dark
                            });
                            host_colors_due = true;
                        }
                    }
                    super::theme_sync::query_host_colors_when_due(
                        &chrome.host_color_query,
                        &mut host_colors_asked_at,
                        std::mem::take(&mut host_colors_due),
                        std::time::Instant::now(),
                        &mut std::io::stdout(),
                    )?;
                } else if let Some(watcher) = &system_theme_watcher {
                    for _ in watcher.try_iter() {}
                }
                // New attachments and theme changes (toggle, menu, system)
                // all reach the panes' hosts here, and the next spawn carries
                // the same colours.
                let terminal_theme = (&chrome.terminal_theme()).into();
                workspace.daemon().set_terminal_theme(&terminal_theme);
                workspace.sync_terminal_themes(&terminal_theme).await;
                chrome.connection.now = std::time::Instant::now();
                chrome.ticker = chrome.ticker.wrapping_add(1);
                chrome.expire_toasts(std::time::Instant::now());
                workspace.submit_expired_detaches(&mut supervisor, Instant::now());
                // A due attach runs beside the loop; the recoveries branch
                // above applies it (#22747).
                if reconnect_job.is_none()
                    && reconnect_stage.is_none()
                    && supervisor.next_attempt_at().is_none()
                {
                    recoveries.extend(workspace.start_due_attaches(Instant::now(), false));
                }
                if chrome.sidebar.pinned || chrome.sidebar.overlay {
                    workspace.request_git_refresh_if_due();
                }
                if !launch_pending && reconnect_stage.is_none() {
                    workspace.request_roster_refresh_if_due();
                }
                // The refetch runs beside the loop; its branch above applies it.
                if !launch_pending && reconnect_stage.is_none() && sidebar_job.is_none() {
                    sidebar_job = workspace.start_sidebar_refetch();
                }
                // Rendering on every 16 ms timer tick rebuilds the full frame
                // while idle. The marquee advances only once per TICKER_STEP.
                if chrome
                    .ticker
                    .is_multiple_of(crate::ui::sidebar_rows::TICKER_STEP)
                {
                    if let Err(error) = render_live_workspace(terminal, workspace, chrome) {
                        workspace.latch_exit(error.to_string());
                        loop_error = Some(error);
                    }
                }
            }
        }
        if first_frame_pending
            && workspace.exit_reason().is_none()
            && workspace.daemon_ready()
            && startup_job.is_none()
            && reconnect_stage.is_none()
            && chrome
                .focused_pane()
                .is_none_or(|pane_id| workspace.pane(pane_id).first_frame_settled())
        {
            startup::mark_done(chrome, StartupStage::FirstFrame);
            if let Err(error) = render_live_workspace(terminal, workspace, chrome) {
                workspace.latch_exit(error.to_string());
                loop_error = Some(error);
            } else {
                workspace.queue_initial_sidebar_fetch();
                launch_pending = false;
                first_frame_pending = false;
            }
        }
        // Projection can replace pane slots while input is queued. Refresh the
        // drawn hit map before the next event uses it, independent of the timer.
        if workspace.exit_reason().is_none() && pane_hit_map_stale(chrome) {
            if let Err(error) = render_live_workspace(terminal, workspace, chrome) {
                workspace.latch_exit(error.to_string());
                loop_error = Some(error);
            }
        }
        // A pane replaced or closed while its host connect was in flight must
        // not come back and attach (#23076).
        host_recoveries.retain_existing(workspace);
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
        offer_focus_hints(workspace, chrome, &mut jobs);
        // Every shown live pane carries the geometry of its slot: the pass
        // keys on pane, rect and attachment, so a slot change, an attach
        // that completed, a transport fallback or a new terminal size each
        // send once, and a quiet iteration sends nothing.
        if workspace.exit_reason().is_none() {
            stage_live_geometry(terminal, workspace, chrome, &mut jobs);
        }
    }

    drop(reconnect_job.take());
    drop(sidebar_job.take());
    drop(relist_job.take());
    recoveries.clear();
    host_recoveries.clear();
    // A reply still in flight has nowhere to land: the exit latch is set, a
    // latched exit issues no further requests, and `shutdown` releases the
    // lease this client asked for either way. Waiting for it here would hang on
    // a daemon that is already gone, which is the common reason this loop is
    // exiting.
    control_rx.close();
    jobs.rx.close();
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

async fn await_sidebar_job(
    job: &mut Option<SidebarFetchFuture>,
) -> Result<SidebarFetch, DaemonError> {
    match job {
        Some(job) => job.as_mut().await,
        None => std::future::pending().await,
    }
}

async fn await_relist_job(job: &mut Option<RelistFuture>) -> Result<Relist, DaemonError> {
    match job {
        Some(job) => job.as_mut().await,
        None => std::future::pending().await,
    }
}

async fn route_live_input(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    event: &RawInputEvent,
    prefix_armed: &mut bool,
) -> Result<bool, FrameError> {
    if let RawInputEvent::HostDefaultColor { kind, color } = event {
        chrome.record_host_color(*kind, *color);
        return Ok(false);
    }
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
