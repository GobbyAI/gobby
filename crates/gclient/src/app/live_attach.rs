//! Frame-source attachment for live panes: the direct and proxy transports,
//! their recovery, and the direct locator an attach reply carries.

use std::future::Future;
use std::path::Path;
use std::pin::Pin;

use super::*;
use crate::frame_source::{ProxyFrameSource, UnixSocketFrameSource};

enum ProxyAttachOutcome {
    Attached(Value, String, ProxyFrameSource),
    Refused { code: String, reason: String },
}

/// A direct attach the daemon granted: its reply, attachment id, locator
/// and the connected source.
pub(super) type DirectAttach = (Value, String, AttachLocator, UnixSocketFrameSource);

/// What one step of a pane's frame recovery came back with.
pub(super) struct Recovery {
    pane_id: PaneId,
    generation: Generation,
    step: RecoveryStep,
}

enum RecoveryStep {
    Detached {
        terminal_id: String,
        old_attachment: String,
        outcome: DetachOutcome,
    },
    Attached {
        outcome: Result<ProxyAttachOutcome, FrameError>,
    },
    /// A retried attach's direct try; failing, it falls back to the proxy.
    DirectAttached {
        terminal_id: String,
        outcome: Result<DirectAttach, FrameError>,
    },
    /// A host-restored pane's fresh daemon attachment on a new generation;
    /// its host stream stays (#23419).
    Reregistered {
        outcome: Result<(Value, String), FrameError>,
    },
}

enum DetachOutcome {
    Retired(Option<String>),
    Failed(FrameError),
    Expired,
}

/// A recovery step in flight; the live loop polls it from a select branch.
pub(super) type RecoveryFuture = Pin<Box<dyn Future<Output = Recovery> + 'static>>;

/// Refusals the daemon expects to clear on their own: the terminal host is
/// still starting, or a host step outran its budget. The pane retries these
/// instead of staying refused until the next reconnect (#22544).
fn attach_refusal_is_transient(code: &str) -> bool {
    matches!(
        code,
        "host_not_ready" | "host_open_timeout" | "proxy_start_timeout"
    )
}

impl Workspace<LiveDaemon> {
    pub(super) async fn attach_ready_panes(&mut self) -> Result<(), DaemonError> {
        let snapshot = self.daemon.subscribe().0;
        if !snapshot.ready || !self.daemon_ready {
            return Ok(());
        }
        let now = tokio::time::Instant::now();
        for pane_id in self.order.clone() {
            // A recovery beside the loop owns its pane until it lands, even
            // once the old attachment's finalization detached it.
            if self.attached_generation.get(&pane_id) == Some(&snapshot.generation)
                || self.panes[&pane_id].attached_generation() == Some(snapshot.generation)
                || self.panes[&pane_id].attach_retry_pending(now)
                || self.panes[&pane_id].fallback_in_flight
                // A pane restored straight onto its host keeps that stream;
                // `start_due_attaches` re-registers it with the daemon (#23419).
                || self.host_recovered.contains(&pane_id)
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
                let outcome = request_direct_source(
                    &self.daemon,
                    self.gobby_home.as_deref(),
                    &terminal_id,
                    &request_id,
                )
                .await;
                if self.finish_direct_attach(pane_id, snapshot.generation, outcome) {
                    continue;
                }
            }
            self.begin_live_proxy_attach(pane_id, &terminal_id, snapshot.generation)
                .await;
        }
        Ok(())
    }

    /// Settle a direct attach: install its source, or retire the pane whose
    /// attachment the daemon finalized. False when the proxy is to be tried
    /// instead.
    fn finish_direct_attach(
        &mut self,
        pane_id: PaneId,
        generation: Generation,
        outcome: Result<DirectAttach, FrameError>,
    ) -> bool {
        match outcome {
            Ok((reply, attachment, locator, source)) => {
                self.install_direct_source(pane_id, &reply, attachment, &locator, source);
            }
            Err(FrameError::Finalized { .. }) => self.retire_pane_attachment(pane_id),
            Err(_) => return false,
        }
        self.attached_generation.insert(pane_id, generation);
        true
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
            pane.backend = Backend::parse(backend);
        }
        pane.expected_host_epoch = locator.frame_host_epoch.clone();
        pane.remember_host_locator(locator);
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
        pane.host_themes = super::theme_sync::host_accepts_themes(reply);
        pane.observe_lease_holder(
            reply
                .get("lease_holder")
                .and_then(|holder| holder.get("attachment_id"))
                .and_then(Value::as_str),
        );
    }

    /// The next frame of one pane, recovering its source inline when it fails.
    pub async fn recv_live_frame(&mut self, pane_id: PaneId) -> Result<ServerMessage, FrameError> {
        let result = self.recv_pane_frame(pane_id).await;
        let Err(error) = &result else {
            return result;
        };
        if let Some(host) = self.begin_host_frame_recovery(pane_id, error) {
            let (_, future) = host;
            let mut step = self.apply_host_recovery(future.await);
            while let Some(recovery) = step {
                step = self.apply_frame_recovery(recovery.await)?;
            }
            return result;
        }
        let mut step = self.begin_frame_recovery(pane_id, error)?;
        while let Some(recovery) = step {
            step = self.apply_frame_recovery(recovery.await)?;
        }
        result
    }

    /// Starts recovering a pane whose frame source failed. The waits run in
    /// the returned step, which the live loop polls beside itself: awaited
    /// inline, one pane's detach held the loop while every other pane's
    /// receiver lagged into a recovery of its own (#22747).
    pub(super) fn begin_frame_recovery(
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
            | FrameError::AttachRefused { .. } => self.begin_proxy_recovery(pane_id),
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
    ) -> Result<Option<RecoveryFuture>, FrameError> {
        if !self.panes.contains_key(&pane_id) {
            return Err(FrameError::Protocol("unknown pane".into()));
        }
        Ok(self.begin_daemon_recovery(pane_id))
    }

    /// The daemon re-attach path, with no error attached: a failed or
    /// cancelled host-local reconnect lands here unchanged (#23076).
    pub(super) fn begin_daemon_recovery(&mut self, pane_id: PaneId) -> Option<RecoveryFuture> {
        self.host_recovering.remove(&pane_id);
        self.host_recovered.remove(&pane_id);
        let pane = self.panes.get_mut(&pane_id)?;
        if pane.fallback_in_flight {
            return None;
        }
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
    pub(super) fn begin_host_frame_recovery(
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
    pub(super) fn apply_frame_recovery(
        &mut self,
        recovery: Recovery,
    ) -> Result<Option<RecoveryFuture>, FrameError> {
        let Recovery {
            pane_id,
            generation,
            step,
        } = recovery;
        // A closed pane has nothing to recover, and after a reconnect the
        // reconcile re-attaches every pane.
        if !self.panes.contains_key(&pane_id) || self.daemon.generation() != generation {
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
                let Ok((reply, attachment)) = outcome else {
                    // No fresh attachment to adopt: the full daemon re-attach
                    // replaces the host stream as well.
                    return Ok(self.begin_daemon_recovery(pane_id));
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
        }
    }

    /// Re-register a host-restored pane with a daemon on a new generation.
    /// The attachment it kept died with the old socket, so it takes a fresh
    /// one and keeps its host stream, which rebinds on its next write.
    pub(super) fn begin_host_reregister(
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

    fn begin_recovery_attach(
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

    /// A failure with no verdict. On a live connection the pane backs off and
    /// retries; with the connection down it waits detached for the
    /// reconnect's reconcile, which a backoff earned on the dead socket would
    /// only hold back (#22747).
    pub(super) fn defer_pane_attach(&mut self, pane_id: PaneId, code: &str, error: &FrameError) {
        let pane = self.panes.get_mut(&pane_id).expect("pane exists");
        if self.daemon.ready() {
            pane.defer_attach(code, &error.to_string(), tokio::time::Instant::now());
        } else {
            pane.refuse_attach(code, &error.to_string());
        }
    }

    async fn begin_live_proxy_attach(
        &mut self,
        pane_id: PaneId,
        terminal_id: &str,
        generation: Generation,
    ) {
        let Some(request_id) = self.begin_proxy_attach(pane_id, generation) else {
            return;
        };
        let outcome = request_proxy_source(&self.daemon, terminal_id, &request_id).await;
        self.finish_proxy_attach(pane_id, generation, outcome);
    }

    /// Marks the pane attaching and returns the request id, or refuses it.
    fn begin_proxy_attach(&mut self, pane_id: PaneId, generation: Generation) -> Option<String> {
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
            return None;
        }
        let request_id = uuid::Uuid::new_v4().to_string();
        self.panes
            .get_mut(&pane_id)
            .expect("pane exists")
            .begin_attaching(request_id.clone(), Transport::Proxy, generation);
        Some(request_id)
    }

    fn finish_proxy_attach(
        &mut self,
        pane_id: PaneId,
        generation: Generation,
        outcome: Result<ProxyAttachOutcome, FrameError>,
    ) {
        match outcome {
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
            }
            Ok(ProxyAttachOutcome::Refused { code, reason }) => {
                let pane = self.panes.get_mut(&pane_id).expect("pane exists");
                if attach_refusal_is_transient(&code) {
                    pane.defer_attach(&code, &reason, tokio::time::Instant::now());
                } else {
                    pane.refuse_attach(&code, &reason);
                    self.attached_generation.insert(pane_id, generation);
                }
                self.clear_fallback_flight(pane_id);
            }
            Err(FrameError::Finalized { code, reason }) => {
                self.panes
                    .get_mut(&pane_id)
                    .expect("pane exists")
                    .refuse_attach(&code, &reason);
                self.attached_generation.insert(pane_id, generation);
                self.clear_fallback_flight(pane_id);
            }
            // No verdict: the daemon did not answer, or the reply was
            // unusable. The pane keeps its place and says so; the live loop
            // retries after the backoff instead of the whole attach pass
            // (and, at startup, the client) failing on one pane.
            Err(error) => {
                self.clear_fallback_flight(pane_id);
                self.defer_pane_attach(pane_id, "attach_failed", &error);
            }
        }
    }

    /// Start every deferred attach that is due, beside the loop as a recovery
    /// runs: the render tick that finds one due never waits on the daemon
    /// (#22747). Each takes the launch's order, the direct path first where
    /// the row offers it, then the proxy.
    pub(super) fn start_due_attaches(
        &mut self,
        now: tokio::time::Instant,
        include_initial: bool,
    ) -> Vec<RecoveryFuture> {
        let snapshot = self.daemon.subscribe().0;
        if !snapshot.ready || !self.daemon_ready {
            return Vec::new();
        }
        let generation = snapshot.generation;
        let stale = |workspace: &Self, pane_id: &PaneId| {
            let pane = &workspace.panes[pane_id];
            !pane.fallback_in_flight
                && pane.attached_generation() != Some(generation)
                && workspace.attached_generation.get(pane_id) != Some(&generation)
        };
        // A host-local restore keeps an attachment that dies with its daemon
        // generation; once the daemon is back on a new one, the pane takes a
        // fresh attachment without waiting for a retry to fall due (#23419).
        let reregister: Vec<PaneId> = self
            .order
            .iter()
            .filter(|pane_id| self.host_recovered.contains(pane_id) && stale(self, pane_id))
            .copied()
            .collect();
        let due: Vec<PaneId> = self
            .order
            .iter()
            .filter(|pane_id| {
                let pane = &self.panes[*pane_id];
                (pane.attach_retry_due(now) || (include_initial && !pane.attach_retry_pending(now)))
                    && !self.host_recovered.contains(pane_id)
                    && stale(self, pane_id)
            })
            .copied()
            .collect();
        let mut started: Vec<RecoveryFuture> = reregister
            .into_iter()
            .map(|pane_id| self.begin_host_reregister(pane_id, generation))
            .collect();
        for pane_id in due {
            let pane = self.panes.get_mut(&pane_id).expect("pane exists");
            // The retry owns its pane until it lands, as a recovery does.
            pane.fallback_in_flight = true;
            let terminal_id = pane.terminal_id.clone();
            if !(self.frame_delivery.allows(Transport::Direct) && pane.direct_available) {
                started.extend(self.begin_recovery_attach(pane_id, terminal_id, generation));
                continue;
            }
            let request_id = uuid::Uuid::new_v4().to_string();
            pane.begin_attaching(request_id.clone(), Transport::Direct, generation);
            let daemon = self.daemon.clone();
            let gobby_home = self.gobby_home.clone();
            let retry: RecoveryFuture = Box::pin(async move {
                let outcome = request_direct_source(
                    &daemon,
                    gobby_home.as_deref(),
                    &terminal_id,
                    &request_id,
                )
                .await;
                Recovery {
                    pane_id,
                    generation,
                    step: RecoveryStep::DirectAttached {
                        terminal_id,
                        outcome,
                    },
                }
            });
            started.push(retry);
        }
        started
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
            pane.backend = Backend::parse(backend);
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
        pane.host_themes = super::theme_sync::host_accepts_themes(reply);
        pane.observe_lease_holder(
            reply
                .get("lease_holder")
                .and_then(|holder| holder.get("attachment_id"))
                .and_then(Value::as_str),
        );
    }

    pub(super) fn clear_fallback_flight(&mut self, pane_id: PaneId) {
        if let Some(pane) = self.panes.get_mut(&pane_id) {
            pane.fallback_in_flight = false;
        }
    }

    /// Hands the panes of recoveries a reconnect dropped to its reconcile.
    pub(super) fn abandon_frame_recoveries(&mut self) {
        for (pane_id, pane) in self.panes.iter_mut() {
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

    pub(super) fn retire_pane_attachment(&mut self, pane_id: PaneId) {
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

async fn request_direct_source(
    daemon: &LiveDaemon,
    gobby_home: Option<&Path>,
    terminal_id: &str,
    request_id: &str,
) -> Result<DirectAttach, FrameError> {
    let (reply, attachment) = request_direct_attachment(daemon, terminal_id, request_id).await?;
    match connect_direct_reply(gobby_home, &reply).await {
        Ok((locator, source)) => Ok((reply, attachment, locator, source)),
        Err(error) => {
            daemon
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

/// Ask the daemon for a direct attachment: its reply and attachment id. The
/// caller connects the host stream the reply locates, or keeps its own.
async fn request_direct_attachment(
    daemon: &LiveDaemon,
    terminal_id: &str,
    request_id: &str,
) -> Result<(Value, String), FrameError> {
    let reply = daemon
        .send(json!({
            "type": "terminal_attach",
            "request_id": request_id,
            "terminal_id": terminal_id,
            "frame_delivery": "direct",
            "encoding": "semantic_frame",
            "viewer": "gclient",
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
    Ok((reply, attachment))
}

async fn connect_direct_reply(
    gobby_home: Option<&Path>,
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
    let source = match gobby_home {
        Some(home) => UnixSocketFrameSource::from_gobby_home(home, &locator, cols, rows).await?,
        None => UnixSocketFrameSource::from_env(&locator, cols, rows).await?,
    };
    Ok((locator, source))
}

async fn request_proxy_source(
    daemon: &LiveDaemon,
    terminal_id: &str,
    request_id: &str,
) -> Result<ProxyAttachOutcome, FrameError> {
    let (_, receiver) = daemon.subscribe();
    let reply = daemon
        .send(json!({
            "type": "terminal_attach",
            "request_id": request_id,
            "terminal_id": terminal_id,
            "frame_delivery": "proxy",
            "encoding": "semantic_frame",
            "viewer": "gclient",
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
        daemon.clone(),
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
