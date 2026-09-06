//! Workspace snapshot projection and mutation persistence.

use super::Workspace;
use crate::daemon::Daemon;
use crate::frame_source::FrameError;
use crate::persist::{load_snapshot, save_snapshot, LayoutNode, SplitAxis, WorkspaceSnapshot};
use std::path::PathBuf;

impl Workspace {
    pub fn restore_project(&mut self, project_id: &str) -> Result<(), FrameError> {
        self.ensure_requests_allowed()
            .map_err(|error| FrameError::Other(error.to_string()))?;
        self.select_project(project_id);
        let home = self.gobby_home.clone().unwrap_or_default();
        let snapshot = load_snapshot(&home, project_id)
            .map_err(|error| FrameError::Other(error.to_string()))?;
        let live = self.daemon.live_terminals();
        let mut terminal_ids = snapshot.tab_order.clone();
        for terminal_id in snapshot.terminal_ids() {
            if !terminal_ids.contains(&terminal_id) {
                terminal_ids.push(terminal_id);
            }
        }
        for terminal_id in terminal_ids {
            if live.contains(&terminal_id) {
                self.open_terminal_unpersisted(&terminal_id, "native", "epoch-a")?;
            }
        }
        self.focus = snapshot
            .focused_terminal_id
            .as_deref()
            .and_then(|terminal_id| self.pane_for_terminal(terminal_id));
        self.roster_ids = live;
        self.persist_workspace()
            .map_err(|error| FrameError::Other(error.to_string()))?;
        Ok(())
    }
}

impl<D: Daemon> Workspace<D> {
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

    pub fn set_tab_order<S: AsRef<str>>(&mut self, terminal_ids: &[S]) -> std::io::Result<()> {
        let mut order = Vec::with_capacity(terminal_ids.len());
        for terminal_id in terminal_ids {
            let terminal_id = terminal_id.as_ref();
            let pane_id = self.pane_for_terminal(terminal_id).ok_or_else(|| {
                std::io::Error::new(
                    std::io::ErrorKind::InvalidInput,
                    format!("unknown terminal in tab order: {terminal_id}"),
                )
            })?;
            if order.contains(&pane_id) {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidInput,
                    format!("duplicate terminal in tab order: {terminal_id}"),
                ));
            }
            order.push(pane_id);
        }
        if order.len() != self.order.len() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "tab order must contain every workspace pane",
            ));
        }
        self.order = order;
        self.persist_workspace().map(|_| ())
    }

    pub fn persist_workspace(&self) -> std::io::Result<Option<PathBuf>> {
        let (Some(home), Some(project_id)) = (&self.gobby_home, &self.project_id) else {
            return Ok(None);
        };
        let children = self
            .tab_order()
            .into_iter()
            .map(|terminal_id| LayoutNode::Pane { terminal_id })
            .collect::<Vec<_>>();
        let layout = match children.len() {
            0 => LayoutNode::Empty,
            1 => children.into_iter().next().expect("one layout child"),
            _ => LayoutNode::Split {
                axis: SplitAxis::Horizontal,
                children,
            },
        };
        let snapshot = WorkspaceSnapshot {
            project_id: project_id.clone(),
            layout,
            tab_order: self.tab_order(),
            focused_terminal_id: self.focused_terminal_id().map(str::to_string),
        };
        save_snapshot(home, &snapshot).map(Some)
    }
}
