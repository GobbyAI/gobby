use super::*;
use crate::frame_source::{ProxyFrameSource, UnixSocketFrameSource};

enum ProxyAttachOutcome {
    Attached(Value, String, ProxyFrameSource),
    Refused { code: String, reason: String },
}

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
            pending_attention: None,
            gobby_home: None,
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

    pub(super) async fn attach_ready_panes(&mut self) -> Result<(), DaemonError> {
        let snapshot = self.daemon.subscribe().0;
        if !snapshot.ready {
            return Ok(());
        }
        for pane_id in self.order.clone() {
            if self.attached_generation.get(&pane_id) == Some(&snapshot.generation)
                || self.panes[&pane_id].attached_generation() == Some(snapshot.generation)
            {
                continue;
            }
            let terminal_id = self.panes[&pane_id].terminal_id.clone();
            if self.panes[&pane_id].direct_available {
                let request_id = uuid::Uuid::new_v4().to_string();
                self.panes
                    .get_mut(&pane_id)
                    .expect("pane exists")
                    .begin_attaching(request_id.clone(), Transport::Direct, snapshot.generation);
                match self.request_direct_source(&terminal_id, &request_id).await {
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
            self.begin_live_proxy_attach(pane_id, &terminal_id, snapshot.generation)
                .await?;
        }
        Ok(())
    }

    async fn request_direct_source(
        &self,
        terminal_id: &str,
        request_id: &str,
    ) -> Result<(Value, String, AttachLocator, UnixSocketFrameSource), FrameError> {
        let reply = self
            .daemon
            .send(json!({
                "type": "terminal_attach",
                "request_id": request_id,
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
        let generation = self.daemon.subscribe().0.generation;
        let pane = self.panes.get_mut(&pane_id).expect("pane exists");
        if let Some(backend) = reply.get("backend").and_then(Value::as_str) {
            pane.backend = backend.to_string();
        }
        pane.expected_host_epoch = locator.frame_host_epoch.clone();
        let lease_generation = reply
            .get("lease_generation")
            .and_then(Value::as_u64)
            .unwrap_or_default();
        pane.install_attachment(
            attachment,
            Transport::Direct,
            generation,
            lease_generation,
            PaneFrameSource::Direct(source),
        );
    }

    pub async fn recv_live_frame(&mut self, pane_id: PaneId) -> Result<ServerMessage, FrameError> {
        let result = self.recv_pane_frame(pane_id).await;
        let Err(error) = &result else {
            return result;
        };
        self.recover_live_frame_error(pane_id, error).await?;
        result
    }

    pub(super) async fn recover_live_frame_error(
        &mut self,
        pane_id: PaneId,
        error: &FrameError,
    ) -> Result<(), FrameError> {
        match error {
            FrameError::Finalized { .. } => self.retire_pane_attachment(pane_id),
            FrameError::Eof
            | FrameError::Lag
            | FrameError::Cancelled
            | FrameError::Io(_)
            | FrameError::Protocol(_) => self.recover_proxy_source(pane_id).await?,
            FrameError::HostEpochChanged { .. } | FrameError::Other(_) => {}
        }
        Ok(())
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
        pane.direct_available = false;
        let terminal_id = pane.terminal_id.clone();
        let (old_attachment, deadline) = pane
            .begin_detaching(tokio::time::Instant::now())
            .map(|(attachment, _)| {
                let deadline = pane.detaching_deadline().expect("detaching deadline");
                (attachment, deadline)
            })
            .unwrap_or_else(|| (String::new(), tokio::time::Instant::now()));
        self.attached_generation.remove(&pane_id);

        if !old_attachment.is_empty() {
            let daemon = self.daemon.clone();
            let (_, mut receiver) = daemon.subscribe();
            let detach = daemon.send(json!({
                "type": "terminal_detach",
                "request_id": uuid::Uuid::new_v4().to_string(),
                "terminal_id": terminal_id,
                "attachment_id": old_attachment,
            }));
            tokio::pin!(detach);
            let reason = loop {
                tokio::select! {
                    reply = &mut detach => {
                        let reply = reply?;
                        if reply.get("success").and_then(Value::as_bool) != Some(true) {
                            self.clear_fallback_flight(pane_id);
                            return Err(FrameError::Protocol(
                                reply.get("reason").and_then(Value::as_str)
                                    .unwrap_or("terminal detach refused").to_string()
                            ));
                        }
                        break reply.get("reason").and_then(Value::as_str).map(str::to_owned);
                    }
                    event = receiver.recv() => {
                        match event {
                            Ok(DaemonEvent::AttachmentFinalized { attachment_id, payload, .. })
                                if attachment_id == old_attachment =>
                            {
                                break payload.get("reason").and_then(Value::as_str).map(str::to_owned);
                            }
                            Ok(_) => {}
                            Err(_) => {
                                let reply = tokio::time::timeout_at(deadline, &mut detach)
                                    .await
                                    .map_err(|_| FrameError::Other("detach deadline expired".into()))??;
                                if reply.get("success").and_then(Value::as_bool) != Some(true) {
                                    return Err(FrameError::Protocol("terminal detach refused".into()));
                                }
                                break reply.get("reason").and_then(Value::as_str).map(str::to_owned);
                            }
                        }
                    }
                    _ = tokio::time::sleep_until(deadline) => return Ok(()),
                }
            };
            if !self
                .panes
                .get_mut(&pane_id)
                .expect("pane exists")
                .retire_attachment(&old_attachment, reason)
            {
                self.clear_fallback_flight(pane_id);
                return Ok(());
            }
        }

        self.begin_live_proxy_attach(pane_id, &terminal_id, self.daemon.generation())
            .await
            .map_err(|error| FrameError::Other(error.to_string()))
    }

    async fn request_proxy_source(
        &self,
        terminal_id: &str,
        request_id: &str,
    ) -> Result<ProxyAttachOutcome, FrameError> {
        let (_, receiver) = self.daemon.subscribe();
        let reply = self
            .daemon
            .send(json!({
                "type": "terminal_attach",
                "request_id": request_id,
                "terminal_id": terminal_id,
                "frame_delivery": "proxy",
                "encoding": "semantic_frame",
            }))
            .await?;
        if reply.get("success").and_then(Value::as_bool) != Some(true) {
            return Ok(ProxyAttachOutcome::Refused {
                code: reply
                    .get("code")
                    .and_then(Value::as_str)
                    .unwrap_or("attach_refused")
                    .to_string(),
                reason: reply
                    .get("reason")
                    .and_then(Value::as_str)
                    .unwrap_or("terminal attach refused")
                    .to_string(),
            });
        }
        let attachment = reply
            .get("attachment_id")
            .and_then(Value::as_str)
            .ok_or_else(|| FrameError::Protocol("attach result omitted attachment_id".into()))?
            .to_string();
        let mut source = ProxyFrameSource::from_attachment(
            self.daemon.clone(),
            terminal_id,
            attachment.clone(),
            receiver,
        )?;
        let rows = reply
            .get("rows")
            .and_then(Value::as_u64)
            .and_then(|value| u16::try_from(value).ok())
            .unwrap_or(24);
        let cols = reply
            .get("cols")
            .and_then(Value::as_u64)
            .and_then(|value| u16::try_from(value).ok())
            .unwrap_or(80);
        source
            .send(&ClientMessage::SetViewport { rows, cols })
            .await?;
        Ok(ProxyAttachOutcome::Attached(reply, attachment, source))
    }

    async fn begin_live_proxy_attach(
        &mut self,
        pane_id: PaneId,
        terminal_id: &str,
        generation: Generation,
    ) -> Result<(), DaemonError> {
        let request_id = uuid::Uuid::new_v4().to_string();
        self.panes
            .get_mut(&pane_id)
            .expect("pane exists")
            .begin_attaching(request_id.clone(), Transport::Proxy, generation);
        match self.request_proxy_source(terminal_id, &request_id).await {
            Ok(ProxyAttachOutcome::Attached(reply, attachment, source)) => {
                if self.panes[&pane_id].tombstones.contains(&attachment) {
                    self.panes
                        .get_mut(&pane_id)
                        .expect("pane exists")
                        .refuse_attach(
                            "attachment_reused",
                            "daemon reused a tombstoned attachment id",
                        );
                } else {
                    self.install_proxy_source(pane_id, &reply, attachment, source);
                }
                self.attached_generation.insert(pane_id, generation);
                self.clear_fallback_flight(pane_id);
                Ok(())
            }
            Ok(ProxyAttachOutcome::Refused { code, reason }) => {
                self.panes
                    .get_mut(&pane_id)
                    .expect("pane exists")
                    .refuse_attach(&code, &reason);
                self.attached_generation.insert(pane_id, generation);
                self.clear_fallback_flight(pane_id);
                Ok(())
            }
            Err(FrameError::Finalized { code, reason }) => {
                self.panes
                    .get_mut(&pane_id)
                    .expect("pane exists")
                    .refuse_attach(&code, &reason);
                self.attached_generation.insert(pane_id, generation);
                self.clear_fallback_flight(pane_id);
                Ok(())
            }
            Err(error) => {
                self.clear_fallback_flight(pane_id);
                Err(DaemonError::Protocol {
                    detail: error.to_string(),
                })
            }
        }
    }

    fn install_proxy_source(
        &mut self,
        pane_id: PaneId,
        reply: &Value,
        attachment: String,
        source: ProxyFrameSource,
    ) {
        let generation = self.daemon.subscribe().0.generation;
        let pane = self.panes.get_mut(&pane_id).expect("pane exists");
        if let Some(backend) = reply.get("backend").and_then(Value::as_str) {
            pane.backend = backend.to_string();
        }
        let lease_generation = reply
            .get("lease_generation")
            .and_then(Value::as_u64)
            .unwrap_or_default();
        pane.install_attachment(
            attachment,
            Transport::Proxy,
            generation,
            lease_generation,
            PaneFrameSource::Proxy(source),
        );
    }

    fn clear_fallback_flight(&mut self, pane_id: PaneId) {
        if let Some(pane) = self.panes.get_mut(&pane_id) {
            pane.fallback_in_flight = false;
        }
    }

    fn retire_pane_attachment(&mut self, pane_id: PaneId) {
        if let Some(pane) = self.panes.get_mut(&pane_id) {
            let attachment = pane.attachment_id().to_string();
            if attachment.is_empty() {
                pane.refuse_attach("attachment_finalized", "attachment finalized");
            } else {
                pane.retire_attachment(&attachment, Some("attachment finalized".into()));
            }
            pane.fallback_in_flight = false;
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
                    Some("exited" | "terminated") => {
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
