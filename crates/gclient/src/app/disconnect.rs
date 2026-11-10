//! Daemon disconnect: what a lost daemon leaves on each pane, and which
//! expired detaches to submit.

use super::{run_loop, Workspace};
use crate::daemon::{Daemon, DaemonError, Generation};

impl<D: Daemon> Workspace<D> {
    pub fn observe_daemon_disconnect(&mut self, _generation: Generation, error: DaemonError) {
        self.daemon_ready = false;
        // An op still placing a terminal answers on the old connection, which
        // the generation gate drops, so its placement would never settle.
        self.pending_placements.clear();
        for pane in self.panes.values_mut() {
            // A native pane types on its host stream, which the daemon does not
            // carry, so losing the daemon leaves its pending input alone.
            if !(pane.writable() && pane.direct_native()) {
                pane.clear_input(error.to_string());
            }
        }
        self.daemon_error = Some(error);
    }

    pub fn submit_expired_detaches(
        &mut self,
        supervisor: &mut run_loop::ReconnectSupervisor,
        now: tokio::time::Instant,
    ) -> usize {
        if self.exit_reason.is_some() {
            return 0;
        }
        let mut submitted = 0;
        for pane in self.panes.values_mut() {
            if let Some(generation) = pane.take_expired_detach_generation(now) {
                drop(supervisor.request(generation));
                submitted += 1;
            }
        }
        submitted
    }
}
