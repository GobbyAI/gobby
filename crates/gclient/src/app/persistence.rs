//! Workspace snapshot projection: the per-project snapshot is written from a
//! tab set and rebuilt into one on restore.

use super::project_tabs::TabSet;
use super::viewer_state::ViewerState;
use super::Workspace;
use crate::daemon::Daemon;
use crate::frame_source::FrameError;
use crate::persist::{load_snapshot, save_snapshot, SidebarSnapshot, WorkspaceSnapshot};
use crate::ui::chrome::SidebarState;
use std::io;
use std::path::{Path, PathBuf};

impl Workspace {
    /// Select the project and rebuild its tab set from the snapshot, opening
    /// a pane for each saved terminal the daemon still has. Vanished
    /// terminals drop out and the snapshot is rewritten without them.
    pub fn restore_project(
        &mut self,
        project_id: &str,
        viewer: &mut ViewerState,
    ) -> Result<TabSet, FrameError> {
        self.ensure_requests_allowed()
            .map_err(|error| FrameError::Other(error.to_string()))?;
        self.select_project(project_id);
        let home = self.gobby_home.clone().unwrap_or_default();
        let snapshot = load_saved(&home, project_id)?;
        let live = self.daemon.live_terminals();
        for terminal_id in snapshot.terminal_ids() {
            if live.contains(&terminal_id) && self.pane_for_terminal(&terminal_id).is_none() {
                self.open_terminal(&terminal_id, "native", "epoch-a")?;
            }
        }
        let tabs = TabSet::from_snapshot(
            &snapshot,
            |terminal_id| self.pane_for_terminal(terminal_id),
            viewer,
            project_id,
        );
        self.focus = snapshot
            .focused_terminal_id
            .as_deref()
            .and_then(|terminal_id| self.pane_for_terminal(terminal_id))
            .or_else(|| {
                tabs.tabs
                    .get(viewer.active_index(project_id, &tabs.tabs))
                    .and_then(|tab| viewer.focused_pane(tab))
            });
        self.roster_ids = live;
        self.persist_workspace(&tabs, viewer)?;
        Ok(tabs)
    }
}

/// The part of the sidebar state `session.json` keeps.
pub fn sidebar_snapshot(sidebar: &SidebarState) -> SidebarSnapshot {
    SidebarSnapshot {
        collapsed: sidebar.collapsed,
        width: sidebar.width,
        all_projects: sidebar.all_projects,
        all_sessions: sidebar.all_sessions,
        machine_filter: sidebar.machine_filter.clone(),
        project_order: sidebar.project_order.clone(),
        project_labels: sidebar.project_labels.clone(),
    }
}

/// Restore the saved sidebar state; the width is clamped to the sidebar's
/// range.
pub fn apply_sidebar_snapshot(sidebar: &mut SidebarState, saved: &SidebarSnapshot) {
    sidebar.collapsed = saved.collapsed;
    sidebar.width = saved.width.clamp(sidebar.min_width, sidebar.max_width);
    sidebar.all_projects = saved.all_projects;
    sidebar.all_sessions = saved.all_sessions;
    sidebar.machine_filter = saved.machine_filter.clone();
    sidebar.project_order = saved.project_order.clone();
    sidebar.project_labels = saved.project_labels.clone();
}

/// The saved snapshot, or an empty one when none was written yet.
pub(super) fn load_saved(home: &Path, project_id: &str) -> io::Result<WorkspaceSnapshot> {
    match load_snapshot(home, project_id) {
        Ok(snapshot) => Ok(snapshot),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            Ok(WorkspaceSnapshot::empty(project_id))
        }
        Err(error) => Err(error),
    }
}

impl<D: Daemon> Workspace<D> {
    pub fn set_gobby_home(&mut self, home: PathBuf) {
        self.gobby_home = Some(home);
    }

    pub fn gobby_home(&self) -> Option<&Path> {
        self.gobby_home.as_deref()
    }

    pub fn set_launch_dir(&mut self, dir: PathBuf) {
        self.launch_dir = Some(dir);
    }

    /// The snapshot `restore_project` read, which the live loop rebuilds the
    /// tab bar from; none when no Gobby home was set.
    pub fn saved_snapshot(&self) -> Option<&WorkspaceSnapshot> {
        self.saved_snapshot.as_ref()
    }

    /// Where a new shell starts: the focused project's checkout root, else
    /// the directory gclient was launched from (the personal project has
    /// no checkout, and a project checked out elsewhere has none here).
    pub fn focused_checkout_path(&self) -> Option<String> {
        let project = self.project_id.as_deref()?;
        self.sidebar_rows
            .projects
            .iter()
            .find(|row| row.id == project)
            .and_then(|row| row.checkout.as_ref())
            .map(|checkout| checkout.root_path.clone())
            .or_else(|| {
                self.launch_dir
                    .as_ref()
                    .map(|dir| dir.display().to_string())
            })
    }

    pub fn tab_order(&self) -> Vec<String> {
        self.order
            .iter()
            .filter_map(|id| self.panes.get(id))
            .map(|pane| pane.terminal_id.clone())
            .collect()
    }

    pub fn focused_terminal_id(&self) -> Option<&str> {
        self.focus
            .and_then(|id| self.panes.get(&id))
            .map(|pane| pane.terminal_id.as_str())
    }

    pub fn set_tab_order<S: AsRef<str>>(&mut self, terminal_ids: &[S]) -> io::Result<()> {
        let mut order = Vec::with_capacity(terminal_ids.len());
        for terminal_id in terminal_ids {
            let terminal_id = terminal_id.as_ref();
            let pane_id = self.pane_for_terminal(terminal_id).ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("unknown terminal in tab order: {terminal_id}"),
                )
            })?;
            if order.contains(&pane_id) {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("duplicate terminal in tab order: {terminal_id}"),
                ));
            }
            order.push(pane_id);
        }
        if order.len() != self.order.len() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "tab order must contain every workspace pane",
            ));
        }
        self.order = order;
        // The roster follows the pane order; ids without a pane keep their
        // place after it.
        let ordered = self.tab_order();
        self.saved_tab_order = ordered.clone();
        self.roster_ids.retain(|id| !ordered.contains(id));
        self.roster_ids.splice(0..0, ordered);
        Ok(())
    }

    /// The focused project's snapshot of `tabs`, once a Gobby home and a
    /// project are set.
    pub fn workspace_snapshot(
        &self,
        tabs: &TabSet,
        viewer: &ViewerState,
    ) -> Option<WorkspaceSnapshot> {
        let project_id = self.project_id.as_deref()?;
        self.gobby_home.as_ref()?;
        Some(tabs.snapshot(
            project_id,
            |pane| self.panes.get(&pane).map(|pane| pane.terminal_id.clone()),
            viewer,
        ))
    }

    pub fn persist_workspace(
        &self,
        tabs: &TabSet,
        viewer: &ViewerState,
    ) -> io::Result<Option<PathBuf>> {
        let (Some(home), Some(snapshot)) =
            (&self.gobby_home, self.workspace_snapshot(tabs, viewer))
        else {
            return Ok(None);
        };
        save_snapshot(home, &snapshot).map(Some)
    }
}
