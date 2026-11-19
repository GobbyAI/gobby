//! Supervision of the worker handshake: what a failed handshake does to the supervisor.

use super::{BackendStatus, BackendSupervisor, SupervisorExit};
use crate::lifecycle::worker_handshake::{HandshakeOutcome, WorkerCallError, handshake_outcome};

impl BackendSupervisor {
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
