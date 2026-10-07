//! Layout mutations as daemon workspace ops (plan gclient-workspaces 4.2).
//! A daemon tab never changes locally: each mutation names explicit daemon
//! ids, and the chrome moves when the daemon's `workspace_event` is
//! projected. A local tab (opened without the daemon) keeps the chrome-only
//! paths, which the callers fall back to when these return `false`.

use gobby_terminal::layout;

use crate::daemon::{Daemon, DaemonError, LayoutAxis, LiveDaemon, WorkspaceOp};
use crate::frame_source::FrameError;
use crate::ui::chrome::Tab;
use crate::ui::status::Toast;
use crate::ui::Chrome;

use super::super::viewer_state::EMPTY_LOCAL_TAB_PREFIX;
use super::super::Workspace;
use super::local_adoption::spawn_in_adopted_local_tab;
use super::mouse::Placement;

/// The active tab when it is the daemon's.
pub(super) fn active_daemon_tab(chrome: &Chrome) -> Option<&Tab> {
    chrome.active_tab().filter(|tab| !tab.is_local())
}

/// The daemon pane id behind `slot`.
pub(super) fn daemon_pane_id(chrome: &Chrome, slot: layout::PaneId) -> Option<String> {
    chrome.viewer.panes.daemon_id(slot).map(str::to_owned)
}

/// The op that places `terminal_id` per `placement` when the window views a
/// daemon workspace: a new tab of the focused project, or a split of the
/// active daemon tab's focused pane (a bar with no tab yet gets a tab).
/// `None` keeps the chrome-only placement: no model, no project, or a
/// local tab active.
pub(super) fn placement_op(
    workspace: &Workspace<LiveDaemon>,
    chrome: &Chrome,
    placement: Placement,
    terminal_id: Option<&str>,
    worktree_id: Option<String>,
    cwd: Option<String>,
    project_override: Option<&str>,
) -> Option<WorkspaceOp> {
    let model = workspace.workspace_model()?;
    let project_id = project_override
        .or_else(|| workspace.project_id())?
        .to_owned();
    if terminal_id.is_some()
        && project_override.is_none()
        && chrome.active_tab().is_some_and(Tab::is_local)
    {
        return None;
    }
    let focused =
        active_daemon_tab(chrome).and_then(|tab| daemon_pane_id(chrome, chrome.tab_focus(tab)));
    let terminal_id = terminal_id.map(str::to_owned);
    let split = |pane, axis| WorkspaceOp::PaneSplit {
        pane,
        axis,
        terminal_id: terminal_id.clone(),
        cwd: cwd.clone(),
        node: None,
    };
    Some(match (placement, focused) {
        (Placement::Tab, _) | (_, None) => WorkspaceOp::TabCreate {
            workspace: model.workspace.id.clone(),
            project_id,
            worktree_id,
            title: None,
            terminal_id,
            cwd,
            node: None,
        },
        (Placement::SplitRight, Some(pane)) => split(pane, LayoutAxis::Horizontal),
        (Placement::SplitDown, Some(pane)) => split(pane, LayoutAxis::Vertical),
    })
}

/// Create a shell as part of its daemon pane, then attach the returned terminal.
pub(super) async fn spawn_owned_live_shell(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    placement: Placement,
    worktree_id: Option<String>,
    cwd: Option<String>,
) -> Result<bool, FrameError> {
    if !matches!(placement, Placement::Tab)
        && chrome
            .active_tab()
            .is_some_and(|tab| tab.is_local() && !tab.slots.is_empty())
    {
        return spawn_in_adopted_local_tab(workspace, chrome, placement, cwd).await;
    }
    let replacing_draft = chrome
        .active_tab()
        .filter(|tab| tab.id.starts_with(EMPTY_LOCAL_TAB_PREFIX) && tab.slots.is_empty())
        .map(|tab| tab.id.clone());
    let next_tab_position = if replacing_draft.is_some() {
        chrome
            .tabs()
            .tabs
            .iter()
            .skip(chrome.active_index() + 1)
            .find(|tab| !tab.is_local())
            .and_then(|tab| workspace.workspace_model()?.tab(&tab.id))
            .map(|row| row.position)
    } else {
        None
    };
    let Some(op) = placement_op(workspace, chrome, placement, None, worktree_id, cwd, None) else {
        return Ok(false);
    };
    let reply = match workspace.daemon().workspace_op(op).await {
        Ok(reply) => reply,
        Err(DaemonError::Workspace(error)) => {
            chrome.notify(Toast::warning(error.reason));
            return Ok(true);
        }
        Err(error) => return Err(error.into()),
    };
    let terminal_id = reply.result["panes"][0]["terminal_id"]
        .as_str()
        .ok_or_else(|| FrameError::Protocol("workspace shell op omitted terminal_id".into()))?
        .to_owned();
    let created_tab = if replacing_draft.is_some() {
        Some(
            reply.result["tabs"][0]["id"]
                .as_str()
                .ok_or_else(|| FrameError::Protocol("tab.create omitted the new tab id".into()))?
                .to_owned(),
        )
    } else {
        None
    };
    let replaced_draft = replacing_draft.is_some();
    if let (Some(draft_id), Some(created_tab)) = (replacing_draft, created_tab.as_deref()) {
        let tab = chrome
            .active_tab_mut()
            .ok_or_else(|| FrameError::Protocol("empty draft disappeared during spawn".into()))?;
        tab.id = created_tab.to_owned();
        chrome.viewer.focus.remove(&draft_id);
        chrome
            .viewer
            .active_tab
            .insert(chrome.project_tabs.key().to_owned(), created_tab.to_owned());
    }
    if let (Some(position), Some(tab)) = (next_tab_position, created_tab.as_deref()) {
        let tab_move = WorkspaceOp::TabMove {
            tab: tab.to_owned(),
            position,
            workspace: None,
            node: None,
        };
        // Awaited inline until A4b (#23232) moves this spawn off the loop.
        match workspace.daemon().workspace_op(tab_move).await {
            Ok(_) => {}
            Err(DaemonError::Workspace(error)) => chrome.notify(Toast::warning(error.reason)),
            Err(error) => chrome.notify(Toast::warning(format!(
                "Shell opened, but tab order could not be saved: {}",
                FrameError::from(error)
            ))),
        }
    }
    if replaced_draft {
        let workspace_id = workspace
            .workspace_model()
            .map(|model| model.workspace.id.clone())
            .ok_or_else(|| FrameError::Protocol("workspace vanished during shell spawn".into()))?;
        let node = workspace.attach_target().node.clone();
        let snapshot = workspace
            .daemon()
            .workspace_snapshot(node.as_deref(), &workspace_id)
            .await?;
        workspace.apply_workspace_snapshot(snapshot);
    } else {
        workspace.expect_placement(&terminal_id);
    }
    workspace.pending_spawns.insert(terminal_id.clone());
    workspace.fetch_roster().await?;
    workspace.attach_ready_panes().await?;
    super::sync_live_chrome(workspace, chrome);
    if replaced_draft {
        if let Some(pane) = workspace.pane_for_terminal(&terminal_id) {
            chrome.focus_pane(pane);
            super::control::focus_live_pane(workspace, pane);
        }
    }
    Ok(true)
}
