//! Daemon frame recovery: a failed pane's detach, its re-attach and the
//! host re-register, each a step the live loop polls beside itself. Every
//! attempt leaves records in `gclient.log`: a WARN when a frame error
//! starts it and when it gives up, and an INFO when a source replaces the
//! failed one.

use super::*;

impl Workspace<LiveDaemon> {
    /// Starts recovering a pane whose frame source failed. The waits run in
    /// the returned step, which the live loop polls beside itself: awaited
    /// inline, one pane's detach held the loop while every other pane's
    /// receiver lagged into a recovery of its own (#22747).
    pub(in crate::app) fn begin_frame_recovery(
        &mut self,
        pane_id: PaneId,
        error: &FrameError,
    ) -> Result<Option<RecoveryFuture>, FrameError> {
        match error {
            FrameError::Finalized { .. } => {
                self.retire_pane_attachment(pane_id);
                Ok(None)
            }
            FrameError::Eof
            | FrameError::Lag
            | FrameError::Cancelled
            | FrameError::Io(_)
            | FrameError::Protocol(_)
            | FrameError::Daemon(_)
            | FrameError::AttachRefused { .. } => {
                let record = self.pane_record(pane_id, None);
                let next = self.begin_proxy_recovery(pane_id, error)?;
                if let (Some(_), Some(record)) = (&next, record) {
                    record.warn("frame_recovery_started", error_class(error));
                }
                Ok(next)
            }
            // A refused control request never reaches a frame source; nothing
            // to recover. A full host-input queue is the same: the stream is
            // healthy and one keystroke was dropped, which `send_host_input`
            // already put in the pane's status line (#22573).
            FrameError::Refused(_) | FrameError::Backpressure => Ok(None),
            // No recovery path either, but not silent: the loop shows what
            // the source reported, so a host swap under a pane is visible.
            FrameError::HostEpochChanged { expected, actual } => {
                Err(FrameError::HostEpochChanged {
                    expected: expected.clone(),
                    actual: actual.clone(),
                })
            }
            FrameError::Other(detail) => Err(FrameError::Other(detail.clone())),
        }
    }

    /// Detaches the failed attachment, or attaches straight away when there
    /// is none; a pane already recovering is left to that recovery.
    fn begin_proxy_recovery(
        &mut self,
        pane_id: PaneId,
        error: &FrameError,
    ) -> Result<Option<RecoveryFuture>, FrameError> {
        if !self.panes.contains_key(&pane_id) {
            return Err(FrameError::Protocol("unknown pane".into()));
        }
        Ok(self.begin_daemon_recovery(pane_id, &error.to_string()))
    }

    /// The daemon re-attach path: a failed or cancelled host-local reconnect
    /// lands here unchanged (#23076). `reason` is why the source was given up.
    pub(in crate::app) fn begin_daemon_recovery(
        &mut self,
        pane_id: PaneId,
        reason: &str,
    ) -> Option<RecoveryFuture> {
        self.host_recovering.remove(&pane_id);
        self.host_recovered.remove(&pane_id);
        let pane = self.panes.get_mut(&pane_id)?;
        if pane.fallback_in_flight {
            return None;
        }
        promotion::log_direct_fallback(pane, reason);
        pane.fallback_in_flight = true;
        let terminal_id = pane.terminal_id.clone();
        let detaching = pane
            .begin_detaching(tokio::time::Instant::now())
            .map(|(attachment, _)| {
                let deadline = pane.detaching_deadline().expect("detaching deadline");
                (attachment, deadline)
            })
            .filter(|(attachment, _)| !attachment.is_empty());
        // The failed source is never polled again, whatever state it left.
        let _ = pane.take_frame_source();
        self.attached_generation.remove(&pane_id);
        let generation = self.daemon.generation();
        let started = Instant::now();
        let Some((old_attachment, deadline)) = detaching else {
            return self.begin_recovery_attach(pane_id, terminal_id, generation, started);
        };
        let daemon = self.daemon.clone();
        Some(Box::pin(async move {
            let outcome = detach_attachment(&daemon, &terminal_id, &old_attachment, deadline).await;
            Recovery {
                pane_id,
                generation,
                started,
                step: RecoveryStep::Detached {
                    terminal_id,
                    old_attachment,
                    outcome,
                },
            }
        }))
    }

    /// A host-local reconnect for a pane whose frame source just died, when
    /// the pane has a direct locator to use. `None` sends the caller down the
    /// daemon path (#23076).
    pub(in crate::app) fn begin_host_frame_recovery(
        &mut self,
        pane_id: PaneId,
        error: &FrameError,
    ) -> Option<(
        std::sync::Arc<super::live_loop::host_recovery::HostCancel>,
        super::live_loop::host_recovery::HostRecoveryFuture,
    )> {
        if !matches!(
            error,
            FrameError::Eof
                | FrameError::Lag
                | FrameError::Cancelled
                | FrameError::Io(_)
                | FrameError::Protocol(_)
                | FrameError::Daemon(_)
        ) {
            return None;
        }
        let record = self.pane_record(pane_id, Some("host_reconnect"));
        let started = self.begin_host_recovery(pane_id);
        if let (Some(_), Some(record)) = (&started, record) {
            record.warn("frame_recovery_started", error_class(error));
        }
        started
    }

    /// A host reconnect that restored the stream closes the attempt
    /// `begin_host_frame_recovery` opened.
    pub(in crate::app) fn host_recovery_completed(&self, pane_id: PaneId, started: Instant) {
        if let Some(mut record) = self.pane_record(pane_id, Some("host_reconnect")) {
            record.started = started;
            record.completed(Transport::Direct);
        }
    }

    pub(in crate::app) fn host_recovery_stale(&self, pane_id: PaneId, started: Instant) {
        if let Some(mut record) = self.pane_record(pane_id, Some("host_reconnect")) {
            record.started = started;
            record.warn("frame_recovery_gave_up", "stale");
        }
    }

    /// Applies one recovery step, returning the next when there is one.
    pub(in crate::app) fn apply_frame_recovery(
        &mut self,
        recovery: Recovery,
    ) -> Result<Option<RecoveryFuture>, FrameError> {
        let Recovery {
            pane_id,
            generation,
            started,
            step,
        } = recovery;
        let record = RecoveryRecord::of_step(pane_id, generation, started, &step);
        // A closed pane has nothing to recover, and after a reconnect the
        // reconcile re-attaches every pane. A superseded result never
        // installs its source. An attachment it won on the current
        // connection is released so the daemon is not left holding it; one
        // won on an older connection died with that socket, which the daemon
        // finalizes on close. A promotion releases what it won itself. The
        // pane's flight flag is left alone: the reconnect clears it, and
        // otherwise it belongs to the attach that superseded this one.
        if !matches!(step, RecoveryStep::Promoted { .. })
            && !self.step_is_current(pane_id, generation, &step)
        {
            if let Some(attachment) = step
                .won_attachment()
                .filter(|_| self.daemon.generation() == generation)
            {
                self.release_attachment(&record.terminal_id, attachment);
            }
            record.warn("frame_recovery_gave_up", "stale");
            return Ok(None);
        }
        match step {
            RecoveryStep::Detached {
                terminal_id,
                old_attachment,
                outcome,
            } => {
                let reason = match outcome {
                    DetachOutcome::Retired(reason) => reason,
                    DetachOutcome::Failed(error) => {
                        record.warn("frame_recovery_gave_up", error_class(&error));
                        self.defer_pane_attach(pane_id, "detach_failed", &error);
                        self.clear_fallback_flight(pane_id);
                        return Err(error);
                    }
                    DetachOutcome::Expired => {
                        record.warn("frame_recovery_gave_up", "deadline");
                        let error = FrameError::Other("detach deadline expired".into());
                        self.defer_pane_attach(pane_id, "detach_failed", &error);
                        self.clear_fallback_flight(pane_id);
                        return Ok(None);
                    }
                };
                if !self
                    .panes
                    .get_mut(&pane_id)
                    .expect("pane exists")
                    .retire_attachment(&old_attachment, reason)
                {
                    record.warn("frame_recovery_gave_up", "stale");
                    self.clear_fallback_flight(pane_id);
                    return Ok(None);
                }
                Ok(self.begin_recovery_attach(pane_id, terminal_id, generation, started))
            }
            RecoveryStep::Attached { outcome, .. } => {
                match &outcome {
                    Ok(ProxyAttachOutcome::Attached(_, attachment, _))
                        if self.panes[&pane_id].tombstones.contains(attachment) =>
                    {
                        record.warn("frame_recovery_gave_up", "tombstoned");
                    }
                    Ok(ProxyAttachOutcome::Attached(..)) => record.completed(Transport::Proxy),
                    Ok(ProxyAttachOutcome::Refused { code, .. }) => {
                        record.warn("frame_recovery_gave_up", code);
                    }
                    Err(error) => record.warn("frame_recovery_gave_up", error_class(error)),
                }
                self.finish_proxy_attach(pane_id, generation, outcome);
                Ok(None)
            }
            RecoveryStep::DirectAttached {
                terminal_id,
                outcome,
                ..
            } => {
                match &outcome {
                    Ok(_) => record.completed(Transport::Direct),
                    Err(error @ FrameError::Finalized { .. }) => {
                        record.warn("frame_recovery_gave_up", error_class(error));
                    }
                    Err(error) if error_class(error) == "timeout" => {
                        record.warn("direct_attach_timed_out", "timeout");
                    }
                    // The proxy attach it falls back to closes the attempt.
                    Err(_) => {}
                }
                if self.finish_direct_attach(pane_id, generation, outcome) {
                    self.clear_fallback_flight(pane_id);
                    return Ok(None);
                }
                Ok(self.begin_recovery_attach(pane_id, terminal_id, generation, started))
            }
            RecoveryStep::Reregistered { outcome, .. } => {
                self.clear_fallback_flight(pane_id);
                let (reply, attachment) = match outcome {
                    Ok(attached) => attached,
                    // No fresh attachment to adopt: the full daemon re-attach
                    // replaces the host stream as well.
                    Err(error) => {
                        return Ok(self.begin_daemon_recovery(pane_id, &error.to_string()))
                    }
                };
                record.completed(Transport::Direct);
                self.host_recovered.remove(&pane_id);
                let lease_generation = reply
                    .get("lease_generation")
                    .and_then(Value::as_u64)
                    .unwrap_or_default();
                let pane = self.panes.get_mut(&pane_id).expect("pane exists");
                pane.adopt_attachment(attachment, generation, lease_generation);
                pane.observe_lease_holder(
                    reply
                        .get("lease_holder")
                        .and_then(|holder| holder.get("attachment_id"))
                        .and_then(Value::as_str),
                );
                self.attached_generation.insert(pane_id, generation);
                // The carried grant named the dead attachment; the focused
                // pane asks again under the fresh one.
                if self.focus == Some(pane_id) {
                    self.request_control(pane_id, false);
                }
                Ok(None)
            }
            RecoveryStep::Promoted {
                terminal_id,
                proxy_attachment,
                outcome,
            } => {
                self.apply_promotion(
                    pane_id,
                    generation,
                    &terminal_id,
                    &proxy_attachment,
                    outcome,
                );
                Ok(None)
            }
        }
    }

    /// Whether a step's result still belongs to its pane: the pane is open,
    /// the connection it was issued on is the current one, and an attach
    /// result answers the attach the pane is still waiting on.
    fn step_is_current(
        &self,
        pane_id: PaneId,
        generation: Generation,
        step: &RecoveryStep,
    ) -> bool {
        let Some(pane) = self.panes.get(&pane_id) else {
            return false;
        };
        if self.daemon.generation() != generation {
            return false;
        }
        match step {
            RecoveryStep::Attached { request_id, .. }
            | RecoveryStep::DirectAttached { request_id, .. } => matches!(
                &pane.attach,
                AttachState::Attaching {
                    request_id: waiting,
                    generation: attaching,
                    ..
                } if waiting == request_id && *attaching == generation
            ),
            _ => true,
        }
    }

    /// The context a record names for `pane_id` as it stands now, before a
    /// recovery takes its attachment apart. `stage` defaults to the step
    /// the recovery starts with.
    fn pane_record(&self, pane_id: PaneId, stage: Option<&'static str>) -> Option<RecoveryRecord> {
        let pane = self.panes.get(&pane_id)?;
        let attachment = pane.attachment_id();
        let attachment_id = (!attachment.is_empty()).then(|| attachment.to_string());
        let stage = stage.unwrap_or(if attachment_id.is_some() {
            "detach"
        } else {
            "proxy_attach"
        });
        Some(RecoveryRecord {
            pane_id,
            terminal_id: pane.terminal_id.clone(),
            attachment_id,
            transport: pane.transport(),
            generation: self.daemon.generation(),
            stage,
            started: Instant::now(),
        })
    }

    /// Starts a proxy attach for a recovering pane; `started` is when its
    /// attempt began.
    pub(super) fn begin_recovery_attach(
        &mut self,
        pane_id: PaneId,
        terminal_id: String,
        generation: Generation,
        started: Instant,
    ) -> Option<RecoveryFuture> {
        let Some(request_id) = self.begin_proxy_attach(pane_id, generation) else {
            RecoveryRecord {
                pane_id,
                terminal_id,
                attachment_id: None,
                transport: Some(Transport::Proxy),
                generation,
                stage: "proxy_attach",
                started,
            }
            .warn("frame_recovery_gave_up", "frame_delivery_direct_only");
            return None;
        };
        let daemon = self.daemon.clone();
        Some(Box::pin(async move {
            let outcome = request_proxy_source(&daemon, &terminal_id, &request_id).await;
            Recovery {
                pane_id,
                generation,
                started,
                step: RecoveryStep::Attached {
                    terminal_id,
                    request_id,
                    outcome,
                },
            }
        }))
    }

    /// Re-register a host-restored pane with a daemon on a new generation.
    /// The attachment it kept died with the old socket, so it takes a fresh
    /// one and keeps its host stream, which rebinds on its next write.
    pub(in crate::app) fn begin_host_reregister(
        &mut self,
        pane_id: PaneId,
        generation: Generation,
    ) -> RecoveryFuture {
        let pane = self.panes.get_mut(&pane_id).expect("pane exists");
        // The re-register owns its pane until it lands, as a recovery does.
        pane.fallback_in_flight = true;
        let terminal_id = pane.terminal_id.clone();
        let daemon = self.daemon.clone();
        let started = Instant::now();
        Box::pin(async move {
            let request_id = uuid::Uuid::new_v4().to_string();
            let outcome = request_direct_attachment(&daemon, &terminal_id, &request_id).await;
            Recovery {
                pane_id,
                generation,
                started,
                step: RecoveryStep::Reregistered {
                    terminal_id,
                    outcome,
                },
            }
        })
    }

    /// Hands the panes of recoveries a reconnect dropped to its reconcile.
    pub(in crate::app) fn abandon_frame_recoveries(&mut self) {
        for (pane_id, pane) in self.panes.iter_mut() {
            pane.promoting = false;
            // A host-local reconnect is not a daemon recovery: a daemon
            // generation change must not clear its flight flag or detach the
            // attachment it is keeping (#23076).
            if self.host_recovering.contains(pane_id) {
                continue;
            }
            pane.fallback_in_flight = false;
            // A canceled recovery cannot complete this attach on the new socket,
            // and its "attaching" status would read as a settled first frame.
            if matches!(&pane.attach, AttachState::Attaching { .. }) {
                pane.attach = AttachState::Detached;
                pane.status_message = None;
            }
        }
    }
}

impl RecoveryStep {
    /// The attachment an attach step won, which only installing its source
    /// keeps.
    fn won_attachment(&self) -> Option<&str> {
        match self {
            RecoveryStep::Attached {
                outcome: Ok(ProxyAttachOutcome::Attached(_, attachment, _)),
                ..
            }
            | RecoveryStep::DirectAttached {
                outcome: Ok((_, attachment, _, _)),
                ..
            }
            | RecoveryStep::Reregistered {
                outcome: Ok((_, attachment)),
                ..
            } => Some(attachment),
            _ => None,
        }
    }
}

/// What every recovery record names, so each line in `gclient.log` says
/// which pane and attachment it is about and how long the attempt has run.
struct RecoveryRecord {
    pane_id: PaneId,
    terminal_id: String,
    /// The attachment the step detached or won, when there is one.
    attachment_id: Option<String>,
    transport: Option<Transport>,
    generation: Generation,
    stage: &'static str,
    started: Instant,
}

impl RecoveryRecord {
    fn of_step(
        pane_id: PaneId,
        generation: Generation,
        started: Instant,
        step: &RecoveryStep,
    ) -> Self {
        let (terminal_id, stage, transport) = match step {
            RecoveryStep::Detached { terminal_id, .. } => (terminal_id, "detach", None),
            RecoveryStep::Attached { terminal_id, .. } => {
                (terminal_id, "proxy_attach", Some(Transport::Proxy))
            }
            RecoveryStep::DirectAttached { terminal_id, .. } => {
                (terminal_id, "direct_attach", Some(Transport::Direct))
            }
            RecoveryStep::Reregistered { terminal_id, .. } => {
                (terminal_id, "reregister", Some(Transport::Direct))
            }
            RecoveryStep::Promoted { terminal_id, .. } => {
                (terminal_id, "promote", Some(Transport::Direct))
            }
        };
        let attachment_id = match step {
            RecoveryStep::Detached { old_attachment, .. } => Some(old_attachment.as_str()),
            _ => step.won_attachment(),
        };
        Self {
            pane_id,
            terminal_id: terminal_id.clone(),
            attachment_id: attachment_id.map(str::to_owned),
            transport,
            generation,
            stage,
            started,
        }
    }

    fn warn(&self, record: &str, error_class: &str) {
        tracing::warn!(
            pane_id = self.pane_id.0,
            terminal_id = %self.terminal_id,
            attachment_id = self.attachment_id.as_deref(),
            transport = self.transport.map(transport_name),
            generation = self.generation.0,
            stage = self.stage,
            error_class,
            elapsed_ms = elapsed_ms(self.started),
            "{record}"
        );
    }

    /// Closes the attempt: `replacement` now delivers the pane's frames.
    fn completed(&self, replacement: Transport) {
        tracing::info!(
            pane_id = self.pane_id.0,
            terminal_id = %self.terminal_id,
            attachment_id = self.attachment_id.as_deref(),
            transport = transport_name(replacement),
            generation = self.generation.0,
            stage = self.stage,
            elapsed_ms = elapsed_ms(self.started),
            "frame_recovery_completed"
        );
    }
}

fn transport_name(transport: Transport) -> &'static str {
    match transport {
        Transport::Direct => "direct",
        Transport::Proxy => "proxy",
    }
}

fn elapsed_ms(started: Instant) -> u64 {
    u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX)
}

/// A short, stable name for what went wrong, for the records above.
/// Timeouts reach a frame source as text: the frame socket's connect and
/// handshake steps say "... timed out" (`frame_source.rs`), and a daemon
/// request that missed its deadline says "... in time." (`DaemonError::Timeout`).
fn error_class(error: &FrameError) -> &'static str {
    match error {
        FrameError::Io(detail) if detail.ends_with("timed out") => "timeout",
        FrameError::Daemon(detail) if detail.ends_with("in time.") => "timeout",
        FrameError::HostEpochChanged { .. } => "host_epoch_changed",
        FrameError::Eof => "eof",
        FrameError::Lag => "lag",
        FrameError::Cancelled => "cancelled",
        FrameError::Finalized { .. } => "finalized",
        FrameError::AttachRefused { .. } => "attach_refused",
        FrameError::Io(_) => "io",
        FrameError::Protocol(_) => "protocol",
        FrameError::Other(_) => "other",
        FrameError::Refused(_) => "refused",
        FrameError::Daemon(_) => "daemon",
        FrameError::Backpressure => "backpressure",
    }
}

/// Detaches `attachment_id`, done when the daemon answers or finalizes it,
/// whichever comes first, or when `deadline` passes.
async fn detach_attachment(
    daemon: &LiveDaemon,
    terminal_id: &str,
    attachment_id: &str,
    deadline: tokio::time::Instant,
) -> DetachOutcome {
    let (_, mut receiver) = daemon.subscribe();
    let detach = daemon.send(json!({
        "type": "terminal_detach",
        "request_id": uuid::Uuid::new_v4().to_string(),
        "terminal_id": terminal_id,
        "attachment_id": attachment_id,
    }));
    tokio::pin!(detach);
    loop {
        tokio::select! {
            reply = &mut detach => return detach_reply(reply),
            event = receiver.recv() => match event {
                Ok(DaemonEvent::AttachmentFinalized { attachment_id: finalized, payload, .. })
                    if finalized == attachment_id =>
                {
                    let reason = payload.get("reason").and_then(Value::as_str);
                    return DetachOutcome::Retired(reason.map(str::to_owned));
                }
                Ok(_) => {}
                // A lagged receiver may have lost the finalization; the
                // answer alone settles it.
                Err(_) => {
                    return match tokio::time::timeout_at(deadline, &mut detach).await {
                        Ok(reply) => detach_reply(reply),
                        Err(_) => DetachOutcome::Expired,
                    };
                }
            },
            _ = tokio::time::sleep_until(deadline) => return DetachOutcome::Expired,
        }
    }
}

fn detach_reply(reply: Result<Value, DaemonError>) -> DetachOutcome {
    let reply = match reply {
        Ok(reply) => reply,
        Err(error) => return DetachOutcome::Failed(FrameError::from(error)),
    };
    let reason = reply.get("reason").and_then(Value::as_str);
    if reply.get("success").and_then(Value::as_bool) != Some(true) {
        let reason = reason.unwrap_or("terminal detach refused");
        return DetachOutcome::Failed(FrameError::Protocol(reason.to_string()));
    }
    DetachOutcome::Retired(reason.map(str::to_owned))
}
