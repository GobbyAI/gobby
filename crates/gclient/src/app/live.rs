use super::sidebar_model::{GIT_REFRESH_INTERVAL, ROSTER_REFRESH_INTERVAL};
use super::*;
use crate::daemon::{message_kind, ProjectRow, RunRow, SessionRow, SourceStatus, WorktreeRow};
use std::collections::BTreeSet;
use std::future::Future;
use std::pin::Pin;
use tokio::sync::mpsc::UnboundedSender;
#[path = "live_sidebar.rs"]
mod live_sidebar;
pub(super) use live_sidebar::FetchRetry;
pub use live_sidebar::{SidebarFetch, SidebarFetchFuture};

pub(super) mod relist;

impl Workspace<LiveDaemon> {
    pub fn live(daemon: LiveDaemon) -> Self {
        Self {
            project_id: None,
            daemon,
            panes: HashMap::new(),
            order: Vec::new(),
            focus: None,
            next_pane: 1,
            roster_ids: Vec::new(),
            pane_order: Vec::new(),
            attach: AttachTarget::default(),
            in_pane: false,
            attention: AttentionState::empty(),
            local_machine: String::new(),
            sidebar_rows: SidebarRows::default(),
            sidebar: SidebarModel::default(),
            git_refreshed_at: Instant::now(),
            roster_refreshed_at: Instant::now(),
            session_retry: FetchRetry::default(),
            projects_retry: FetchRetry::default(),
            last_roster_refresh_completed_at: None,
            pending_sidebar: PendingSidebar::default(),
            sidebar_stamps: SidebarStamps::default(),
            pending_attention: None,
            gobby_home: None,
            launch_dir: None,
            frame_delivery: FrameDelivery::Auto,
            lifecycle: None,
            relist: relist::RelistState::default(),
            daemon_ready: false,
            daemon_error: None,
            event_rx: None,
            attached_generation: HashMap::new(),
            host_recovered: HashSet::new(),
            host_recovering: HashSet::new(),
            pending_spawns: HashSet::new(),
            status_message: None,
            exit_reason: None,
            shutdown_started: false,
            workspace_model: None,
            pending_placements: HashSet::new(),
            placed_panes: Vec::new(),
            requested_focus: None,
            pending_control: None,
            next_control_seq: 0,
        }
    }

    pub fn daemon(&self) -> &LiveDaemon {
        &self.daemon
    }

    pub fn select_project(&mut self, project_id: impl Into<String>) {
        self.project_id = Some(project_id.into());
    }

    pub fn set_frame_delivery(&mut self, frame_delivery: FrameDelivery) {
        self.frame_delivery = frame_delivery;
    }

    pub fn project_id(&self) -> Option<&str> {
        self.project_id.as_deref()
    }

    pub fn daemon_ready(&self) -> bool {
        self.daemon_ready
    }

    /// An empty `backend` leaves a known pane's backend alone: not every
    /// terminal event names one.
    fn ensure_live_pane(&mut self, terminal_id: &str, backend: &str) -> PaneId {
        if let Some(id) = self.order.iter().copied().find(|id| {
            self.panes
                .get(id)
                .is_some_and(|pane| pane.terminal_id == terminal_id)
        }) {
            if !backend.is_empty() {
                self.panes.get_mut(&id).expect("pane exists").backend = Backend::parse(backend);
            }
            return id;
        }
        let id = PaneId(self.next_pane);
        self.next_pane += 1;
        self.relist.invalidate();
        self.panes.insert(
            id,
            Pane::new_detached(id, terminal_id, Backend::parse(backend), ""),
        );
        self.order.push(id);
        id
    }

    fn install_live_row(&mut self, row: &TerminalRow) -> PaneId {
        let terminal_id = row.id();
        let backend = row
            .fields
            .get("backend")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let pane_id = self.ensure_live_pane(terminal_id, backend);
        let pane = self.panes.get_mut(&pane_id).expect("live pane exists");
        pane.direct_available = row_has_direct_locator(row);
        pane.external = row_is_external(row);
        pane.address = row_address(row);
        pane.command = row_command(row);
        pane.cwd = row_cwd(row);
        pane.terminal_state = row
            .fields
            .get("state")
            .and_then(Value::as_str)
            .map(str::to_owned);
        pane.session_id = row
            .fields
            .get("session_id")
            .and_then(Value::as_str)
            .map(str::to_string);
        pane.observe_lease_holder(
            row.fields
                .get("lease_holder")
                .and_then(|holder| holder.get("attachment_id"))
                .and_then(Value::as_str),
        );
        pane_id
    }

    pub(super) async fn open_live_terminal(
        &mut self,
        terminal_id: &str,
    ) -> Result<PaneId, FrameError> {
        let pane = self.open_live_terminal_unattached(terminal_id).await?;
        self.attach_ready_panes().await?;
        Ok(pane)
    }

    pub(super) async fn open_live_terminal_unattached(
        &mut self,
        terminal_id: &str,
    ) -> Result<PaneId, FrameError> {
        if let Some(pane) = self.pane_for_terminal(terminal_id) {
            return Ok(pane);
        }
        let row = self.daemon.terminal(terminal_id).await?;
        let pane = self.install_live_row(&row);
        if !self.roster_ids.iter().any(|id| id == terminal_id) {
            self.roster_ids.push(terminal_id.to_string());
        }
        self.rebuild_sidebar();
        Ok(pane)
    }

    pub(super) fn install_unresolved_rows(&mut self, rows: Vec<TerminalRow>) {
        for row in rows {
            if self.pane_for_terminal(&row.terminal_id).is_some() {
                continue;
            }
            self.install_live_row(&row);
            if !self.roster_ids.contains(&row.terminal_id) {
                self.roster_ids.push(row.terminal_id);
            }
        }
        self.rebuild_sidebar();
    }

    fn install_live_rows(&mut self, mut rows: Vec<TerminalRow>) {
        // The window's pane order leads; rows outside it keep daemon order
        // after it.
        let pane_order = &self.pane_order;
        rows.sort_by_key(|row| {
            pane_order
                .iter()
                .position(|id| id == row.id())
                .unwrap_or(usize::MAX)
        });
        let mut ids = Vec::with_capacity(rows.len());
        for row in rows {
            let terminal_id = row.id().to_string();
            self.install_live_row(&row);
            ids.push(terminal_id);
        }
        let wanted: HashSet<_> = ids.iter().cloned().collect();
        self.panes
            .retain(|_, pane| wanted.contains(&pane.terminal_id));
        self.order.retain(|id| self.panes.contains_key(id));
        self.attached_generation
            .retain(|id, _| self.panes.contains_key(id));
        if self.focus.is_some_and(|id| !self.panes.contains_key(&id)) {
            self.focus = self.order.first().copied();
        }
        // Roster order is pane order: the window's order first, new rows
        // after it in daemon order (`ensure_live_pane` appends them).
        self.roster_ids = self.tab_order();
    }

    /// Records the control request a focus change or a queued key needs. It is
    /// only a note: `start_control_request` turns it into the daemon round
    /// trip, beside the loop, so nothing a person does waits on it (#22573).
    /// A pane already waiting on a reply keeps that one.
    pub fn request_control(&mut self, pane_id: PaneId, takeover: bool) {
        let idle = self
            .panes
            .get(&pane_id)
            .is_some_and(|pane| pane.control_request.is_none());
        if idle {
            self.pending_control = Some(PendingControl { pane_id, takeover });
        }
    }

    /// True while this pane is waiting on a grant, whether its request is
    /// already in flight or is still the note a focus change just made. Input
    /// arriving in that window belongs to the pane, not to the floor (#22573).
    pub fn awaiting_control(&self, pane_id: PaneId) -> bool {
        self.pending_control
            .is_some_and(|pending| pending.pane_id == pane_id)
            || self
                .panes
                .get(&pane_id)
                .is_some_and(|pane| pane.is_acquiring())
    }

    /// Starts the recorded control request, if the pane can still use one. The
    /// reply comes back through `outcomes`, so requests never queue behind one
    /// another: a pane whose grant is still in flight must not delay the grant
    /// the pane someone just clicked is waiting for (#22573).
    pub fn start_control_request(&mut self, outcomes: &UnboundedSender<ControlOutcome>) {
        let Some(PendingControl { pane_id, takeover }) = self.pending_control else {
            return;
        };
        if self.exit_reason.is_some() {
            self.pending_control = None;
            return;
        }
        // A focus change can arrive while the daemon is reconnecting. Keep
        // the wish until it can be sent so the input queued behind that focus
        // is not orphaned (#22573).
        if !self.daemon_ready() {
            return;
        }
        let Some(pane) = self.panes.get(&pane_id) else {
            self.pending_control = None;
            return;
        };
        // An attachment can become live on a later daemon event. The pending
        // request belongs to that pane until then.
        if !pane.is_live() {
            return;
        }
        self.pending_control = None;
        self.next_control_seq += 1;
        let request = self.next_control_seq;
        let pane = self.panes.get_mut(&pane_id).expect("pane checked above");
        pane.control_request = Some(request);
        let message = json!({
            "type": "terminal_take_control",
            "terminal_id": pane.terminal_id,
            "attachment_id": pane.attachment_id(),
            "takeover": takeover,
        });
        let daemon = self.daemon.clone();
        let outcomes = outcomes.clone();
        tokio::spawn(async move {
            let reply = daemon.send(message).await;
            // The loop is gone when the send fails on a closed channel; the
            // lease it was asking for is released by `shutdown` either way.
            let _ = outcomes.send(ControlOutcome {
                pane_id,
                request,
                reply,
            });
        });
    }

    pub async fn reconcile_subscribe_first(&mut self) -> Result<(), DaemonError> {
        let (subscribed, mut receiver) = self.daemon.subscribe();
        self.daemon_ready = subscribed.ready;
        self.daemon_error = subscribed.last_error;
        if !subscribed.ready {
            return Err(self
                .daemon_error
                .clone()
                .unwrap_or(DaemonError::Unavailable { retry_after: None }));
        }
        self.attach_live_workspace().await?;
        self.fetch_roster().await?;
        self.fetch_attention().await?;
        self.fetch_sidebar_rows().await?;
        let result = self.drain_receiver(&mut receiver).await;
        self.event_rx = Some(receiver);
        result
    }

    pub async fn drain_live_events(&mut self) -> Result<(), DaemonError> {
        let mut receiver = self
            .event_rx
            .take()
            .unwrap_or_else(|| self.daemon.subscribe().1);
        let result = self.drain_receiver(&mut receiver).await;
        self.event_rx = Some(receiver);
        result
    }

    async fn drain_receiver(&mut self, receiver: &mut EventReceiver) -> Result<(), DaemonError> {
        loop {
            let mut buffered = Vec::new();
            let relist = loop {
                match receiver.try_recv() {
                    Ok(DaemonEvent::Lagged) => break true,
                    Ok(event) if buffered.len() < WORKSPACE_EVENT_BUFFER => buffered.push(event),
                    Ok(_) | Err(TryRecvError::Lagged(_)) => break true,
                    Err(TryRecvError::Empty | TryRecvError::Closed) => break false,
                }
            };
            if relist {
                let (status, replacement) = self.daemon.subscribe();
                *receiver = replacement;
                self.daemon_ready = status.ready;
                self.daemon_error = status.last_error.clone();
                if !status.ready {
                    return Err(self
                        .daemon_error
                        .clone()
                        .unwrap_or(DaemonError::Unavailable { retry_after: None }));
                }
                self.attach_live_workspace().await?;
                self.fetch_roster().await?;
                self.fetch_attention().await?;
                self.fetch_sidebar_rows().await?;
                continue;
            }
            for event in buffered {
                self.apply_live_event(event).await?;
            }
            self.flush_sidebar_refetches().await?;
            self.rebuild_sidebar();
            let current = self.daemon.subscribe().0;
            self.daemon_ready = current.ready;
            self.daemon_error = current.last_error;
            if !current.ready {
                return Err(self
                    .daemon_error
                    .clone()
                    .unwrap_or(DaemonError::Unavailable { retry_after: None }));
            }
            self.open_unresolved_terminals().await;
            self.attach_ready_panes().await?;
            return Ok(());
        }
    }

    pub(super) async fn apply_live_event(&mut self, event: DaemonEvent) -> Result<(), DaemonError> {
        match event {
            DaemonEvent::Terminal {
                daemon_epoch,
                seq,
                payload,
            } => {
                if !self.accept_lifecycle(&daemon_epoch, seq) {
                    if self
                        .lifecycle
                        .as_ref()
                        .is_some_and(|pin| pin.daemon_epoch != daemon_epoch)
                    {
                        self.fetch_roster().await?;
                    }
                    return Ok(());
                }
                let terminal_id = payload
                    .get("terminal_id")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                match payload.get("event").and_then(Value::as_str) {
                    Some("created") if !terminal_id.is_empty() => {
                        if !self.roster_ids.iter().any(|id| id == terminal_id) {
                            self.roster_ids.push(terminal_id.to_string());
                        }
                        let backend = payload
                            .get("backend")
                            .and_then(Value::as_str)
                            .unwrap_or_default();
                        self.ensure_live_pane(terminal_id, backend);
                    }
                    Some("exited" | "killed" | "terminated" | "orphaned") => {
                        self.roster_ids.retain(|id| id != terminal_id);
                        self.remove_terminal(terminal_id);
                    }
                    _ => {}
                }
                self.advance_lifecycle(daemon_epoch, seq);
            }
            DaemonEvent::LeaseLost {
                daemon_epoch,
                seq,
                payload,
            } => {
                if self.accept_lifecycle(&daemon_epoch, seq) {
                    if let Some(attachment_id) =
                        payload.get("attachment_id").and_then(Value::as_str)
                    {
                        let lease_generation = payload
                            .get("lease_generation")
                            .and_then(Value::as_u64)
                            .unwrap_or(0);
                        if let Some(pane) = self.pane_for_attachment_mut(attachment_id) {
                            if lease_generation >= pane.lease_generation() {
                                pane.set_lease_generation(lease_generation);
                                pane.control = ControlState::LeaseLost;
                                pane.take_back = true;
                                pane.clear_pending_input();
                            }
                        }
                    }
                    self.advance_lifecycle(daemon_epoch, seq);
                } else if self
                    .lifecycle
                    .as_ref()
                    .is_some_and(|pin| pin.daemon_epoch != daemon_epoch)
                {
                    self.fetch_roster().await?;
                }
            }
            DaemonEvent::AttachmentFinalized {
                daemon_epoch,
                seq,
                attachment_id,
                payload,
            } => {
                if self.accept_lifecycle(&daemon_epoch, seq) {
                    let reason = payload.get("reason").and_then(Value::as_str);
                    self.arm_indeterminate_reattach(&attachment_id, &payload);
                    self.retire_attachment(&attachment_id, reason);
                    self.advance_lifecycle(daemon_epoch, seq);
                } else if self
                    .lifecycle
                    .as_ref()
                    .is_some_and(|pin| pin.daemon_epoch != daemon_epoch)
                {
                    self.fetch_roster().await?;
                }
            }
            DaemonEvent::Attention {
                epoch,
                seq,
                payload,
            } => {
                if !self.attention.epoch.is_empty() && self.attention.epoch != epoch {
                    self.fetch_attention().await?;
                    return Ok(());
                }
                if epoch == self.attention.epoch && seq <= self.attention.seq {
                    return Ok(());
                }
                self.note_attention_event(&payload);
                self.attention.epoch = epoch;
                self.attention.seq = seq;
                self.attention.applied_seqs.push(seq);
            }
            DaemonEvent::Disconnected { generation, error } => {
                self.observe_daemon_disconnect(generation, error);
            }
            DaemonEvent::Lagged => {
                // The repairs run beside the loop: inline, a slow answer held
                // it while frames lagged the receiver again (#22747).
                self.request_relist();
                self.pending_sidebar = PendingSidebar::everything(self.checked_out_projects());
            }
            DaemonEvent::Message(message) => {
                if message_kind(&message) == Some("terminal_resize_result") {
                    self.note_resize_result(&message);
                } else {
                    self.note_sidebar_message(&message);
                }
            }
            DaemonEvent::Output(_)
            | DaemonEvent::Frame(_)
            | DaemonEvent::AttachHistory(_)
            | DaemonEvent::ScrollOffsetApplied(_) => {}
            DaemonEvent::Workspace(event) => {
                if self.apply_live_workspace_event(&event) {
                    self.open_unresolved_terminals().await;
                }
            }
        }
        Ok(())
    }

    fn accept_lifecycle(&self, epoch: &str, seq: u64) -> bool {
        self.lifecycle
            .as_ref()
            .is_none_or(|pin| pin.daemon_epoch == epoch && seq > pin.seq)
    }

    fn advance_lifecycle(&mut self, daemon_epoch: String, seq: u64) {
        self.lifecycle = Some(Snapshot { daemon_epoch, seq });
    }

    pub async fn reconnect_daemon_ws(&mut self) -> Result<Generation, DaemonError> {
        for pane in self.panes.values_mut() {
            pane.clear_control("Daemon disconnected.");
        }
        self.reconcile_subscribe_first().await?;
        self.daemon_ready = true;
        self.daemon_error = None;
        Ok(self.daemon.generation())
    }
}

fn is_cursor_error(error: &DaemonError) -> bool {
    let DaemonError::Protocol { detail } = error else {
        return false;
    };
    let detail = detail.to_ascii_lowercase();
    detail.contains("cursor_stale") || detail.contains("invalid cursor")
}

/// What one control request came back with.
pub struct ControlOutcome {
    pub(super) pane_id: PaneId,
    pub(super) request: u64,
    pub(super) reply: Result<Value, DaemonError>,
}

/// The command in the terminal's foreground, as the daemon observed it when it
/// served the row: `pane_current_command` for tmux, the foreground process
/// group of the recorded shell pid for native. Absent on a row the daemon could
/// not probe, which the label ladder answers with its literal last rung.
fn row_command(row: &TerminalRow) -> Option<String> {
    let command = row.fields.get("command")?.as_str()?;
    (!command.is_empty()).then(|| command.to_string())
}

/// The terminal's working directory, as the daemon observed it when it
/// served the row. Absent on a row the daemon could not probe.
fn row_cwd(row: &TerminalRow) -> Option<String> {
    let cwd = row.fields.get("cwd")?.as_str()?;
    (!cwd.is_empty()).then(|| cwd.to_string())
}

/// The terminal's address on its backend. Only tmux has one the user can act
/// on; a native row's host terminal id is another UUID, so it stays hidden.
fn row_address(row: &TerminalRow) -> Option<String> {
    let attach = row.fields.get("attach")?.as_object()?;
    if attach.get("backend").and_then(Value::as_str) != Some("tmux") {
        return None;
    }
    let pane_id = attach.get("pane_id").and_then(Value::as_str)?;
    (!pane_id.is_empty()).then(|| pane_id.to_string())
}

fn row_is_external(row: &TerminalRow) -> bool {
    row.fields.get("ownership").and_then(Value::as_str) == Some("external")
}

/// Whether a `/api/terminals` row advertises enough to attempt a direct attach.
///
/// The row's `attach` block is a flat `AttachLocator` (`asdict`, not
/// `direct_block`), and identity in it is backend shaped: a native terminal is
/// named by its `host_terminal_id`, while a tmux pane is named by its physical
/// locator — socket, pane id, and the server generation that keeps a recycled
/// pane id unambiguous. The frame host draws the same line: `embed::attach_frame`
/// ignores `host_terminal_id` outright once a pane locator is present.
///
/// The daemon's row producer leaves `host_terminal_id` null for tmux, so
/// demanding it here matched no real tmux row and silently downgraded every one
/// of them to proxy.
fn row_has_direct_locator(row: &TerminalRow) -> bool {
    let Some(attach) = row.fields.get("attach").and_then(Value::as_object) else {
        return false;
    };
    let filled = |name: &str| {
        attach
            .get(name)
            .and_then(Value::as_str)
            .is_some_and(|value| !value.is_empty())
    };
    // `as_i64` rejects a JSON bool and a float, which is the generation check.
    let generation = |name: &str| attach.get(name).and_then(Value::as_i64).is_some();
    if !filled("frame_host_epoch") || !filled("host_socket") {
        return false;
    }
    match attach.get("backend").and_then(Value::as_str) {
        Some("native") => filled("host_terminal_id"),
        Some("tmux") => {
            filled("socket_path")
                && filled("pane_id")
                && generation("server_pid")
                && generation("server_start_time")
        }
        _ => false,
    }
}

#[cfg(test)]
#[path = "live/tests.rs"]
mod tests;
