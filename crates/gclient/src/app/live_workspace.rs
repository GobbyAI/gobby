//! The live client's side of the daemon-owned workspace (plan
//! gclient-workspaces 4.2): attach on every (re)connect, follow the panes
//! the daemon's events name, and open their terminals on demand.

use super::*;
use crate::daemon::WorkspaceEvent;

impl Workspace<LiveDaemon> {
    /// Attach the window's workspace (`attach_target`; absent fields leave
    /// the daemon to pick the local node's `default`) and take its snapshot
    /// as the model. Every reconnect re-attaches: the reader forgets the
    /// attachment with the socket, and the daemon may have restarted.
    pub async fn attach_live_workspace(&mut self) -> Result<(), DaemonError> {
        let project_id = self
            .attach
            .workspace
            .is_none()
            .then_some(self.attach.project_id.as_deref())
            .flatten();
        let snapshot = self
            .daemon
            .attach_workspace(
                self.attach.node.as_deref(),
                self.attach.workspace.as_deref(),
                project_id,
            )
            .await?;
        self.apply_workspace_snapshot(snapshot);
        Ok(())
    }

    /// Point the next attach at `target`; startup sets it from the flags.
    pub fn set_attach_target(&mut self, target: AttachTarget) {
        self.attach = target;
    }

    pub fn attach_target(&self) -> &AttachTarget {
        &self.attach
    }

    /// Apply a `workspace_event` and note where a pending placement landed;
    /// `true` when the model changed.
    pub(super) fn apply_live_workspace_event(&mut self, event: &WorkspaceEvent) -> bool {
        if !self.apply_workspace_event(event) {
            return false;
        }
        for pane in &event.panes {
            let Some(terminal_id) = pane.terminal_id.as_deref() else {
                continue;
            };
            if self.pending_placements.remove(terminal_id) {
                self.placed_panes
                    .push((pane.tab_id.clone(), pane.id.clone()));
            }
        }
        true
    }

    /// Remember that `terminal_id`'s placement op is in flight, so the
    /// event that places it moves this window's focus there.
    pub(super) fn expect_placement(&mut self, terminal_id: &str) {
        self.pending_placements.insert(terminal_id.to_string());
    }

    /// Forget a placement the daemon refused, so the event that later
    /// names the terminal is another window's placement, not this one's.
    pub(super) fn forget_placement(&mut self, terminal_id: &str) {
        self.pending_placements.remove(terminal_id);
    }

    /// `(tab, pane)` of every placement that landed since the last call.
    pub(super) fn take_placed_panes(&mut self) -> Vec<(String, String)> {
        std::mem::take(&mut self.placed_panes)
    }

    /// Hold `placements` for a later projection: they landed in tabs the
    /// shown bar does not carry.
    pub(super) fn hold_placed_panes(&mut self, placements: Vec<(String, String)>) {
        self.placed_panes = placements;
    }

    /// Open the terminal of every pane in the focused project's tabs that
    /// the roster has not delivered. A row the daemon does not serve yet
    /// stays unresolved: its slot renders empty until a later event or
    /// reconcile retries.
    pub(super) async fn open_unresolved_terminals(&mut self) {
        let Some(project) = self.project_id.as_deref() else {
            return;
        };
        let Some(model) = &self.workspace_model else {
            return;
        };
        let missing: Vec<String> = model
            .panes()
            .filter(|pane| {
                model
                    .tab(&pane.tab_id)
                    .is_some_and(|tab| tab.project_id == project)
            })
            .filter_map(|pane| pane.terminal_id.clone())
            .filter(|terminal_id| self.pane_for_terminal(terminal_id).is_none())
            .collect();
        for terminal_id in missing {
            // Best effort: an unavailable row leaves the slot empty.
            let _ = self.open_live_terminal(&terminal_id).await;
        }
    }
}
