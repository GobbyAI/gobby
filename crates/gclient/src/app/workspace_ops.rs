//! The daemon-owned workspace as this window last saw it (plan
//! gclient-workspaces 4.2). The daemon is the layout authority: the model is
//! rebuilt from the `workspace_attach` snapshot and moved only by
//! `workspace_event`s, never by a local mutation.

use std::collections::BTreeMap;

use crate::daemon::{
    PaneRow, Snapshot, TabRow, WorkspaceEvent, WorkspaceEventKind, WorkspaceRow, WorkspaceSnapshot,
};

/// The attached workspace's rows, keyed by daemon id.
#[derive(Debug)]
pub struct WorkspaceModel {
    pub workspace: WorkspaceRow,
    tabs: BTreeMap<String, TabRow>,
    panes: BTreeMap<String, PaneRow>,
    /// Events at or below this watermark are already in the model.
    pub watermark: Snapshot,
    /// Bumped by every change, so a viewer knows when to re-project.
    generation: u64,
}

impl WorkspaceModel {
    pub fn from_snapshot(snapshot: WorkspaceSnapshot) -> Self {
        Self {
            workspace: snapshot.workspace,
            tabs: snapshot
                .tabs
                .into_iter()
                .map(|tab| (tab.id.clone(), tab))
                .collect(),
            panes: snapshot
                .panes
                .into_iter()
                .map(|pane| (pane.id.clone(), pane))
                .collect(),
            watermark: snapshot.snapshot,
            generation: 1,
        }
    }

    /// Apply one event; `false` when it is foreign, stale, or from another
    /// daemon epoch, in which case the model is unchanged.
    pub fn apply(&mut self, event: &WorkspaceEvent) -> bool {
        if event.workspace_id != self.workspace.id
            || event.daemon_epoch != self.watermark.daemon_epoch
            || event.seq <= self.watermark.seq
        {
            return false;
        }
        match event.kind {
            WorkspaceEventKind::TabClosed | WorkspaceEventKind::TabRemoved => {
                for tab in &event.tabs {
                    self.tabs.remove(&tab.id);
                    self.panes.retain(|_, pane| pane.tab_id != tab.id);
                }
            }
            WorkspaceEventKind::PaneRemoved => {
                for pane in &event.panes {
                    self.panes.remove(&pane.id);
                }
                self.upsert_tabs(&event.tabs);
            }
            _ => {
                self.upsert_tabs(&event.tabs);
                for pane in &event.panes {
                    self.panes.insert(pane.id.clone(), pane.clone());
                }
            }
        }
        if let Some(workspace) = &event.workspace {
            // Only the attach reply names the node; events keep the one we have.
            let node_ref = workspace.node_ref.or(self.workspace.node_ref);
            self.workspace = workspace.clone();
            self.workspace.node_ref = node_ref;
        }
        self.watermark.seq = event.seq;
        self.generation += 1;
        true
    }

    fn upsert_tabs(&mut self, tabs: &[TabRow]) {
        for tab in tabs {
            self.tabs.insert(tab.id.clone(), tab.clone());
        }
    }

    pub fn generation(&self) -> u64 {
        self.generation
    }

    pub fn tab(&self, tab_id: &str) -> Option<&TabRow> {
        self.tabs.get(tab_id)
    }

    pub fn pane(&self, pane_id: &str) -> Option<&PaneRow> {
        self.panes.get(pane_id)
    }

    /// A project's tabs in bar order.
    pub fn tabs_for_project(&self, project_id: &str) -> Vec<&TabRow> {
        let mut tabs: Vec<&TabRow> = self
            .tabs
            .values()
            .filter(|tab| tab.project_id == project_id)
            .collect();
        tabs.sort_by_key(|tab| (tab.position, tab.reference));
        tabs
    }

    /// The `n#:w#:t#:p#` ref of a pane.
    pub fn pane_ref(&self, pane_id: &str) -> Option<String> {
        let pane = self.panes.get(pane_id)?;
        let tab = self.tabs.get(&pane.tab_id)?;
        let node = self.workspace.node_ref?;
        Some(format!(
            "n{node}:w{}:t{}:p{}",
            self.workspace.reference, tab.reference, pane.reference
        ))
    }
}
