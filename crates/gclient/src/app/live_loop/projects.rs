//! Project focus for the live loop: the outgoing project's tab set is saved
//! to its snapshot, the incoming one is restored from its own or seeded with
//! a shell (decision 7), and the client session follows the sidebar.

use crate::daemon::LiveDaemon;
use crate::frame_source::FrameError;
use crate::persist::{save_session, save_snapshot, ClientSession, WorkspaceSnapshot};
use crate::ui::Chrome;

use super::super::persistence::sidebar_snapshot;
use super::super::project_tabs::TabSet;
use super::super::Workspace;
use super::actions::{spawn_live_terminal, sync_live_chrome};
use super::mouse::Placement;

/// Make `project_id` the focused project: its roster replaces the current
/// one and its tab set the tab bar. The outgoing set is saved first, so the
/// panes the roster drops are already in that project's snapshot.
pub async fn focus_project(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    project_id: &str,
) -> Result<(), FrameError> {
    if workspace.project_id() == Some(project_id) {
        return Ok(());
    }
    if let Some(current) = workspace.project_id() {
        chrome.project_tabs.focus(current);
    }
    workspace.persist_workspace(chrome.tabs())?;
    workspace.restore_project(project_id)?;
    workspace.fetch_roster().await?;
    workspace.attach_ready_panes().await?;
    workspace.fetch_sidebar_rows().await?;
    chrome.project_tabs.focus(project_id);
    sync_live_chrome(workspace, chrome);
    save_client_session(workspace, chrome)?;
    restore_focused(workspace, chrome).await
}

/// Fill the focused project's tab bar when it is empty: from its snapshot
/// when one was read, else with one shell. A workspace whose snapshot store
/// was never consulted (no Gobby home) is left alone.
pub(super) async fn restore_focused(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
) -> Result<(), FrameError> {
    if let Some(project) = workspace.project_id() {
        chrome.project_tabs.focus(project);
    }
    if !chrome.tabs().tabs.is_empty() {
        return Ok(());
    }
    let Some(snapshot) = workspace.saved_snapshot() else {
        return Ok(());
    };
    *chrome.tabs_mut() = TabSet::from_snapshot(snapshot, |terminal_id| {
        workspace.pane_for_terminal(terminal_id)
    });
    if chrome.tabs().tabs.is_empty() {
        spawn_live_terminal(workspace, chrome, Placement::Tab).await?;
    }
    Ok(())
}

/// Write the focused project's snapshot when it differs from the last one
/// written; `last` carries that between calls.
pub(super) fn persist_if_changed(
    workspace: &Workspace<LiveDaemon>,
    chrome: &Chrome,
    last: &mut Option<WorkspaceSnapshot>,
) -> std::io::Result<()> {
    let Some(snapshot) = workspace.workspace_snapshot(chrome.tabs()) else {
        return Ok(());
    };
    if last.as_ref() == Some(&snapshot) {
        return Ok(());
    }
    if let Some(home) = workspace.gobby_home() {
        save_snapshot(home, &snapshot)?;
    }
    *last = Some(snapshot);
    Ok(())
}

/// Write `session.json`: the focused project and the sidebar state.
pub(super) fn save_client_session(
    workspace: &Workspace<LiveDaemon>,
    chrome: &Chrome,
) -> std::io::Result<()> {
    let Some(home) = workspace.gobby_home() else {
        return Ok(());
    };
    let session = ClientSession {
        focused_project: chrome.project_tabs.focused.clone(),
        sidebar: sidebar_snapshot(&chrome.sidebar),
    };
    save_session(home, &session).map(|_| ())
}
