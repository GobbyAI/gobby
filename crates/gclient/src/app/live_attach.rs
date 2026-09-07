//! Frame-source attachment for live panes: the direct and proxy transports,
//! their recovery, and the direct locator an attach reply carries.

use super::*;
use crate::frame_source::{ProxyFrameSource, UnixSocketFrameSource};

enum ProxyAttachOutcome {
    Attached(Value, String, ProxyFrameSource),
    Refused { code: String, reason: String },
}

impl Workspace<LiveDaemon> {
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
            if self.frame_delivery.allows(Transport::Direct)
                && self.panes[&pane_id].direct_available
            {
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
        // The only route to the proxy transport, for both a row with no direct
        // locator and the fallback after a direct attach dies. Refusing here
        // rather than at each caller keeps `--frame-delivery direct` honest:
        // a broken direct path stays visible instead of downgrading silently.
        if !self.frame_delivery.allows(Transport::Proxy) {
            self.panes
                .get_mut(&pane_id)
                .expect("pane exists")
                .refuse_attach(
                    "frame_delivery_direct_only",
                    "proxy delivery declined by --frame-delivery direct",
                );
            self.attached_generation.insert(pane_id, generation);
            self.clear_fallback_flight(pane_id);
            return Ok(());
        }
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
    // Same split as `row_has_direct_locator`: a pane locator carries the
    // identity on its own, and the frame host discards `host_terminal_id` the
    // moment one is present. It stays required when there is no pane, because
    // then it is the only thing naming the terminal.
    let host_terminal_id = if pane.is_some() {
        string("host_terminal_id").unwrap_or_default()
    } else {
        string("host_terminal_id")?
    };
    Ok(AttachLocator {
        backend: reply
            .get("backend")
            .and_then(Value::as_str)
            .unwrap_or("native")
            .to_string(),
        frame_host_epoch: string("host_epoch")?,
        host_terminal_id,
        frame_socket_path: string("frame_socket_path")?,
        pane,
    })
}

#[cfg(test)]
#[path = "live_attach/tests.rs"]
mod tests;
