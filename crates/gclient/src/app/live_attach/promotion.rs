//! Proxy-to-direct promotion (#23714). A pane on the daemon's proxy whose
//! terminal still offers a direct locator tries a direct attach again on a
//! backoff. The try is make-before-break: the proxy keeps delivering frames
//! until the direct source is installed, so a failed try changes nothing.

use super::*;
use crate::app::attach::ATTACH_RETRY_MAX;

impl Workspace<LiveDaemon> {
    /// Starts a direct try for each proxy pane whose promotion fell due. Only
    /// auto delivery promotes; a pane's first eligible tick arms its backoff.
    pub(in crate::app) fn start_due_promotions(&mut self, now: Instant) -> Vec<RecoveryFuture> {
        let snapshot = self.daemon.subscribe().0;
        if !snapshot.ready
            || !self.daemon_ready
            || !self.frame_delivery.allows(Transport::Direct)
            || !self.frame_delivery.allows(Transport::Proxy)
        {
            return Vec::new();
        }
        let generation = snapshot.generation;
        let mut started: Vec<RecoveryFuture> = Vec::new();
        for pane_id in &self.order {
            let Some(pane) = self.panes.get_mut(pane_id) else {
                continue;
            };
            let on_proxy = matches!(
                pane.attach,
                AttachState::Attached {
                    transport: Transport::Proxy,
                    generation: attached,
                    ..
                } if attached == generation
            );
            if !on_proxy || !pane.direct_available || pane.fallback_in_flight || pane.promoting {
                continue;
            }
            if *pane.promote_at.get_or_insert(now + pane.promote_delay) > now {
                continue;
            }
            pane.promoting = true;
            let pane_id = *pane_id;
            let terminal_id = pane.terminal_id.clone();
            let proxy_attachment = pane.attachment_id().to_string();
            let request_id = uuid::Uuid::new_v4().to_string();
            let daemon = self.daemon.clone();
            let gobby_home = self.gobby_home.clone();
            started.push(Box::pin(async move {
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
                    step: RecoveryStep::Promoted {
                        terminal_id,
                        proxy_attachment,
                        outcome,
                    },
                }
            }));
        }
        started
    }

    /// Settles a direct try. A failure keeps the proxy and backs off. A won
    /// attachment replaces the proxy only while the pane is still on the
    /// proxy attachment the try began from; otherwise it is released.
    pub(super) fn apply_promotion(
        &mut self,
        pane_id: PaneId,
        generation: Generation,
        terminal_id: &str,
        proxy_attachment: &str,
        outcome: Result<DirectAttach, FrameError>,
    ) {
        let pane = self.panes.get_mut(&pane_id);
        let (reply, attachment, locator, source) = match outcome {
            Ok(won) => won,
            Err(_) => {
                if let Some(pane) = pane {
                    pane.promoting = false;
                    pane.promote_at = Some(Instant::now() + pane.promote_delay);
                    pane.promote_delay = (pane.promote_delay * 2).min(ATTACH_RETRY_MAX);
                }
                return;
            }
        };
        let current = self.daemon.generation() == generation;
        let Some(pane) = pane.filter(|pane| {
            current
                && pane.attachment_id() == proxy_attachment
                && pane.transport() == Some(Transport::Proxy)
        }) else {
            if let Some(pane) = self.panes.get_mut(&pane_id) {
                pane.promoting = false;
            }
            // A stale generation's attachments died with its connection.
            if current {
                self.release_attachment(terminal_id, &attachment);
            }
            return;
        };
        pane.promoting = false;
        // Installing resets control to observe, so a focused pane that held or
        // was taking the lease asks again under the new attachment.
        let held = pane.is_held();
        let reask = self.focus == Some(pane_id) && (held || pane.is_acquiring());
        // A take still in flight answers for the proxy attachment (#22747).
        pane.control_request = None;
        // The proxy's finalization must not retire the promoted pane.
        pane.tombstones.insert(proxy_attachment.to_string());
        self.install_direct_source(pane_id, &reply, attachment.clone(), &locator, source);
        self.attached_generation.insert(pane_id, generation);
        tracing::info!(
            lifecycle_stage = "direct-promotion",
            pane = pane_id.0,
            terminal_id = %terminal_id,
            attachment = %attachment,
            replaced = %proxy_attachment,
            reason = "direct locator usable; host accepted the attach",
            "pane promoted to direct frame delivery"
        );
        self.release_attachment(terminal_id, proxy_attachment);
        if reask {
            // A held lease names this client's proxy attachment until its
            // release lands, so only a takeover moves it. An unanswered take
            // asks again without one: a peer holder is never displaced.
            self.request_control(pane_id, held);
        }
    }

    /// Detaches `attachment` beside the loop.
    fn release_attachment(&self, terminal_id: &str, attachment: &str) {
        let daemon = self.daemon.clone();
        let message = json!({
            "type": "terminal_detach",
            "request_id": uuid::Uuid::new_v4().to_string(),
            "terminal_id": terminal_id,
            "attachment_id": attachment,
        });
        let attachment = attachment.to_string();
        tokio::spawn(async move {
            if let Err(error) = daemon.notify(message).await {
                tracing::warn!(
                    attachment = %attachment,
                    error = %error,
                    "releasing a replaced attachment failed"
                );
            }
        });
    }
}

/// Records a pane leaving direct delivery: a failed direct attach, or a
/// direct stream recovering through the daemon.
pub(super) fn log_direct_fallback(pane: &Pane, reason: &str) {
    if pane.transport() == Some(Transport::Direct) {
        tracing::info!(
            lifecycle_stage = "direct-fallback",
            pane = pane.id.0,
            terminal_id = %pane.terminal_id,
            reason = %reason,
            "pane left direct frame delivery"
        );
    }
}
