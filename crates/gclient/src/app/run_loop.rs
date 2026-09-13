//! Tokio event loop and reconnect ownership for the live client.

use std::future::Future;
use std::pin::Pin;
use std::time::Duration;

use futures_util::stream::{FuturesUnordered, StreamExt};
use gobby_terminal::input::KeyboardProtocol;
use gobby_terminal::raw_input::RawInputEvent;
use ratatui::backend::Backend;
use ratatui::Terminal;
use tokio::sync::mpsc;
use tokio::sync::oneshot;
use tokio::time::Instant;

pub use crate::teardown::shutdown;

use crate::copy_mode::{copy_selection, route_paste_event};
use crate::daemon::{Daemon, DaemonError, Generation};
use crate::frame_source::{FrameError, FrameSource};
use gobby_terminal::protocol::ClientMessage;
use serde_json::{json, Value};

use super::live_loop::menu::{apply_local_menu_action, ContextMenuKind, MenuAction};
use super::live_loop::modal_input::apply_rename;
use super::{route_modal_key, route_mouse, ModalOutcome, MouseOutcome};
use super::{PaneId, Workspace};
use crate::daemon::{DaemonEvent, ScriptedDaemon};
use crate::key_input::{key_input, resolve_chord, text_bytes, Resolution};
use crate::ui::chrome::attention_pane;
use crate::ui::{Action, Chrome, Mode};

/// The steady render cadence used by both the real loop and paused-clock tests.
pub const RENDER_TICK: Duration = Duration::from_millis(16);

/// Fixed reconnect episode: one immediate attempt and four delayed attempts.
pub const RECONNECT_DELAYS: [Duration; 4] = [
    Duration::from_millis(250),
    Duration::from_millis(500),
    Duration::from_secs(1),
    Duration::from_secs(2),
];

pub const MIN_RETRY_AFTER: Duration = Duration::from_millis(250);
pub const MAX_RETRY_AFTER: Duration = Duration::from_secs(4);
const SHUTDOWN_DEADLINE: Duration = Duration::from_secs(2);

/// Scripted carrier for the real select loop used by integration tests.
pub async fn run_scripted_loop<B: Backend>(
    workspace: &mut Workspace<ScriptedDaemon>,
    terminal: &mut Terminal<B>,
    chrome: &mut Chrome,
    mut input: mpsc::Receiver<RawInputEvent>,
) -> Result<(), FrameError> {
    let (_, mut events) = Daemon::subscribe(workspace.daemon());
    let mut render_tick = tokio::time::interval(RENDER_TICK);
    render_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut prefix_armed = false;

    loop {
        tokio::select! {
            biased;
            event = input.recv() => {
                let Some(event) = event else {
                    workspace.latch_exit("terminal input closed");
                    break;
                };
                if route_scripted_input(workspace, chrome, &event, &mut prefix_armed)? {
                    workspace.latch_exit("quit");
                    break;
                }
            }
            event = events.recv() => {
                match event {
                    Ok(DaemonEvent::Message(message)) => workspace.apply_ws(&message)?,
                    Ok(DaemonEvent::Disconnected { generation, error }) => {
                        workspace.observe_daemon_disconnect(generation, error);
                    }
                    Ok(_) => {}
                    Err(_) => {
                        workspace.latch_exit("daemon event stream closed");
                        break;
                    }
                }
            }
            frame = recv_scripted_frame(workspace) => {
                if let Some((pane, Err(_))) = frame {
                    if let Some(pane) = workspace.panes.get_mut(&pane) {
                        let _ = pane.take_frame_source();
                    }
                }
                render_workspace(terminal, workspace, chrome)?;
            }
            _ = render_tick.tick() => {
                render_workspace(terminal, workspace, chrome)?;
            }
        }
    }
    let daemon = workspace.daemon().clone();
    shutdown(workspace, daemon, Instant::now() + SHUTDOWN_DEADLINE).await?;
    Ok(())
}

fn route_scripted_input(
    workspace: &mut Workspace,
    chrome: &mut Chrome,
    event: &RawInputEvent,
    prefix_armed: &mut bool,
) -> Result<bool, FrameError> {
    if let RawInputEvent::Mouse(mouse) = event {
        let outcome = route_mouse(&*workspace, chrome, mouse);
        if outcome != MouseOutcome::Ignore {
            return apply_scripted_mouse_outcome(workspace, chrome, outcome);
        }
    }
    if route_paste_event(workspace, chrome, event)
        .map_err(|error| FrameError::Other(error.to_string()))?
    {
        return Ok(false);
    }
    if let Some(input) = key_input(event, KeyboardProtocol::Legacy) {
        let outcome = route_modal_key(&*workspace, chrome, &input);
        if outcome != ModalOutcome::Passthrough {
            *prefix_armed = false;
            return apply_scripted_modal_outcome(workspace, chrome, outcome);
        }
        match resolve_chord(&chrome.keymap, chrome.mode, &input.key, *prefix_armed) {
            Resolution::Prefix => {
                *prefix_armed = true;
                chrome.mode = Mode::Prefix;
            }
            Resolution::Action(action) => {
                *prefix_armed = false;
                if apply_scripted_action(chrome, action) {
                    return Ok(true);
                }
            }
            Resolution::Unbound => {
                *prefix_armed = false;
                chrome.mode = Mode::Terminal;
                if let Some(pane) = chrome.focused_pane() {
                    workspace
                        .send_input(pane, &input.bytes)
                        .map_err(|error| FrameError::Other(error.to_string()))?;
                }
            }
        }
    } else if let Some(bytes) = text_bytes(event) {
        if let Some(pane) = chrome.focused_pane() {
            workspace
                .send_input(pane, &bytes)
                .map_err(|error| FrameError::Other(error.to_string()))?;
        }
    }
    Ok(false)
}

/// Apply what `route_mouse` decided to the scripted workspace: chrome, focus,
/// roster order, pane input and scrollback only, since the scripted daemon
/// has no terminals to spawn, no prompts to answer and no desktop to open
/// links on. Returns whether the client should exit, like the key router.
fn apply_scripted_mouse_outcome(
    workspace: &mut Workspace,
    chrome: &mut Chrome,
    outcome: MouseOutcome,
) -> Result<bool, FrameError> {
    match outcome {
        // The scripted daemon has no terminal to close, as with the key path.
        MouseOutcome::Handled
        | MouseOutcome::Ignore
        | MouseOutcome::Spawn { .. }
        | MouseOutcome::OpenLink(_)
        | MouseOutcome::Confirm(_) => {}
        MouseOutcome::Focus { pane, observe_only } => {
            scripted_focus(workspace, chrome, pane, observe_only)?;
        }
        MouseOutcome::Action(action) => return Ok(apply_scripted_action(chrome, action)),
        MouseOutcome::Menu { kind, action } => {
            return apply_scripted_menu_action(workspace, chrome, kind, action);
        }
        MouseOutcome::Write { pane, bytes } => workspace
            .send_input(pane, &bytes)
            .map_err(|error| FrameError::Other(error.to_string()))?,
        MouseOutcome::FocusWrite { pane, bytes } => {
            chrome.focus_pane(pane);
            workspace
                .focus_pane(pane)
                .map_err(|error| FrameError::Other(error.to_string()))?;
            workspace
                .send_input(pane, &bytes)
                .map_err(|error| FrameError::Other(error.to_string()))?;
        }
        MouseOutcome::Scroll { pane, rows } => workspace.set_scroll_offset(pane, rows)?,
        MouseOutcome::FocusProject(project_id) => {
            scripted_focus_project(workspace, chrome, &project_id)
        }
        MouseOutcome::FocusAgent(entry_id) => {
            if let Some(pane) = attention_pane(&*workspace, &entry_id) {
                chrome.reveal_pane(pane, workspace.pane(pane).display_name());
                scripted_focus(workspace, chrome, pane, true)?;
            }
        }
        // The scripted daemon spawns nothing, so a worktree has no shell to open.
        MouseOutcome::OpenWorktree(_) => {}
        // The scripted loop has no terminal to reach: keep the text, skip OSC 52.
        MouseOutcome::Copy => {
            copy_selection(workspace, chrome, &mut std::io::sink())?;
        }
    }
    Ok(false)
}

/// The scripted loop's project focus: the workspace follows the sidebar and
/// the chrome swaps to the project's tab set.
fn scripted_focus_project(workspace: &mut Workspace, chrome: &mut Chrome, project_id: &str) {
    workspace.select_project(project_id);
    chrome.project_tabs.focus(project_id);
}

/// The scripted loop's action effects are chrome-only. Returns true on `Quit`.
fn apply_scripted_action(chrome: &mut Chrome, action: Action) -> bool {
    match action {
        Action::Quit => return true,
        Action::CopyMode => chrome.mode = Mode::Copy,
        _ => chrome.mode = Mode::Terminal,
    }
    false
}

/// Focus `pane` on the chrome and the workspace; `observe_only` moves focus
/// without taking control, releasing the previous pane's lease instead.
fn scripted_focus(
    workspace: &mut Workspace,
    chrome: &mut Chrome,
    pane: PaneId,
    observe_only: bool,
) -> Result<(), FrameError> {
    chrome.focus_pane(pane);
    if observe_only {
        if let Some(previous) = workspace.focus.filter(|previous| *previous != pane) {
            workspace
                .release_control(previous)
                .map_err(|error| FrameError::Other(error.to_string()))?;
        }
        workspace.focus = Some(pane);
    } else {
        workspace
            .focus_pane(pane)
            .map_err(|error| FrameError::Other(error.to_string()))?;
    }
    Ok(())
}

/// A context menu item in the scripted loop: a keymap action runs as its
/// chord would once the menu's pane (observed) or tab is the active one, the
/// chrome-only items apply directly, and `respond` and the sidebar rows have
/// nothing to reach here.
fn apply_scripted_menu_action(
    workspace: &mut Workspace,
    chrome: &mut Chrome,
    kind: ContextMenuKind,
    action: MenuAction,
) -> Result<bool, FrameError> {
    let MenuAction::Act(action) = action else {
        apply_local_menu_action(workspace, chrome, &action);
        return Ok(false);
    };
    match kind {
        ContextMenuKind::Pane(pane) if chrome.focused_pane() != Some(pane) => {
            scripted_focus(workspace, chrome, pane, true)?;
        }
        ContextMenuKind::Tab(index) if index != chrome.tabs().active_tab => {
            if let Some(pane) = chrome
                .tabs()
                .tabs
                .get(index)
                .and_then(|tab| tab.focused_pane())
            {
                scripted_focus(workspace, chrome, pane, false)?;
            }
        }
        _ => {}
    }
    Ok(apply_scripted_action(chrome, action))
}

/// Apply a modal outcome on the scripted carrier: focus and renames land on
/// the workspace, actions go through `apply_scripted_action`, and a confirmed
/// close is a no-op like the other terminal-reaching actions here.
fn apply_scripted_modal_outcome(
    workspace: &mut Workspace,
    chrome: &mut Chrome,
    outcome: ModalOutcome,
) -> Result<bool, FrameError> {
    match outcome {
        ModalOutcome::Consumed
        | ModalOutcome::Close
        | ModalOutcome::Passthrough
        | ModalOutcome::Confirm(_)
        | ModalOutcome::InitProject(_)
        | ModalOutcome::CreateWorktree { .. }
        | ModalOutcome::RemoveWorktree(_)
        | ModalOutcome::DestroyOrphans(_) => {}
        ModalOutcome::Focus(pane) => {
            chrome.focus_pane(pane);
            workspace
                .focus_pane(pane)
                .map_err(|error| FrameError::Other(error.to_string()))?;
        }
        ModalOutcome::FocusTerminal(terminal_id) => {
            if let Some(pane) = workspace.pane_for_terminal(&terminal_id) {
                chrome.reveal_pane(pane, workspace.pane(pane).display_name());
                scripted_focus(workspace, chrome, pane, true)?;
            }
        }
        ModalOutcome::FocusProject(project_id) => {
            scripted_focus_project(workspace, chrome, &project_id);
        }
        ModalOutcome::OpenWorktree(_) => {}
        ModalOutcome::Action(action) => return Ok(apply_scripted_action(chrome, action)),
        ModalOutcome::Commit(kind, value) => apply_rename(workspace, chrome, kind, value),
        ModalOutcome::Menu { kind, action } => {
            return apply_scripted_menu_action(workspace, chrome, kind, action);
        }
    }
    Ok(false)
}

async fn recv_scripted_frame(
    workspace: &mut Workspace,
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

fn render_workspace<B: Backend>(
    terminal: &mut Terminal<B>,
    workspace: &mut Workspace,
    chrome: &mut Chrome,
) -> Result<(), FrameError> {
    workspace.rebuild_sidebar();
    let workspace = &*workspace;
    terminal
        .draw(|frame| {
            chrome.compute_view(workspace, frame.area());
            let mut content = |frame: &mut ratatui::Frame<'_>, area, pane| {
                crate::views::grid::render(frame, area, workspace.pane(pane));
            };
            let hits = crate::ui::render_workspace_with(frame, workspace, chrome, &mut content);
            chrome.apply_hits(hits);
        })
        .map(|_| ())
        .map_err(|error| FrameError::Other(error.to_string()))
}

impl<D: Daemon> Workspace<D> {
    pub async fn propagate_geometry(
        &mut self,
        updates: &[(PaneId, u16, u16)],
    ) -> Result<(), FrameError> {
        self.ensure_requests_allowed()
            .map_err(|error| FrameError::Other(error.to_string()))?;
        let mut coalesced: Vec<(PaneId, u16, u16)> = Vec::new();
        for &(pane_id, rows, cols) in updates {
            if rows == 0 || cols == 0 {
                continue;
            }
            if let Some(update) = coalesced.iter_mut().find(|update| update.0 == pane_id) {
                *update = (pane_id, rows, cols);
            } else {
                coalesced.push((pane_id, rows, cols));
            }
        }
        for (pane_id, rows, cols) in coalesced {
            let resize = {
                let Some(pane) = self.panes.get_mut(&pane_id) else {
                    continue;
                };
                if !pane.is_live() {
                    continue;
                }
                pane.viewport = (rows, cols);
                let Some(source) = pane.frame_source_mut() else {
                    continue;
                };
                source
                    .send(&ClientMessage::SetViewport { rows, cols })
                    .await?;
                // Decision 14: every live pane claims its size as the gclient
                // viewer, whatever its backend or hold. A refusal comes back
                // as `terminal_resize_result` and re-marks the owner.
                pane.sized_by = None;
                json!({
                    "type": "terminal_resize",
                    "request_id": uuid::Uuid::new_v4().to_string(),
                    "terminal_id": pane.terminal_id,
                    "attachment_id": pane.attachment_id(),
                    "lease_generation": pane.lease_generation(),
                    "rows": rows,
                    "cols": cols,
                    "viewer": "gclient",
                })
            };
            self.daemon
                .notify(resize)
                .await
                .map_err(|error| FrameError::Other(error.to_string()))?;
        }
        Ok(())
    }

    /// Record who the daemon says sizes a pane's terminal: a refused
    /// `terminal_resize` names the owning viewer; anything else clears it.
    pub(super) fn note_resize_result(&mut self, message: &Value) {
        let Some(pane) = message
            .get("attachment_id")
            .and_then(Value::as_str)
            .and_then(|attachment| self.pane_for_attachment_mut(attachment))
        else {
            return;
        };
        pane.sized_by = match message.get("applied").and_then(Value::as_bool) {
            Some(false) => Some(
                message
                    .get("owner_viewer")
                    .and_then(Value::as_str)
                    .unwrap_or("another viewer")
                    .to_string(),
            ),
            _ => None,
        };
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReconnectAttempt {
    Reconnected(Generation),
    RetryScheduled { delay: Duration },
    Exhausted(DaemonError),
    Idle,
}

pub type ReconnectFuture = Pin<Box<dyn Future<Output = Result<Generation, DaemonError>> + 'static>>;

#[derive(Debug)]
enum ReconnectPhase {
    ReadyAt(Instant),
    AwaitingHandshake,
}

#[derive(Debug)]
struct ReconnectEpisode {
    observed: Generation,
    attempts: usize,
    phase: ReconnectPhase,
    waiters: Vec<oneshot::Sender<Result<Generation, DaemonError>>>,
    /// Unexpected losses spend the `RECONNECT_DELAYS` budget and then exit.
    /// A deliberate daemon stop or restart (`DaemonError::GoingAway`) keeps
    /// retrying at the last delay until the daemon returns (#22002).
    bounded: bool,
}

fn reconnect_delay(attempts: usize) -> Duration {
    RECONNECT_DELAYS[attempts.saturating_sub(1).min(RECONNECT_DELAYS.len() - 1)]
}

#[derive(Debug, Default)]
pub struct ReconnectSupervisor {
    episode: Option<ReconnectEpisode>,
}

impl ReconnectSupervisor {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn request(
        &mut self,
        observed: Generation,
        cause: &DaemonError,
    ) -> oneshot::Receiver<Result<Generation, DaemonError>> {
        let (sender, receiver) = oneshot::channel();
        let going_away = matches!(cause, DaemonError::GoingAway);
        let episode = self.episode.get_or_insert_with(|| ReconnectEpisode {
            observed,
            attempts: 0,
            phase: ReconnectPhase::ReadyAt(Instant::now()),
            waiters: Vec::new(),
            bounded: !going_away,
        });
        if going_away {
            episode.bounded = false;
        }
        let rolled_forward = observed > episode.observed;
        episode.observed = episode.observed.max(observed);
        if rolled_forward && matches!(episode.phase, ReconnectPhase::AwaitingHandshake) {
            let delay = if episode.attempts == 0 {
                Duration::ZERO
            } else {
                reconnect_delay(episode.attempts)
            };
            episode.phase = ReconnectPhase::ReadyAt(Instant::now() + delay);
        }
        episode.waiters.push(sender);
        receiver
    }

    pub fn attempt_count(&self) -> usize {
        self.episode.as_ref().map_or(0, |episode| episode.attempts)
    }

    pub fn next_attempt_at(&self) -> Option<Instant> {
        match self.episode.as_ref()?.phase {
            ReconnectPhase::ReadyAt(ready_at) => Some(ready_at),
            ReconnectPhase::AwaitingHandshake => None,
        }
    }

    fn begin_attempt(&mut self) -> Option<Generation> {
        let episode = self.episode.as_mut()?;
        let ReconnectPhase::ReadyAt(ready_at) = episode.phase else {
            return None;
        };
        if ready_at > Instant::now() {
            return None;
        }
        episode.attempts += 1;
        episode.phase = ReconnectPhase::AwaitingHandshake;
        Some(episode.observed)
    }

    pub fn start_due_attempt<D>(&mut self, daemon: D) -> Option<ReconnectFuture>
    where
        D: Daemon + 'static,
    {
        let observed = self.begin_attempt()?;
        Some(Box::pin(async move { daemon.reconnect(observed).await }))
    }

    pub fn complete_attempt(
        &mut self,
        result: Result<Generation, DaemonError>,
    ) -> ReconnectAttempt {
        if self
            .episode
            .as_ref()
            .is_some_and(|episode| matches!(episode.phase, ReconnectPhase::ReadyAt(_)))
        {
            if let Ok(generation) = result {
                let episode = self.episode.as_mut().expect("episode exists");
                episode.observed = episode.observed.max(generation);
            }
            if self.budget_exhausted() {
                let error = DaemonError::Unavailable { retry_after: None };
                self.settle(Err(error.clone()));
                return ReconnectAttempt::Exhausted(error);
            }
            return ReconnectAttempt::Idle;
        }
        match result {
            Ok(generation) => {
                if let Some(episode) = self.episode.as_mut() {
                    episode.observed = episode.observed.max(generation);
                }
                ReconnectAttempt::Reconnected(generation)
            }
            Err(error) => self.record_failure(error),
        }
    }

    pub async fn attempt_when_due<D: Daemon>(&mut self, daemon: &D) -> ReconnectAttempt {
        let Some(ready_at) = self.next_attempt_at() else {
            return ReconnectAttempt::Idle;
        };
        if ready_at > Instant::now() {
            tokio::time::sleep_until(ready_at).await;
        }
        let Some(observed) = self.begin_attempt() else {
            return ReconnectAttempt::Idle;
        };
        let result = daemon.reconnect(observed).await;
        self.complete_attempt(result)
    }

    pub fn handshake_failed(&mut self, error: DaemonError) -> ReconnectAttempt {
        self.record_failure(error)
    }

    pub fn handshake_complete(&mut self, generation: Generation) {
        self.settle(Ok(generation));
    }

    pub fn cancel(&mut self, error: DaemonError) {
        self.settle(Err(error));
    }

    fn budget_exhausted(&self) -> bool {
        self.episode
            .as_ref()
            .is_some_and(|episode| episode.bounded && episode.attempts > RECONNECT_DELAYS.len())
    }

    fn record_failure(&mut self, error: DaemonError) -> ReconnectAttempt {
        if self.budget_exhausted() {
            self.settle(Err(error.clone()));
            return ReconnectAttempt::Exhausted(error);
        }
        let Some(episode) = self.episode.as_mut() else {
            return ReconnectAttempt::Idle;
        };
        let delay = match &error {
            DaemonError::Unavailable {
                retry_after: Some(delay),
            } => (*delay).clamp(MIN_RETRY_AFTER, MAX_RETRY_AFTER),
            _ => reconnect_delay(episode.attempts),
        };
        episode.phase = ReconnectPhase::ReadyAt(Instant::now() + delay);
        ReconnectAttempt::RetryScheduled { delay }
    }

    fn settle(&mut self, result: Result<Generation, DaemonError>) {
        let Some(episode) = self.episode.take() else {
            return;
        };
        for waiter in episode.waiters {
            let _ = waiter.send(result.clone());
        }
    }
}
