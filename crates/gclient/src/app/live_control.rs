//! The control-request machinery (plan A2.5 of gclient-daemon-resilience). A
//! focus change or a queued key only records the request it needs; the loop
//! starts it beside the select, and the reply comes back as a
//! [`ControlOutcome`], so no click or keystroke ever waits on the daemon.

use serde_json::{json, Value};
use tokio::sync::mpsc::UnboundedSender;
use tokio::sync::watch;

use crate::daemon::{DaemonError, LiveDaemon};

use super::{PaneId, Workspace};

/// A control request recorded but not yet started.
#[derive(Debug, Clone, Copy)]
pub struct PendingControl {
    pub(super) pane_id: PaneId,
    pub(super) takeover: bool,
}

/// What one control request came back with.
pub struct ControlOutcome {
    pub(super) pane_id: PaneId,
    pub(super) request: u64,
    pub(super) reply: Result<Value, DaemonError>,
}

impl Workspace<LiveDaemon> {
    /// Records the control request a focus change or a queued key needs. It is
    /// only a note: `start_control_request` turns it into the daemon round
    /// trip, beside the loop, so nothing a person does waits on it (#22573).
    /// A pane already waiting on a reply keeps that one.
    pub fn request_control(&mut self, pane_id: PaneId, takeover: bool) {
        let idle = self
            .panes
            .get(&pane_id)
            .is_some_and(|pane| pane.control_request.is_none());
        if idle {
            let takeover = takeover
                || self
                    .pending_control
                    .is_some_and(|pending| pending.pane_id == pane_id && pending.takeover);
            self.pending_control = Some(PendingControl { pane_id, takeover });
        }
    }

    /// True while this pane is waiting on a grant, whether its request is
    /// already in flight or is still the note a focus change just made. Input
    /// arriving in that window belongs to the pane, not to the floor (#22573).
    pub fn awaiting_control(&self, pane_id: PaneId) -> bool {
        self.pending_control
            .is_some_and(|pending| pending.pane_id == pane_id)
            || self
                .panes
                .get(&pane_id)
                .is_some_and(|pane| pane.is_acquiring())
    }

    /// Starts the recorded control request, if the pane can still use one. The
    /// reply comes back through `outcomes`, so requests never queue behind one
    /// another: a pane whose grant is still in flight must not delay the grant
    /// the pane someone just clicked is waiting for (#22573).
    pub fn start_control_request(&mut self, outcomes: &UnboundedSender<ControlOutcome>) {
        let Some(PendingControl { pane_id, takeover }) = self.pending_control else {
            return;
        };
        if self.exit_reason.is_some() {
            self.pending_control = None;
            return;
        }
        // A focus change can arrive while the daemon is reconnecting. Keep
        // the wish until it can be sent so the input queued behind that focus
        // is not orphaned (#22573).
        if !self.daemon_ready() {
            return;
        }
        let Some(pane) = self.panes.get(&pane_id) else {
            self.pending_control = None;
            return;
        };
        // An attachment can become live on a later daemon event. The pending
        // request belongs to that pane until then.
        if !pane.is_live() {
            return;
        }
        // A host-recovered pane stays live on its host stream while it takes
        // a fresh daemon attachment; the one it holds died with the old
        // generation, so the request waits for the new one (#23419).
        if pane.attached_generation() != Some(self.daemon.generation()) {
            return;
        }
        self.pending_control = None;
        self.next_control_seq += 1;
        let request = self.next_control_seq;
        // The take goes out after every release still behind its pane's
        // accepted writes, so the daemon sees the old lease end first.
        let releases: Vec<_> = self
            .panes
            .values()
            .filter_map(|pane| pane.pending_release())
            .collect();
        let (taken, take_done) = watch::channel(false);
        let (answered, take_answered) = watch::channel(false);
        let pane = self.panes.get_mut(&pane_id).expect("pane checked above");
        pane.control_request = Some(request);
        pane.take_done = Some(take_done);
        // The daemon answers one take per attachment at a time, so this one
        // goes out after the pane's last take was answered.
        let previous = pane.take_answered.replace(take_answered);
        let message = json!({
            "type": "terminal_take_control",
            "terminal_id": pane.terminal_id,
            "attachment_id": pane.attachment_id(),
            "takeover": takeover,
        });
        let daemon = self.daemon.clone();
        let outcomes = outcomes.clone();
        tokio::spawn(async move {
            if let Some(mut previous) = previous {
                let _ = previous.wait_for(|answered| *answered).await;
            }
            for mut release in releases {
                let _ = release.wait_for(|sent| *sent).await;
            }
            let reply = daemon.send_marking_written(message, taken).await;
            let _ = answered.send(true);
            // The loop is gone when the send fails on a closed channel; the
            // lease it was asking for is released by `shutdown` either way.
            let _ = outcomes.send(ControlOutcome {
                pane_id,
                request,
                reply,
            });
        });
    }
}
