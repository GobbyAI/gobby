//! The daemon-owned workspace as this window last saw it (plan
//! gclient-workspaces 4.2). The daemon is the layout authority: the model is
//! rebuilt from the `workspace_attach` snapshot and moved only by
//! `workspace_event`s, never by a local mutation.

use std::collections::BTreeMap;

use super::Workspace;
use crate::daemon::{
    Daemon, PaneRow, Snapshot, TabRow, WorkspaceEvent, WorkspaceEventKind, WorkspaceRow,
    WorkspaceSnapshot,
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

    /// Continue `previous`'s generation count, so a viewer that projected
    /// the old model sees this one as newer.
    pub fn succeed(&mut self, previous: &WorkspaceModel) {
        self.generation = previous.generation + 1;
    }

    /// Every pane row.
    pub fn panes(&self) -> impl Iterator<Item = &PaneRow> {
        self.panes.values()
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

    /// The tab's address, `node:workspace:tab`, zero-based and letterless:
    /// position carries the meaning, as it does for a pane ref.
    pub fn tab_ref(&self, tab_id: &str) -> Option<String> {
        let tab = self.tabs.get(tab_id)?;
        let node = self.workspace.node_ref?;
        Some(format!(
            "{node}:{}:{}",
            self.workspace.reference, tab.reference
        ))
    }

    /// The pane's address, `node:workspace:tab:pane`.
    pub fn pane_ref(&self, pane_id: &str) -> Option<String> {
        let pane = self.panes.get(pane_id)?;
        Some(format!(
            "{}:{}",
            self.tab_ref(&pane.tab_id)?,
            pane.reference
        ))
    }

    /// The address of the pane hosting a terminal, when the model places it.
    pub fn pane_ref_for_terminal(&self, terminal_id: &str) -> Option<String> {
        let pane = self
            .panes
            .values()
            .find(|pane| pane.terminal_id.as_deref() == Some(terminal_id))?;
        self.pane_ref(&pane.id)
    }
}

impl<D: Daemon> Workspace<D> {
    /// The attached daemon workspace, once its snapshot arrived.
    pub fn workspace_model(&self) -> Option<&WorkspaceModel> {
        self.workspace_model.as_ref()
    }

    /// Replace the workspace model with a fresh `workspace_attach` snapshot.
    pub fn apply_workspace_snapshot(&mut self, snapshot: WorkspaceSnapshot) {
        let mut model = WorkspaceModel::from_snapshot(snapshot);
        if let Some(previous) = &self.workspace_model {
            model.succeed(previous);
        }
        self.workspace_model = Some(model);
    }

    /// Apply a `workspace_event`; `true` when the model changed.
    pub fn apply_workspace_event(&mut self, event: &WorkspaceEvent) -> bool {
        self.workspace_model
            .as_mut()
            .is_some_and(|model| model.apply(event))
    }
}
