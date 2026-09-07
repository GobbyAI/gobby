use super::*;
use crate::persist::load_snapshot;

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
            attention: AttentionState {
                epoch: String::new(),
                seq: 0,
                entries: Vec::new(),
                applied_seqs: Vec::new(),
            },
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

    /// Selects the project and loads its saved tab order so the first roster
    /// page follows it; without a Gobby home or a snapshot, daemon order leads.
    pub fn restore_project(&mut self, project_id: &str) -> std::io::Result<()> {
        self.select_project(project_id);
        let Some(home) = &self.gobby_home else {
            return Ok(());
        };
        self.saved_tab_order = match load_snapshot(home, project_id) {
            Ok(snapshot) => snapshot.tab_order,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Vec::new(),
            Err(error) => return Err(error),
        };
        Ok(())
    }

    pub fn set_frame_delivery(&mut self, frame_delivery: FrameDelivery) {
        self.frame_delivery = frame_delivery;
    }

    pub fn project_id(&self) -> Option<&str> {
        self.project_id.as_deref()
    }

    pub fn roster_terminal_ids(&self) -> Vec<String> {
        self.roster_ids.clone()
    }

    pub fn attention_entry_ids(&self) -> Vec<String> {
        self.attention.entries.clone()
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

    async fn fetch_attention(&mut self) -> Result<(), DaemonError> {
        let (epoch, seq, entries) = self.daemon.attention_roster().await?;
        self.attention.epoch = epoch;
        self.attention.seq = seq;
        self.attention.entries = entries.into_iter().map(|entry| entry.entry_id).collect();
        self.attention.applied_seqs = vec![seq];
        Ok(())
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
                continue;
            }
            for event in buffered {
                self.apply_live_event(event).await?;
            }
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
                let entry_id = payload
                    .get("entry_id")
                    .or_else(|| {
                        payload
                            .get("metadata")
                            .and_then(|value| value.get("entry_id"))
                    })
                    .and_then(Value::as_str);
                if let Some(entry_id) = entry_id {
                    if !self.attention.entries.iter().any(|known| known == entry_id) {
                        self.attention.entries.push(entry_id.to_string());
                    }
                }
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
            }
            DaemonEvent::Output(_)
            | DaemonEvent::Frame(_)
            | DaemonEvent::AttachHistory(_)
            | DaemonEvent::ScrollOffsetApplied(_)
            | DaemonEvent::Message(_) => {}
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
