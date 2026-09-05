use super::*;
use crate::frame_source::{ProxyFrameSource, UnixSocketFrameSource};

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
            attention: AttentionState {
                epoch: String::new(),
                seq: 0,
                entries: Vec::new(),
                applied_seqs: Vec::new(),
            },
            gobby_home: None,
            lifecycle: None,
            daemon_ready: false,
            daemon_error: None,
            event_rx: None,
            attached_generation: HashMap::new(),
        }
    }

    pub fn daemon(&self) -> &LiveDaemon {
        &self.daemon
    }

    pub fn select_project(&mut self, project_id: impl Into<String>) {
        self.project_id = Some(project_id.into());
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

    fn install_live_rows(&mut self, rows: Vec<TerminalRow>) {
        let mut ids = Vec::with_capacity(rows.len());
        for row in rows {
            let terminal_id = row.id().to_string();
            let backend = row
                .fields
                .get("backend")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let pane_id = self.ensure_live_pane(&terminal_id, backend);
            self.panes
                .get_mut(&pane_id)
                .expect("live pane exists")
                .direct_available = row_has_direct_locator(&row);
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
        self.roster_ids = ids;
    }

    async fn attach_ready_panes(&mut self) -> Result<(), DaemonError> {
        let snapshot = self.daemon.subscribe().0;
        if !snapshot.ready {
            return Ok(());
        }
        for pane_id in self.order.clone() {
            if self.attached_generation.get(&pane_id) == Some(&snapshot.generation) {
                continue;
            }
            let terminal_id = self.panes[&pane_id].terminal_id.clone();
            if self.panes[&pane_id].direct_available {
                match self.request_direct_source(&terminal_id).await {
                    Ok((reply, attachment, locator, source)) => {
                        self.install_direct_source(pane_id, &reply, attachment, &locator, source);
                        self.attached_generation
                            .insert(pane_id, snapshot.generation);
                        continue;
                    }
                    Err(FrameError::Finalized { .. }) => {
                        self.retire_pane_attachment(pane_id);
                        self.attached_generation
                            .insert(pane_id, snapshot.generation);
                        continue;
                    }
                    Err(_) => {}
                }
            }
            let attachment = self.request_proxy_source(&terminal_id).await;
            let (reply, attachment, source) = match attachment {
                Ok(attachment) => attachment,
                Err(FrameError::Finalized { .. }) => {
                    self.retire_pane_attachment(pane_id);
                    self.attached_generation
                        .insert(pane_id, snapshot.generation);
                    continue;
                }
                Err(error) => {
                    return Err(DaemonError::Protocol {
                        detail: error.to_string(),
                    });
                }
            };
            self.install_proxy_source(pane_id, &reply, attachment, source);
            self.attached_generation
                .insert(pane_id, snapshot.generation);
        }
        Ok(())
    }

    async fn request_direct_source(
        &self,
        terminal_id: &str,
    ) -> Result<(Value, String, AttachLocator, UnixSocketFrameSource), FrameError> {
        let reply = self
            .daemon
            .send(json!({
                "type": "terminal_attach",
                "request_id": uuid::Uuid::new_v4().to_string(),
                "terminal_id": terminal_id,
                "frame_delivery": "direct",
                "encoding": "semantic_frame",
            }))
            .await?;
        if reply.get("success").and_then(Value::as_bool) != Some(true) {
            return Err(FrameError::Protocol(
                reply
                    .get("reason")
                    .and_then(Value::as_str)
                    .unwrap_or("terminal attach refused")
                    .to_string(),
            ));
        }
        let attachment = reply
            .get("attachment_id")
            .and_then(Value::as_str)
            .ok_or_else(|| FrameError::Protocol("attach result omitted attachment_id".into()))?
            .to_string();
        let result = self.connect_direct_reply(&reply).await;
        match result {
            Ok((locator, source)) => Ok((reply, attachment, locator, source)),
            Err(error) => {
                self.daemon
                    .notify(json!({
                        "type": "terminal_detach",
                        "request_id": uuid::Uuid::new_v4().to_string(),
                        "terminal_id": terminal_id,
                        "attachment_id": attachment,
                    }))
                    .await?;
                Err(error)
            }
        }
    }

    async fn connect_direct_reply(
        &self,
        reply: &Value,
    ) -> Result<(AttachLocator, UnixSocketFrameSource), FrameError> {
        let locator = direct_reply_locator(reply)?;
        let cols = reply
            .get("cols")
            .and_then(Value::as_u64)
            .and_then(|value| u16::try_from(value).ok())
            .unwrap_or(80);
        let rows = reply
            .get("rows")
            .and_then(Value::as_u64)
            .and_then(|value| u16::try_from(value).ok())
            .unwrap_or(24);
        let source = match &self.gobby_home {
            Some(home) => {
                UnixSocketFrameSource::from_gobby_home(home, &locator, cols, rows).await?
            }
            None => UnixSocketFrameSource::from_env(&locator, cols, rows).await?,
        };
        Ok((locator, source))
    }

    fn install_direct_source(
        &mut self,
        pane_id: PaneId,
        reply: &Value,
        attachment: String,
        locator: &AttachLocator,
        source: UnixSocketFrameSource,
    ) {
        let pane = self.panes.get_mut(&pane_id).expect("pane exists");
        pane.attachment_id = attachment;
        if let Some(backend) = reply.get("backend").and_then(Value::as_str) {
            pane.backend = backend.to_string();
        }
        pane.expected_host_epoch = locator.frame_host_epoch.clone();
        pane.lease_generation = reply
            .get("lease_generation")
            .and_then(Value::as_u64)
            .unwrap_or_default();
        pane.live = true;
        pane.control = ControlState::Observe;
        pane.install_frame_source(PaneFrameSource::Direct(source));
    }

    pub async fn recv_live_frame(&mut self, pane_id: PaneId) -> Result<ServerMessage, FrameError> {
        let result = self.recv_pane_frame(pane_id).await;
        let Err(error) = result else {
            return result;
        };
        match error {
            FrameError::Finalized { .. } => self.retire_pane_attachment(pane_id),
            FrameError::Eof
            | FrameError::Lag
            | FrameError::Cancelled
            | FrameError::Io(_)
            | FrameError::Protocol(_) => self.recover_proxy_source(pane_id).await?,
            FrameError::HostEpochChanged { .. } | FrameError::Other(_) => {}
        }
        Err(error)
    }

    async fn recover_proxy_source(&mut self, pane_id: PaneId) -> Result<(), FrameError> {
        let pane = self
            .panes
            .get_mut(&pane_id)
            .ok_or_else(|| FrameError::Protocol("unknown pane".into()))?;
        if pane.fallback_in_flight {
            return Ok(());
        }
        pane.fallback_in_flight = true;
        let terminal_id = pane.terminal_id.clone();
        let old_attachment = std::mem::take(&mut pane.attachment_id);
        pane.live = false;
        pane.control = ControlState::Observe;
        pane.fragment = None;
        let _ = pane.take_frame_source();

        if !old_attachment.is_empty() {
            if let Err(error) = self
                .daemon
                .notify(json!({
                    "type": "terminal_detach",
                    "request_id": uuid::Uuid::new_v4().to_string(),
                    "terminal_id": terminal_id,
                    "attachment_id": old_attachment,
                }))
                .await
            {
                self.clear_fallback_flight(pane_id);
                return Err(error.into());
            }
        }

        let source = self.request_proxy_source(&terminal_id).await;
        let (reply, attachment, source) = match source {
            Ok(source) => source,
            Err(error @ FrameError::Finalized { .. }) => {
                self.retire_pane_attachment(pane_id);
                return Err(error);
            }
            Err(error) => {
                self.clear_fallback_flight(pane_id);
                return Err(error);
            }
        };
        self.install_proxy_source(pane_id, &reply, attachment, source);
        self.attached_generation
            .insert(pane_id, self.daemon.generation());
        Ok(())
    }

    async fn request_proxy_source(
        &self,
        terminal_id: &str,
    ) -> Result<(Value, String, ProxyFrameSource), FrameError> {
        let (_, receiver) = self.daemon.subscribe();
        let reply = self
            .daemon
            .send(json!({
                "type": "terminal_attach",
                "request_id": uuid::Uuid::new_v4().to_string(),
                "terminal_id": terminal_id,
                "frame_delivery": "proxy",
                "encoding": "semantic_frame",
            }))
            .await?;
        if reply.get("success").and_then(Value::as_bool) != Some(true) {
            return Err(FrameError::Protocol(
                reply
                    .get("reason")
                    .and_then(Value::as_str)
                    .unwrap_or("terminal attach refused")
                    .to_string(),
            ));
        }
        let attachment = reply
            .get("attachment_id")
            .and_then(Value::as_str)
            .ok_or_else(|| FrameError::Protocol("attach result omitted attachment_id".into()))?
            .to_string();
        let source = ProxyFrameSource::from_attachment(
            self.daemon.clone(),
            terminal_id,
            attachment.clone(),
            receiver,
        )?;
        Ok((reply, attachment, source))
    }

    fn install_proxy_source(
        &mut self,
        pane_id: PaneId,
        reply: &Value,
        attachment: String,
        source: ProxyFrameSource,
    ) {
        let pane = self.panes.get_mut(&pane_id).expect("pane exists");
        pane.attachment_id = attachment;
        if let Some(backend) = reply.get("backend").and_then(Value::as_str) {
            pane.backend = backend.to_string();
        }
        pane.lease_generation = reply
            .get("lease_generation")
            .and_then(Value::as_u64)
            .unwrap_or_default();
        pane.live = true;
        pane.control = ControlState::Observe;
        pane.install_frame_source(PaneFrameSource::Proxy(source));
    }

    fn clear_fallback_flight(&mut self, pane_id: PaneId) {
        if let Some(pane) = self.panes.get_mut(&pane_id) {
            pane.fallback_in_flight = false;
        }
    }

    fn retire_pane_attachment(&mut self, pane_id: PaneId) {
        if let Some(pane) = self.panes.get_mut(&pane_id) {
            pane.attachment_id.clear();
            pane.live = false;
            pane.control = ControlState::Observe;
            pane.fragment = None;
            pane.fallback_in_flight = false;
            let _ = pane.take_frame_source();
        }
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
                    self.daemon.reconnect(status.generation).await?;
                    *receiver = self.daemon.subscribe().1;
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
            if current.ready {
                self.attach_ready_panes().await?;
            }
            return Ok(());
        }
    }

    async fn apply_live_event(&mut self, event: DaemonEvent) -> Result<(), DaemonError> {
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
                    Some("exited" | "terminated") => {
                        self.roster_ids.retain(|id| id != terminal_id);
                        self.remove_terminal(terminal_id);
                    }
                    _ => {}
                }
                self.advance_lifecycle(daemon_epoch, seq);
            }
            DaemonEvent::LeaseLost {
                daemon_epoch, seq, ..
            }
            | DaemonEvent::AttachmentFinalized {
                daemon_epoch, seq, ..
            } => {
                if self.accept_lifecycle(&daemon_epoch, seq) {
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
            DaemonEvent::Disconnected { error, .. } => {
                self.daemon_ready = false;
                self.daemon_error = Some(error);
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
        let observed = self.daemon.generation();
        for pane in self.panes.values_mut() {
            pane.live = false;
            pane.control = ControlState::Observe;
        }
        let generation = self.daemon.reconnect(observed).await?;
        self.reconcile_subscribe_first().await?;
        self.daemon_ready = true;
        self.daemon_error = None;
        Ok(generation)
    }
}

fn is_cursor_error(error: &DaemonError) -> bool {
    let DaemonError::Protocol { detail } = error else {
        return false;
    };
    let detail = detail.to_ascii_lowercase();
    detail.contains("cursor_stale") || detail.contains("invalid cursor")
}

fn row_has_direct_locator(row: &TerminalRow) -> bool {
    let Some(attach) = row.fields.get("attach").and_then(Value::as_object) else {
        return false;
    };
    matches!(
        (
            attach.get("backend").and_then(Value::as_str),
            attach.get("frame_host_epoch").and_then(Value::as_str),
            attach.get("host_socket").and_then(Value::as_str),
            attach.get("host_terminal_id").and_then(Value::as_str),
        ),
        (Some("native" | "tmux"), Some(epoch), Some(socket), Some(host_id))
            if !epoch.is_empty() && !socket.is_empty() && !host_id.is_empty()
    )
}

fn direct_reply_locator(reply: &Value) -> Result<AttachLocator, FrameError> {
    let direct = reply
        .get("direct")
        .and_then(Value::as_object)
        .ok_or_else(|| FrameError::Protocol("attach result omitted direct locator".into()))?;
    let string = |name: &str| {
        direct
            .get(name)
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(str::to_string)
            .ok_or_else(|| FrameError::Protocol(format!("direct locator omitted {name}")))
    };
    let pane = direct
        .get("pane")
        .filter(|value| !value.is_null())
        .map(
            |value| -> Result<gobby_terminal::protocol::PaneLocator, FrameError> {
                let pane = value.as_object().ok_or_else(|| {
                    FrameError::Protocol("direct pane locator is not an object".into())
                })?;
                let pane_string = |name: &str| {
                    pane.get(name)
                        .and_then(Value::as_str)
                        .filter(|value| !value.is_empty())
                        .map(str::to_string)
                        .ok_or_else(|| {
                            FrameError::Protocol(format!("direct pane locator omitted {name}"))
                        })
                };
                let server_pid = pane
                    .get("server_pid")
                    .and_then(Value::as_i64)
                    .and_then(|value| i32::try_from(value).ok())
                    .ok_or_else(|| {
                        FrameError::Protocol("direct pane locator omitted server_pid".into())
                    })?;
                let server_start_time = pane
                    .get("server_start_time")
                    .and_then(Value::as_i64)
                    .ok_or_else(|| {
                        FrameError::Protocol("direct pane locator omitted server_start_time".into())
                    })?;
                Ok(gobby_terminal::protocol::PaneLocator {
                    socket_path: pane_string("socket_path")?,
                    pane_id: pane_string("pane_id")?,
                    server_pid,
                    server_start_time,
                })
            },
        )
        .transpose()?;
    Ok(AttachLocator {
        backend: reply
            .get("backend")
            .and_then(Value::as_str)
            .unwrap_or("native")
            .to_string(),
        frame_host_epoch: string("host_epoch")?,
        host_terminal_id: string("host_terminal_id")?,
        frame_socket_path: string("frame_socket_path")?,
        pane,
    })
}
