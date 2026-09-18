//! Per-window state that never reaches a file: the Gobby home, the launch
//! directory, and the pane order the window set. Layout lives in the
//! daemon's workspace rows and user preferences in `prefs.toml`.

use super::Workspace;
use crate::daemon::Daemon;
use std::io;
use std::path::{Path, PathBuf};

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

    /// The window runs inside a gclient pane (`GOBBY_PANE_ID` was set).
    pub fn set_in_pane(&mut self, in_pane: bool) {
        self.in_pane = in_pane;
    }

    pub fn in_pane(&self) -> bool {
        self.in_pane
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
        self.pane_order = ordered.clone();
        self.roster_ids.retain(|id| !ordered.contains(id));
        self.roster_ids.splice(0..0, ordered);
        Ok(())
    }
}
