//! Daemon frame recovery: a failed pane's detach, its re-attach and the
//! host re-register, each a step the live loop polls beside itself.

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
            | FrameError::AttachRefused { .. } => self.begin_proxy_recovery(pane_id, error),
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
        let Some((old_attachment, deadline)) = detaching else {
            return self.begin_recovery_attach(pane_id, terminal_id, generation);
        };
        let daemon = self.daemon.clone();
        Some(Box::pin(async move {
            let outcome = detach_attachment(&daemon, &terminal_id, &old_attachment, deadline).await;
            Recovery {
                pane_id,
                generation,
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
        self.begin_host_recovery(pane_id)
    }

    /// Applies one recovery step, returning the next when there is one.
    pub(in crate::app) fn apply_frame_recovery(
        &mut self,
        recovery: Recovery,
    ) -> Result<Option<RecoveryFuture>, FrameError> {
        let Recovery {
            pane_id,
            generation,
            step,
        } = recovery;
        // A closed pane has nothing to recover, and after a reconnect the
        // reconcile re-attaches every pane. A promotion releases what it won.
        if !matches!(step, RecoveryStep::Promoted { .. })
            && (!self.panes.contains_key(&pane_id) || self.daemon.generation() != generation)
        {
            self.clear_fallback_flight(pane_id);
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
                        self.defer_pane_attach(pane_id, "detach_failed", &error);
                        self.clear_fallback_flight(pane_id);
                        return Err(error);
                    }
                    DetachOutcome::Expired => {
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
                    self.clear_fallback_flight(pane_id);
                    return Ok(None);
                }
                Ok(self.begin_recovery_attach(pane_id, terminal_id, generation))
            }
            RecoveryStep::Attached { outcome } => {
                self.finish_proxy_attach(pane_id, generation, outcome);
                Ok(None)
            }
            RecoveryStep::DirectAttached {
                terminal_id,
                outcome,
            } => {
                if self.finish_direct_attach(pane_id, generation, outcome) {
                    self.clear_fallback_flight(pane_id);
                    return Ok(None);
                }
                Ok(self.begin_recovery_attach(pane_id, terminal_id, generation))
            }
            RecoveryStep::Reregistered { outcome } => {
                self.clear_fallback_flight(pane_id);
                let (reply, attachment) = match outcome {
                    Ok(attached) => attached,
                    // No fresh attachment to adopt: the full daemon re-attach
                    // replaces the host stream as well.
                    Err(error) => {
                        return Ok(self.begin_daemon_recovery(pane_id, &error.to_string()))
                    }
                };
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
        Box::pin(async move {
            let request_id = uuid::Uuid::new_v4().to_string();
            let outcome = request_direct_attachment(&daemon, &terminal_id, &request_id).await;
            Recovery {
                pane_id,
                generation,
                step: RecoveryStep::Reregistered { outcome },
            }
        })
    }

    pub(super) fn begin_recovery_attach(
        &mut self,
        pane_id: PaneId,
        terminal_id: String,
        generation: Generation,
    ) -> Option<RecoveryFuture> {
        let request_id = self.begin_proxy_attach(pane_id, generation)?;
        let daemon = self.daemon.clone();
        Some(Box::pin(async move {
            let outcome = request_proxy_source(&daemon, &terminal_id, &request_id).await;
            Recovery {
                pane_id,
                generation,
                step: RecoveryStep::Attached { outcome },
            }
        }))
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
            // A canceled recovery cannot complete this attach on the new socket.
            if matches!(&pane.attach, AttachState::Attaching { .. }) {
                pane.attach = AttachState::Detached;
            }
        }
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
