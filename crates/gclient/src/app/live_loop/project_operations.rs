//! Project attachment, termination and worktree removal for the live loop.

use super::super::sidebar_model::{ProjectEntry, WorktreeEntry};
use super::super::{PaneId, Workspace};
use super::control::close_barrier;
use super::daemon_ops::{apply_kills, await_barriers, kill_batch, kill_live_panes, op_tag};
use super::jobs::{spawn_job, JobOutcome, JobResult};
use super::modal_input::close_modal;
use super::project_navigation::{defer_project, ProjectAction};
use super::sync_live_chrome;
use crate::daemon::{Daemon, DaemonError, LiveDaemon, WorkspaceErrorCode, WorkspaceOp};
use crate::frame_source::FrameError;
use crate::ui::chrome::Tab;
use crate::ui::dialogs::project::plural;
use crate::ui::dialogs::{CloseScope, CloseTarget, Dialog};
use crate::ui::sidebar_rows::project_label;
use crate::ui::status::Toast;
use crate::ui::{Chrome, Mode};
use tokio::sync::mpsc::UnboundedSender;

pub async fn focus_project(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    project_id: &str,
) -> Result<(), FrameError> {
    defer_project(workspace, chrome, project_id, false, ProjectAction::Focus);
    Ok(())
}

pub async fn remove_worktree(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    worktree_id: &str,
) -> Result<(), FrameError> {
    let Some((project_id, _)) = worktree_entry(workspace, worktree_id) else {
        return Ok(());
    };
    if defer_project(
        workspace,
        chrome,
        &project_id,
        false,
        ProjectAction::RemoveWorktree(worktree_id.to_owned()),
    ) {
        return Ok(());
    }
    if !workspace.daemon_ready() || !workspace.removing_worktrees.insert(worktree_id.to_owned()) {
        return Ok(());
    }
    let panes: Vec<PaneId> = tagged_tabs(chrome, &project_id, worktree_id)
        .iter()
        .flat_map(|tab| tab.slots.values().copied())
        .collect();
    let tabs: Vec<String> = tagged_tabs(chrome, &project_id, worktree_id)
        .iter()
        .filter(|tab| !tab.is_local())
        .map(|tab| tab.id.clone())
        .collect();
    let after = panes
        .iter()
        .filter_map(|pane| close_barrier(workspace, outcomes, *pane))
        .collect();
    let kills: Vec<_> = panes
        .into_iter()
        .filter_map(|id| {
            let pane = workspace.panes.get_mut(&id)?;
            pane.terminating = true;
            Some((id, pane.terminal_id.clone()))
        })
        .collect();
    let daemon = workspace.daemon().clone();
    let captured: Vec<String> = kills.iter().map(|(_, terminal)| terminal.clone()).collect();
    let node = workspace.attach_target().node.clone();
    let worktree_id = worktree_id.to_owned();
    spawn_job(outcomes, op_tag(&daemon), async move {
        await_barriers(after).await;
        let (killed, kept, mut result) = kill_batch(&daemon, kills).await;
        let mut warnings = Vec::new();
        if kept.is_empty() && result.is_ok() {
            result =
                ensure_no_new_worktree_terminals(&daemon, node.as_deref(), &worktree_id, &captured)
                    .await;
        }
        if kept.is_empty() && result.is_ok() {
            for tab in tabs {
                match daemon
                    .workspace_op(WorkspaceOp::TabClose { tab, node: None })
                    .await
                {
                    Ok(_) => {}
                    Err(DaemonError::Workspace(error))
                        if error.code == WorkspaceErrorCode::NotFound => {}
                    Err(DaemonError::Workspace(error)) => warnings.push(error.reason),
                    Err(error) => {
                        result = Err(error);
                        break;
                    }
                }
            }
            if result.is_ok() {
                result = ensure_no_new_worktree_terminals(
                    &daemon,
                    node.as_deref(),
                    &worktree_id,
                    &captured,
                )
                .await;
                if result.is_ok() {
                    result = daemon.delete_worktree(&worktree_id).await;
                }
            }
        }
        JobResult::WorktreeRemoved(WorktreeRemoval {
            project_id,
            worktree_id,
            killed,
            kept,
            warnings,
            result,
        })
    });
    Ok(())
}

async fn ensure_no_new_worktree_terminals(
    daemon: &LiveDaemon,
    node: Option<&str>,
    worktree: &str,
    captured: &[String],
) -> Result<(), DaemonError> {
    let reply = daemon
        .workspace_op(WorkspaceOp::WorkspaceList {
            node: node.map(str::to_owned),
        })
        .await?;
    let rows = reply
        .result
        .as_array()
        .ok_or_else(|| DaemonError::Protocol {
            detail: "Cannot verify worktree occupants: invalid workspace list".into(),
        })?;
    for row in rows {
        let id = row
            .get("id")
            .and_then(|value| value.as_str())
            .ok_or_else(|| DaemonError::Protocol {
                detail: "Cannot verify worktree occupants: invalid workspace identity".into(),
            })?;
        let snapshot = match daemon.workspace_snapshot(node, id).await {
            Ok(snapshot) => snapshot,
            Err(DaemonError::Workspace(error)) if error.code == WorkspaceErrorCode::NotFound => {
                continue
            }
            Err(error) => return Err(error),
        };
        for tab in snapshot
            .tabs
            .iter()
            .filter(|tab| tab.worktree_id.as_deref() == Some(worktree))
        {
            for terminal in snapshot
                .panes
                .iter()
                .filter(|pane| pane.tab_id == tab.id)
                .filter_map(|pane| pane.terminal_id.as_ref())
                .filter(|terminal| !captured.contains(terminal))
            {
                let row = daemon.terminal(terminal).await?;
                if !matches!(
                    row.fields.get("state").and_then(|value| value.as_str()),
                    Some("closed" | "exited" | "failed")
                ) {
                    return Err(DaemonError::Protocol {
                        detail: format!(
                            "Worktree {worktree} has a new live terminal; retry removal"
                        ),
                    });
                }
            }
        }
    }
    Ok(())
}

/// Apply kill bookkeeping even when navigation superseded the dialog.
pub fn apply_worktree_removal(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    current_generation: bool,
    outcome: WorktreeRemoval,
) {
    let WorktreeRemoval {
        project_id,
        worktree_id,
        killed,
        kept,
        warnings,
        result,
    } = outcome;
    workspace.removing_worktrees.remove(&worktree_id);
    let surviving = kept.len();
    apply_kills(workspace, chrome, killed, kept, Ok(()));
    if result.is_ok() && surviving == 0 {
        workspace.pending_sidebar.projects = true;
    }
    let matching = current_generation
        && workspace.project_id() == Some(project_id.as_str())
        && matches!(&chrome.dialog, Some(Dialog::RemoveWorktree { worktree_id: id, .. }) if id == &worktree_id);
    for warning in warnings {
        chrome.notify(Toast::warning(format!("Worktree {worktree_id}: {warning}")));
    }
    if !matching {
        if let Err(error) = result {
            chrome.notify(Toast::warning(format!(
                "Cannot remove worktree {worktree_id}: {}",
                daemon_reason(&error)
            )));
        } else if surviving > 0 {
            chrome.notify(Toast::warning(format!(
                "Cannot remove worktree {worktree_id}: {} still live",
                plural(surviving, "terminal")
            )));
        }
        return;
    }
    match result {
        Err(error) => set_dialog_error(chrome, daemon_reason(&error)),
        Ok(()) if surviving > 0 => set_dialog_error(
            chrome,
            format!("{} still live", plural(surviving, "terminal")),
        ),
        Ok(()) => {
            close_modal(chrome);
        }
    }
}

#[derive(Debug)]
pub struct WorktreeRemoval {
    pub(super) project_id: String,
    pub(super) worktree_id: String,
    pub(super) killed: bool,
    pub(super) kept: Vec<PaneId>,
    pub(super) warnings: Vec<String>,
    pub(super) result: Result<(), DaemonError>,
}

pub async fn close_project(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    project_id: &str,
) -> Result<(), FrameError> {
    let Some(project) = project_entry(workspace, project_id) else {
        return Ok(());
    };
    if !chrome.prefs.confirm_close {
        return close_project_confirmed(workspace, chrome, outcomes, project_id).await;
    }
    let (tabs, panes) = chrome
        .project_tabs
        .sets
        .get(project_id)
        .map_or((0, 0), |set| {
            (
                set.tabs.len(),
                set.tabs.iter().map(|tab| tab.slots.len()).sum(),
            )
        });
    let (target, scope) = if project.worktrees.is_empty() {
        (
            CloseTarget::Project(project_id.to_owned()),
            CloseScope::Tabs { tabs, panes },
        )
    } else {
        (
            CloseTarget::WorktreeGroup(project_id.to_owned()),
            CloseScope::Group {
                workspaces: 1 + project.worktrees.len(),
                panes,
            },
        )
    };
    chrome.dialog = Some(Dialog::ConfirmClose {
        target,
        title: project_label(workspace, chrome, project_id).unwrap_or_default(),
        scope,
    });
    chrome.mode = Mode::ConfirmClose;
    Ok(())
}

pub async fn close_project_confirmed(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    project_id: &str,
) -> Result<(), FrameError> {
    if defer_project(
        workspace,
        chrome,
        project_id,
        false,
        ProjectAction::CloseProject(project_id.to_owned()),
    ) {
        return Ok(());
    }
    let panes: Vec<PaneId> = chrome
        .tabs()
        .tabs
        .iter()
        .flat_map(|tab| tab.slots.values().copied())
        .collect();
    let panes: Vec<PaneId> = panes
        .into_iter()
        .filter(|pane| !workspace.pane(*pane).terminating)
        .collect();
    let mut after = Vec::new();
    for pane in &panes {
        after.extend(close_barrier(workspace, outcomes, *pane));
    }
    kill_live_panes(workspace, outcomes, panes, after, None);
    sync_live_chrome(workspace, chrome);
    Ok(())
}

pub(super) fn project_entry<'a, D: Daemon>(
    workspace: &'a Workspace<D>,
    project_id: &str,
) -> Option<&'a ProjectEntry> {
    workspace
        .sidebar()
        .projects
        .iter()
        .find(|project| project.project_id == project_id)
}

pub(super) fn worktree_entry<'a, D: Daemon>(
    workspace: &'a Workspace<D>,
    worktree_id: &str,
) -> Option<(String, &'a WorktreeEntry)> {
    workspace.sidebar().projects.iter().find_map(|project| {
        project
            .worktrees
            .iter()
            .find(|worktree| worktree.worktree_id == worktree_id)
            .map(|worktree| (project.project_id.clone(), worktree))
    })
}

pub(super) fn tagged_tabs<'a>(
    chrome: &'a Chrome,
    project_id: &str,
    worktree_id: &str,
) -> Vec<&'a Tab> {
    chrome
        .project_tabs
        .sets
        .get(project_id)
        .map(|set| {
            set.tabs
                .iter()
                .filter(|tab| tab.worktree_id.as_deref() == Some(worktree_id))
                .collect()
        })
        .unwrap_or_default()
}

pub(super) fn set_dialog_error(chrome: &mut Chrome, message: String) {
    match chrome.dialog.as_mut() {
        Some(
            Dialog::NewProject { error, .. }
            | Dialog::OpenProject { error, .. }
            | Dialog::NewWorktree { error, .. }
            | Dialog::RemoveWorktree { error, .. },
        ) => *error = Some(message),
        _ => chrome.notify(Toast::warning(message)),
    }
}

pub(super) fn daemon_reason(error: &DaemonError) -> String {
    let DaemonError::Protocol { detail } = error else {
        return error.to_string();
    };
    let Ok(body) = serde_json::from_str::<serde_json::Value>(detail) else {
        return detail.clone();
    };
    let detail_value = body.get("detail").unwrap_or(&body);
    ["git_error", "message", "error_code", "error"]
        .iter()
        .find_map(|key| {
            detail_value
                .get(key)
                .and_then(|value| value.as_str())
                .filter(|value| !value.trim().is_empty())
        })
        .or_else(|| detail_value.as_str())
        .map_or_else(|| detail.clone(), str::to_owned)
}
