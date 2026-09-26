//! Locate a roster terminal's existing pane across workspaces before focus.

use crate::daemon::{Daemon, DaemonError, LiveDaemon, WorkspaceErrorCode, WorkspaceOp};
use crate::frame_source::FrameError;
use crate::ui::status::Toast;
use crate::ui::Chrome;

use super::super::Workspace;
use super::sync_live_chrome;

/// A roster terminal may live in another workspace on the same node. Find its
/// existing pane before considering a new placement in the current workspace.
pub(super) async fn locate_terminal_workspace(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    terminal_id: &str,
    reuse_client_pane: bool,
) -> Result<bool, FrameError> {
    let local_project = workspace.workspace_model().and_then(|model| {
        let pane = model
            .panes()
            .find(|pane| pane.terminal_id.as_deref() == Some(terminal_id))?;
        model.tab(&pane.tab_id).map(|tab| tab.project_id.clone())
    });
    if let Some(project) = local_project {
        if let Err(error) = show_terminal_project(workspace, chrome, &project).await {
            chrome.notify(Toast::warning(format!("Cannot open terminal: {error}")));
            return Ok(false);
        }
        sync_live_chrome(workspace, chrome);
        return Ok(true);
    }

    let node = workspace.attach_target().node.clone();
    let reply = match workspace
        .daemon()
        .workspace_op(WorkspaceOp::WorkspaceList { node: node.clone() })
        .await
    {
        Ok(reply) => reply,
        Err(error) => {
            warn_and_refresh_terminal(
                workspace,
                chrome,
                format!("Cannot locate terminal: {error}"),
            )
            .await;
            return Ok(false);
        }
    };
    let Some(rows) = reply.result.as_array() else {
        warn_and_refresh_terminal(
            workspace,
            chrome,
            "Cannot locate terminal: invalid workspace list".to_owned(),
        )
        .await;
        return Ok(false);
    };
    let current = workspace
        .workspace_model()
        .map(|model| model.workspace.id.as_str());
    for row in rows {
        let Some(id) = row.get("id").and_then(|id| id.as_str()) else {
            warn_and_refresh_terminal(
                workspace,
                chrome,
                "Cannot locate terminal: invalid workspace row".to_owned(),
            )
            .await;
            return Ok(false);
        };
        if current == Some(id) {
            continue;
        }
        let snapshot = match workspace
            .daemon()
            .workspace_snapshot(node.as_deref(), id)
            .await
        {
            Ok(snapshot) => snapshot,
            Err(DaemonError::Workspace(error)) if error.code == WorkspaceErrorCode::NotFound => {
                continue;
            }
            Err(error) => {
                warn_and_refresh_terminal(
                    workspace,
                    chrome,
                    format!("Cannot locate terminal: {error}"),
                )
                .await;
                return Ok(false);
            }
        };
        let project = snapshot
            .panes
            .iter()
            .find(|pane| pane.terminal_id.as_deref() == Some(terminal_id))
            .and_then(|pane| snapshot.tabs.iter().find(|tab| tab.id == pane.tab_id))
            .map(|tab| tab.project_id.clone());
        let Some(project) = project else {
            continue;
        };
        let attached = match workspace
            .daemon()
            .attach_workspace(node.as_deref(), Some(id), None)
            .await
        {
            Ok(attached) => attached,
            Err(error) => {
                warn_and_refresh_terminal(
                    workspace,
                    chrome,
                    format!("Cannot open terminal: {error}"),
                )
                .await;
                return Ok(false);
            }
        };
        let still_placed = attached
            .panes
            .iter()
            .any(|pane| pane.terminal_id.as_deref() == Some(terminal_id));
        workspace.apply_workspace_snapshot(attached);
        let mut target = workspace.attach_target().clone();
        target.workspace = Some(id.to_owned());
        target.project_id = None;
        workspace.set_attach_target(target);
        if !still_placed {
            warn_and_refresh_terminal(
                workspace,
                chrome,
                "Terminal moved; select it again".to_owned(),
            )
            .await;
            return Ok(false);
        }
        if let Err(error) = show_terminal_project(workspace, chrome, &project).await {
            chrome.notify(Toast::warning(format!("Cannot open terminal: {error}")));
            return Ok(false);
        }
        if workspace.pane_for_terminal(terminal_id).is_none() {
            chrome.notify(Toast::warning("Terminal is no longer available"));
            return Ok(false);
        }
        workspace.attach_ready_panes().await?;
        sync_live_chrome(workspace, chrome);
        return Ok(true);
    }

    // A pane already attached in this client can be focused directly even if
    // the roster and workspace snapshot arrived at different revisions.
    if reuse_client_pane && workspace.pane_for_terminal(terminal_id).is_some() {
        return Ok(true);
    }

    // A row that vanished since the last roster fetch must not remain clickable.
    if let Err(error) = workspace.fetch_roster().await {
        chrome.notify(Toast::warning(format!("Cannot refresh Sessions: {error}")));
        return Ok(false);
    }
    sync_live_chrome(workspace, chrome);
    let in_roster = workspace
        .roster_terminal_ids()
        .iter()
        .any(|listed| listed == terminal_id);
    let in_attention = workspace
        .sidebar()
        .agents
        .iter()
        .any(|agent| agent.terminal_id == terminal_id);
    if !in_roster && !in_attention {
        chrome.notify(Toast::warning("Terminal is no longer available"));
        return Ok(false);
    }
    Ok(true)
}

async fn warn_and_refresh_terminal(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    message: String,
) {
    if workspace.fetch_roster().await.is_ok() {
        sync_live_chrome(workspace, chrome);
    }
    chrome.notify(Toast::warning(message));
}

async fn show_terminal_project(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    project: &str,
) -> Result<(), FrameError> {
    if workspace.project_id() != Some(project) {
        workspace.select_project(project);
        workspace.fetch_roster().await?;
        workspace.request_focused_sessions();
        chrome.focus_project(project);
        chrome.sidebar.expanded_project = Some(project.to_owned());
        sync_live_chrome(workspace, chrome);
    }
    Ok(())
}
