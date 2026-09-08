use super::sidebar_model::GIT_REFRESH_INTERVAL;
use super::*;
use crate::daemon::message_kind;

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
            saved_tab_order: Vec::new(),
            saved_snapshot: None,
            attention: AttentionState::empty(),
            local_machine: String::new(),
            sidebar_rows: SidebarRows::default(),
            sidebar: SidebarModel::default(),
            git_refreshed_at: Instant::now(),
            pending_sidebar: PendingSidebar::default(),
            pending_attention: None,
            gobby_home: None,
            frame_delivery: FrameDelivery::Auto,
            lifecycle: None,
            daemon_ready: false,
            daemon_error: None,
            event_rx: None,
            attached_generation: HashMap::new(),
            pending_spawns: HashSet::new(),
            status_message: None,
            exit_reason: None,
            shutdown_started: false,
        }
    }

    pub fn daemon(&self) -> &LiveDaemon {
        &self.daemon
    }

    pub fn select_project(&mut self, project_id: impl Into<String>) {
        self.project_id = Some(project_id.into());
    }

    /// Selects the project and reads its snapshot: the first roster page
    /// follows the saved tab order and the loop rebuilds the tab bar from
    /// it. Without a Gobby home nothing is read, and the loop then neither
    /// restores nor seeds.
    pub fn restore_project(&mut self, project_id: &str) -> std::io::Result<()> {
        self.select_project(project_id);
        let Some(home) = &self.gobby_home else {
            return Ok(());
        };
        let snapshot = super::persistence::load_saved(home, project_id)?;
        self.saved_tab_order = snapshot.terminal_ids();
        self.saved_snapshot = Some(snapshot);
        Ok(())
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

    pub fn daemon_error(&self) -> Option<&DaemonError> {
        self.daemon_error.as_ref()
    }

    fn ensure_live_pane(&mut self, terminal_id: &str, backend: &str) -> PaneId {
        if let Some(id) = self.order.iter().copied().find(|id| {
            self.panes
                .get(id)
                .is_some_and(|pane| pane.terminal_id == terminal_id)
        }) {
            if !backend.is_empty() {
                self.panes.get_mut(&id).expect("pane exists").backend = backend.to_string();
            }
            return id;
        }
        let id = PaneId(self.next_pane);
        self.next_pane += 1;
        self.panes
            .insert(id, Pane::new_detached(id, terminal_id, backend, ""));
        self.order.push(id);
        id
    }

    fn install_live_rows(&mut self, mut rows: Vec<TerminalRow>) {
        // The saved tab order leads; rows outside it keep daemon order after it.
        let saved = &self.saved_tab_order;
        rows.sort_by_key(|row| {
            saved
                .iter()
                .position(|id| id == row.id())
                .unwrap_or(usize::MAX)
        });
        let mut ids = Vec::with_capacity(rows.len());
        for row in rows {
            let terminal_id = row.id().to_string();
            let backend = row
                .fields
                .get("backend")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let pane_id = self.ensure_live_pane(&terminal_id, backend);
            let pane = self.panes.get_mut(&pane_id).expect("live pane exists");
            pane.direct_available = row_has_direct_locator(&row);
            pane.title = row_title(&row);
            pane.address = row_address(&row);
            pane.session_id = row
                .fields
                .get("session_id")
                .and_then(Value::as_str)
                .map(str::to_string);
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
        // Roster order is pane order: the saved order first, new rows after
        // it in daemon order (`ensure_live_pane` appends them).
        self.roster_ids = self.tab_order();
    }

    pub async fn fetch_roster(&mut self) -> Result<(), DaemonError> {
        let project = self.project_id.clone().unwrap_or_default();
        for attempt in 0..2 {
            let mut cursor: Option<String> = None;
            let mut cursors = HashSet::new();
            let mut rows = Vec::new();
            let mut known = HashSet::new();
            let mut pin = None;
            let result = loop {
                let page = match self
                    .daemon
                    .list_terminals(&project, cursor.as_deref())
                    .await
                {
                    Ok(page) => page,
                    Err(error) => break Err(error),
                };
                if cursor.is_none() {
                    let Some(snapshot) = page.snapshot else {
                        break Err(DaemonError::Protocol {
                            detail: "first terminal page omitted snapshot".into(),
                        });
                    };
                    pin = Some(snapshot);
                }
                for row in page.items {
                    if !row.id().is_empty() && known.insert(row.id().to_string()) {
                        rows.push(row);
                    }
                }
                match page.next_cursor.filter(|next| !next.is_empty()) {
                    Some(next) if cursors.insert(next.clone()) => cursor = Some(next),
                    Some(_) => {
                        break Err(DaemonError::Protocol {
                            detail: "terminal cursor repeated".into(),
                        });
                    }
                    None => break Ok(()),
                }
            };
            match result {
                Ok(()) => {
                    self.install_live_rows(rows);
                    self.lifecycle = pin;
                    return Ok(());
                }
                Err(error) if attempt == 0 && is_cursor_error(&error) => continue,
                Err(error) => return Err(error),
            }
        }
        Err(DaemonError::Protocol {
            detail: "terminal pagination did not converge".into(),
        })
    }

    /// Replace the roster wholesale: an entry the daemon no longer returns is
    /// gone, whatever an event said about it.
    async fn fetch_attention(&mut self) -> Result<(), DaemonError> {
        let (epoch, seq, entries) = self.daemon.attention_roster().await?;
        self.attention.epoch = epoch;
        self.attention.seq = seq;
        self.attention.entries = entries;
        self.attention.applied_seqs = vec![seq];
        self.rebuild_sidebar();
        Ok(())
    }

    /// Fetch every sidebar row: projects, then status and worktrees for each
    /// project checked out here, then the focused project's sessions and runs.
    pub async fn fetch_sidebar_rows(&mut self) -> Result<(), DaemonError> {
        self.sidebar_rows.projects = self.daemon.projects().await?;
        let checked_out: Vec<String> = self.checked_out_projects();
        self.sidebar_rows.statuses.clear();
        self.sidebar_rows.worktrees.clear();
        for project in checked_out {
            self.fetch_project_rows(&project).await?;
        }
        self.fetch_focused_sessions().await?;
        self.pending_sidebar = PendingSidebar::default();
        self.rebuild_sidebar();
        Ok(())
    }

    fn checked_out_projects(&self) -> Vec<String> {
        self.sidebar_rows
            .projects
            .iter()
            .filter(|project| project.checkout.is_some())
            .map(|project| project.id.clone())
            .collect()
    }

    /// Refresh one project's source status and worktrees. A project with no
    /// checkout here has neither, so it is skipped rather than asked.
    async fn fetch_project_rows(&mut self, project: &str) -> Result<(), DaemonError> {
        let checked_out = self
            .sidebar_rows
            .projects
            .iter()
            .any(|row| row.id == project && row.checkout.is_some());
        if !checked_out {
            return Ok(());
        }
        let status = self.daemon.source_status(project).await?;
        let worktrees = self.daemon.worktrees(project).await?;
        self.sidebar_rows
            .statuses
            .insert(project.to_string(), status);
        self.sidebar_rows
            .worktrees
            .retain(|row| row.project_id != project);
        self.sidebar_rows.worktrees.extend(worktrees);
        self.git_refreshed_at = Instant::now();
        Ok(())
    }

    async fn fetch_focused_sessions(&mut self) -> Result<(), DaemonError> {
        let Some(project) = self.project_id.clone() else {
            return Ok(());
        };
        let sessions = self.daemon.sessions(&project).await?;
        let runs = self.daemon.agent_runs(&project).await?;
        self.sidebar_rows.sessions.insert(project.clone(), sessions);
        self.sidebar_rows.runs.insert(project, runs);
        Ok(())
    }

    /// Run the refetches live events queued since the last flush, at most
    /// one per route and project however many events asked for it.
    pub async fn flush_sidebar_refetches(&mut self) -> Result<(), DaemonError> {
        let pending = std::mem::take(&mut self.pending_sidebar);
        if pending.projects {
            self.sidebar_rows.projects = self.daemon.projects().await?;
        }
        for project in &pending.project_rows {
            self.fetch_project_rows(project).await?;
        }
        if pending.sessions {
            self.fetch_focused_sessions().await?;
        }
        if pending.projects || pending.sessions || !pending.project_rows.is_empty() {
            self.rebuild_sidebar();
        }
        Ok(())
    }

    /// Queue a status refresh for every checked-out project once the last
    /// one is older than `GIT_REFRESH_INTERVAL`; the caller flushes it.
    pub fn request_git_refresh_if_due(&mut self) {
        if self.git_refreshed_at.elapsed() < GIT_REFRESH_INTERVAL {
            return;
        }
        self.pending_sidebar
            .project_rows
            .extend(self.checked_out_projects());
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
                    Some("exited" | "killed" | "terminated") => {
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
                                pane.pending_input = None;
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
                self.fetch_roster().await?;
                self.fetch_attention().await?;
                self.fetch_sidebar_rows().await?;
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
        }
        Ok(())
    }

    /// Queue the refetch a project, worktree, or session event calls for;
    /// `flush_sidebar_refetches` runs each at most once per drain.
    fn note_sidebar_message(&mut self, message: &Value) {
        let project_id = message
            .get("project_id")
            .and_then(Value::as_str)
            .map(str::to_string);
        match message_kind(message) {
            Some("project_event") => {
                self.pending_sidebar.projects = true;
                if let Some(project_id) = project_id {
                    self.pending_sidebar.project_rows.insert(project_id);
                }
            }
            Some("worktree_event") => match project_id {
                Some(project_id) => {
                    self.pending_sidebar.project_rows.insert(project_id);
                }
                // A worktree event without its project refreshes every
                // checkout rather than guessing which one moved.
                None => self
                    .pending_sidebar
                    .project_rows
                    .extend(self.checked_out_projects()),
            },
            Some("session_event") => self.pending_sidebar.sessions = true,
            _ => {}
        }
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
            pane.clear_control("daemon disconnected");
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
/// The terminal's own name. A `null` title and a `""` title mean the same thing
/// to the chrome, so both collapse to the empty string `display_name` handles.
fn row_title(row: &TerminalRow) -> String {
    row.fields
        .get("title")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
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
