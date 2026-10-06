//! Daemon disconnect and control retirement: what a lost daemon or an
//! indeterminate control result leaves on each pane.

use super::{run_loop, PaneId, Workspace};
use crate::daemon::{Daemon, DaemonError, Generation};

impl<D: Daemon> Workspace<D> {
    pub fn retire_indeterminate_control(
        &mut self,
        pane_id: PaneId,
        now: tokio::time::Instant,
    ) -> Option<(String, String, Generation)> {
        // A pane reconnecting or restored straight onto its host does not
        // depend on the daemon control plane: a control request that failed
        // while the daemon was away must not tear down the host stream, the
        // reconnect still in flight, or the grant it carries (#23076).
        if self.host_recovering.contains(&pane_id) || self.host_recovered.contains(&pane_id) {
            return None;
        }
        let outcome = {
            let pane = self.panes.get_mut(&pane_id)?;
            let terminal_id = pane.terminal_id.clone();
            let (attachment_id, generation) = pane.begin_detaching(now)?;
            pane.clear_control("Control result indeterminate.");
            pane.reattach_after_indeterminate = true;
            (terminal_id, attachment_id, generation)
        };
        // Same removal as proxy recovery. An unchanged socket generation
        // would otherwise make the next attach pass skip this pane.
        self.attached_generation.remove(&pane_id);
        Some(outcome)
    }

    pub fn observe_daemon_disconnect(&mut self, _generation: Generation, error: DaemonError) {
        self.daemon_ready = false;
        // An op still placing a terminal answers on the old connection, which
        // the generation gate drops, so its placement would never settle.
        self.pending_placements.clear();
        for pane in self.panes.values_mut() {
            // The host owns an existing direct input grant. Losing the daemon
            // does not revoke it; a host refusal still clears it on that stream.
            if !(pane.writable() && pane.direct_input()) {
                pane.clear_control(error.to_string());
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
