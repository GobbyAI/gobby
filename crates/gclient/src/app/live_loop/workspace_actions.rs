//! Layout mutations as daemon workspace ops (plan gclient-workspaces 4.2).
//! A daemon tab never changes locally: each mutation names explicit daemon
//! ids, and the chrome moves when the daemon's `workspace_event` is
//! projected. A local tab (opened without the daemon) keeps the chrome-only
//! paths, which the callers fall back to when these return `false`.

use gobby_terminal::layout;
use tokio::sync::mpsc::UnboundedSender;

use crate::daemon::{Daemon, DaemonError, LayoutAxis, LiveDaemon, WorkspaceOp};
use crate::frame_source::FrameError;
use crate::ui::chrome::Tab;
use crate::ui::pane_layout::lone_pane_inner_rect;
use crate::ui::Chrome;

use super::super::viewer_state::EMPTY_LOCAL_TAB_PREFIX;
use super::super::Workspace;
use super::actions::live_layout_area;
use super::daemon_ops::op_tag;
use super::jobs::{spawn_job, JobOutcome, JobResult, OwnedShell};
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

/// Rows and columns a new tab's first shell spawns at: the terminal rect its
/// lone pane gets in the live layout, so the PTY starts at the viewport. With
/// no room for a terminal, the daemon's default size applies.
pub(super) fn new_tab_viewport(chrome: &Chrome) -> (Option<u16>, Option<u16>) {
    let inner = lone_pane_inner_rect(live_layout_area(chrome), chrome.prefs.pane_scrollbars);
    if inner.is_empty() {
        return (None, None);
    }
    (Some(inner.height), Some(inner.width))
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
    let (rows, cols) = new_tab_viewport(chrome);
    Some(match (placement, focused) {
        (Placement::Tab, _) | (_, None) => WorkspaceOp::TabCreate {
            workspace: model.workspace.id.clone(),
            project_id,
            worktree_id,
            title: None,
            terminal_id,
            cwd,
            rows,
            cols,
            node: None,
        },
        (Placement::SplitRight, Some(pane)) => split(pane, LayoutAxis::Horizontal),
        (Placement::SplitDown, Some(pane)) => split(pane, LayoutAxis::Vertical),
    })
}

/// Create a shell as part of its daemon pane. The create and its tab-order
/// and snapshot steps run as one chained job; the loop installs the answer.
/// Returns `false` when no daemon placement applies.
pub(super) fn spawn_owned_live_shell(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    placement: Placement,
    worktree_id: Option<String>,
    cwd: Option<String>,
) -> bool {
    if !matches!(placement, Placement::Tab)
        && chrome
            .active_tab()
            .is_some_and(|tab| tab.is_local() && !tab.slots.is_empty())
    {
        return spawn_in_adopted_local_tab(workspace, chrome, outcomes, placement, cwd);
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
        return false;
    };
    let project = workspace.project_id().map(str::to_owned);
    let workspace_id = workspace
        .workspace_model()
        .map(|model| model.workspace.id.clone());
    let node = workspace.attach_target().node.clone();
    let roster = workspace.issue_roster_fetch(project.clone().unwrap_or_default());
    let daemon = workspace.daemon().clone();
    spawn_job(outcomes, op_tag(&daemon), async move {
        let result = async move {
            let reply = daemon.workspace_op(op).await?;
            let terminal_id = reply.result["panes"][0]["terminal_id"]
                .as_str()
                .ok_or_else(|| DaemonError::Protocol {
                    detail: "workspace shell op omitted terminal_id".into(),
                })?
                .to_owned();
            let pane_id = reply.result["panes"][0]["id"]
                .as_str()
                .ok_or_else(|| DaemonError::Protocol {
                    detail: "workspace shell op omitted the pane id".into(),
                })?
                .to_owned();
            let draft = match replacing_draft {
                Some(draft_id) => {
                    let created = reply.result["tabs"][0]["id"]
                        .as_str()
                        .ok_or_else(|| DaemonError::Protocol {
                            detail: "tab.create omitted the new tab id".into(),
                        })?
                        .to_owned();
                    Some((draft_id, created))
                }
                None => None,
            };
            let mut tab_order_warning = None;
            if let (Some(position), Some((_, created))) = (next_tab_position, draft.as_ref()) {
                let tab_move = WorkspaceOp::TabMove {
                    tab: created.clone(),
                    position,
                    workspace: None,
                    node: None,
                };
                match daemon.workspace_op(tab_move).await {
                    Ok(_) => {}
                    Err(DaemonError::Workspace(error)) => tab_order_warning = Some(error.reason),
                    Err(error) => {
                        tab_order_warning = Some(format!(
                            "Shell opened, but tab order could not be saved: {}",
                            FrameError::from(error)
                        ));
                    }
                }
            }
            let snapshot = match (&draft, workspace_id) {
                (Some(_), Some(workspace_id)) => Some(
                    daemon
                        .workspace_snapshot(node.as_deref(), &workspace_id)
                        .await?,
                ),
                (Some(_), None) => {
                    return Err(DaemonError::Protocol {
                        detail: "workspace vanished during shell spawn".into(),
                    });
                }
                (None, _) => None,
            };
            Ok(OwnedShell {
                terminal_id,
                pane_id,
                draft,
                snapshot,
                tab_order_warning,
                roster: Some(roster.await),
            })
        }
        .await;
        JobResult::Shell { project, result }
    });
    true
}
