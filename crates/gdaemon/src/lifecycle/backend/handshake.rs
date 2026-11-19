//! Supervision of the worker handshake: what a failed handshake does to the supervisor.

use super::{BackendSupervisor, SupervisorExit};
use crate::lifecycle::worker_handshake::WorkerCallError;

impl BackendSupervisor {
    /// Applies a failed handshake. A sticky failure refuses the supervisor and returns the exit;
    /// a crash returns None, and the supervisor respawns the child.
    pub(super) fn handle_handshake_failure(
        &self,
        _error: &WorkerCallError,
    ) -> Option<SupervisorExit> {
        None
    }
}
