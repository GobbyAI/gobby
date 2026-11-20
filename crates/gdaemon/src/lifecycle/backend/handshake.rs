//! Supervision of the worker handshake: how a child's handshake ends while the child is supervised,
//! and what a failed handshake does to the supervisor.

use tokio::{sync::watch, time::Interval};

use super::{BackendStatus, BackendSupervisor, SupervisorExit, shutdown_signal};
use crate::lifecycle::{
    worker_channel::WorkerAttachment,
    worker_handshake::{HandshakeOutcome, WorkerCallError, handshake_outcome},
};

/// How a child's handshake ended.
pub(super) enum HandshakeStep {
    /// The worker answered, and its session is bound.
    Ready,
    /// The handshake failed. Its classification decides what the supervisor does next.
    Failed(WorkerCallError),
    /// A shutdown was requested before the handshake ended.
    Shutdown,
    /// The lease was lost before the handshake ended.
    LeaseLost,
}

impl BackendSupervisor {
    /// Runs the handshake over the child's attachment. While it runs, the supervisor keeps
    /// heartbeating and watches for shutdown, so a stalled worker cannot hold either.
    pub(super) async fn await_handshake(
        &self,
        attachment: &mut WorkerAttachment,
        shutdown: &mut watch::Receiver<bool>,
        heartbeats: &mut Interval,
    ) -> HandshakeStep {
        let handshake = self.handshake_worker(
            &mut attachment.link,
            &mut attachment.session,
            &attachment.credential,
        );
        tokio::pin!(handshake);
        loop {
            tokio::select! { biased;
                outcome = &mut handshake => {
                    return match outcome {
                        Ok(_) => HandshakeStep::Ready,
                        Err(error) => HandshakeStep::Failed(error),
                    };
                }
                _ = shutdown_signal(shutdown) => return HandshakeStep::Shutdown,
                _ = heartbeats.tick() => {
                    if let Err(error) = self.heartbeat().await {
                        tracing::warn!(target: "gobby.lifecycle", "backend lease lost: {error:#}");
                        return HandshakeStep::LeaseLost;
                    }
                }
            }
        }
    }

    /// Applies a failed handshake. A sticky failure refuses the supervisor with a fixed reason
    /// and returns the exit; a crash returns None, and the supervisor respawns the child.
    pub(super) fn handle_handshake_failure(
        &self,
        error: &WorkerCallError,
    ) -> Option<SupervisorExit> {
        if handshake_outcome(error) == HandshakeOutcome::Crash {
            return None;
        }
        self.state.send_replace(BackendStatus::Refused {
            reason: format!("worker handshake failed: {}", sticky_class(error)),
        });
        Some(SupervisorExit::Refused)
    }
}

/// The fixed name of a failure's class. The reason never carries the worker's own text.
fn sticky_class(error: &WorkerCallError) -> &'static str {
    match error {
        WorkerCallError::CredentialMissing => "credential_missing",
        WorkerCallError::RequestTooLarge { .. } => "request_too_large",
        WorkerCallError::Unknown(_) => "unknown_outcome",
        WorkerCallError::Protocol(_) => "protocol",
        WorkerCallError::Refused(_) => "refused",
        WorkerCallError::OpFailed(_) => "op_failed",
    }
}
