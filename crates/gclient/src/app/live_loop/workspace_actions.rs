//! Layout mutations as daemon workspace ops (plan gclient-workspaces 4.2).
//! A daemon tab never changes locally: each mutation names explicit daemon
//! ids, and the chrome moves when the daemon's `workspace_event` is
//! projected. A local tab (opened without the daemon) keeps the chrome-only
//! paths, which the callers fall back to when these return `false`.

use gobby_terminal::layout;

use crate::daemon::{Daemon, DaemonError, LayoutAxis, LiveDaemon, WorkspaceOp};
use crate::frame_source::FrameError;
use crate::ui::chrome::Tab;
use crate::ui::dialogs::RenameKind;
use crate::ui::Chrome;

use super::super::Workspace;
use super::menu::MenuAction;
use super::mouse::Placement;

/// The focus a window shows on a daemon tab: `(project, tab, pane)`.
pub(super) type ShownFocus = (String, String, Option<String>);

/// Send `op`; `true` when the daemon accepted it. A refusal lands on the
/// status line and the layout stays as the daemon has it; any other
/// failure propagates.
pub(super) async fn send_workspace_op(
    workspace: &Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    op: WorkspaceOp,
) -> Result<bool, FrameError> {
    match workspace.daemon().workspace_op(op).await {
        Ok(_) => Ok(true),
        Err(DaemonError::Workspace(error)) => {
            chrome.status_message = Some(error.reason);
            Ok(false)
        }
        Err(error) => Err(error.into()),
    }
}

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
fn placement_op(
    workspace: &Workspace<LiveDaemon>,
    chrome: &Chrome,
    placement: Placement,
    terminal_id: &str,
    worktree_id: Option<String>,
) -> Option<WorkspaceOp> {
    let model = workspace.workspace_model()?;
    let project_id = workspace.project_id()?.to_owned();
    if chrome.active_tab().is_some_and(Tab::is_local) {
        return None;
    }
    let focused =
        active_daemon_tab(chrome).and_then(|tab| daemon_pane_id(chrome, chrome.tab_focus(tab)));
    let terminal_id = Some(terminal_id.to_owned());
    let split = |pane, axis| WorkspaceOp::PaneSplit {
        pane,
        axis,
        terminal_id: terminal_id.clone(),
        node: None,
    };
    Some(match (placement, focused) {
        (Placement::Tab, _) | (_, None) => WorkspaceOp::TabCreate {
            workspace: model.workspace.id.clone(),
            project_id,
            worktree_id,
            title: None,
            terminal_id,
            node: None,
        },
        (Placement::SplitRight, Some(pane)) => split(pane, LayoutAxis::Horizontal),
        (Placement::SplitDown, Some(pane)) => split(pane, LayoutAxis::Vertical),
    })
}

/// Show `terminal_id`, already on the roster, per `placement`: through the
/// daemon when the window views its workspace, so the event that adds the
/// pane places it and this window follows; else on the chrome directly.
pub(super) async fn place_live_terminal(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    placement: Placement,
    terminal_id: &str,
    worktree_id: Option<String>,
) -> Result<(), FrameError> {
    if let Some(op) = placement_op(workspace, chrome, placement, terminal_id, worktree_id) {
        workspace.expect_placement(terminal_id);
        if !send_workspace_op(workspace, chrome, op).await? {
            workspace.forget_placement(terminal_id);
        }
        return Ok(());
    }
    if let Some(pane) = workspace.pane_for_terminal(terminal_id) {
        let title = workspace.pane(pane).display_name();
        match placement {
            Placement::Tab => chrome.open_tab(pane, title),
            Placement::SplitRight => {
                chrome.open_pane(pane, title);
            }
            Placement::SplitDown => {
                chrome.open_pane_below(pane, title);
            }
        }
    }
    Ok(())
}

/// Exchange two slots of the active tab: a `pane.swap` for a daemon tab,
/// the layout itself for a local one.
pub(super) async fn swap_live_slots(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    first: layout::PaneId,
    second: layout::PaneId,
) -> Result<(), FrameError> {
    if active_daemon_tab(chrome).is_none() {
        if let Some(tab) = chrome.active_tab_mut() {
            tab.layout.swap_panes(first, second);
        }
        return Ok(());
    }
    let (Some(pane), Some(other)) = (
        daemon_pane_id(chrome, first),
        daemon_pane_id(chrome, second),
    ) else {
        return Ok(());
    };
    let op = WorkspaceOp::PaneSwap {
        pane,
        other,
        node: None,
    };
    send_workspace_op(workspace, chrome, op).await?;
    Ok(())
}

/// Close the active daemon tab's `slot` through the daemon; `false` for a
/// local tab, whose caller reaps the slot itself.
pub(super) async fn close_daemon_pane(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    slot: layout::PaneId,
) -> Result<bool, FrameError> {
    if active_daemon_tab(chrome).is_none() {
        return Ok(false);
    }
    let Some(pane) = daemon_pane_id(chrome, slot) else {
        return Ok(false);
    };
    send_workspace_op(
        workspace,
        chrome,
        WorkspaceOp::PaneClose { pane, node: None },
    )
    .await?;
    Ok(true)
}

/// Close the active tab through the daemon; `false` for a local tab.
pub(super) async fn close_daemon_tab(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
) -> Result<bool, FrameError> {
    let Some(tab) = active_daemon_tab(chrome).map(|tab| tab.id.clone()) else {
        return Ok(false);
    };
    send_workspace_op(workspace, chrome, WorkspaceOp::TabClose { tab, node: None }).await?;
    Ok(true)
}

/// Rename the active daemon tab or its focused pane through the daemon; an
/// empty value clears the title or label. `false` leaves a local tab's
/// rename, and every project rename, to `apply_rename`.
pub(super) async fn rename_daemon_target(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    kind: &RenameKind,
    value: &str,
) -> Result<bool, FrameError> {
    let Some(tab) = active_daemon_tab(chrome) else {
        return Ok(false);
    };
    let value = (!value.is_empty()).then(|| value.to_owned());
    let op = match kind {
        RenameKind::Tab => WorkspaceOp::TabRename {
            tab: tab.id.clone(),
            title: value,
            node: None,
        },
        RenameKind::Pane | RenameKind::Terminal => {
            let Some(pane) = daemon_pane_id(chrome, chrome.tab_focus(tab)) else {
                return Ok(true);
            };
            WorkspaceOp::PaneRename {
                pane,
                label: value,
                node: None,
            }
        }
        RenameKind::Project(_) => return Ok(false),
    };
    send_workspace_op(workspace, chrome, op).await?;
    Ok(true)
}

/// The chrome-only menu items that move a daemon tab's rows: the swap with
/// the focused pane and the label clear. `false` for any other item, or a
/// local tab.
pub(super) async fn apply_daemon_menu_action(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    action: &MenuAction,
) -> Result<bool, FrameError> {
    if active_daemon_tab(chrome).is_none() {
        return Ok(false);
    }
    let slot_of = |chrome: &Chrome, pane| chrome.active_tab().and_then(|tab| tab.slot_for(pane));
    match action {
        MenuAction::SwapWithFocused(pane) => {
            if let (Some(focused), Some(slot)) = (chrome.focus_slot(), slot_of(chrome, *pane)) {
                if focused != slot {
                    swap_live_slots(workspace, chrome, focused, slot).await?;
                }
            }
            Ok(true)
        }
        MenuAction::ClearPaneName(pane) => {
            if let Some(pane) = slot_of(chrome, *pane).and_then(|slot| daemon_pane_id(chrome, slot))
            {
                let op = WorkspaceOp::PaneRename {
                    pane,
                    label: None,
                    node: None,
                };
                send_workspace_op(workspace, chrome, op).await?;
            }
            Ok(true)
        }
        _ => Ok(false),
    }
}

/// `pane.resize` for the split a pane directly under it sits in.
pub(super) async fn resize_daemon_split(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    slot: layout::PaneId,
    ratio: f32,
) -> Result<(), FrameError> {
    let Some(pane) = daemon_pane_id(chrome, slot) else {
        return Ok(());
    };
    let op = WorkspaceOp::PaneResize {
        pane,
        ratio: f64::from(ratio),
        node: None,
    };
    send_workspace_op(workspace, chrome, op).await?;
    Ok(())
}

/// `tab.move` to `position` in the workspace's tab order.
pub(super) async fn move_daemon_tab(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    tab: String,
    position: u32,
) -> Result<(), FrameError> {
    let op = WorkspaceOp::TabMove {
        tab,
        position,
        workspace: None,
        node: None,
    };
    send_workspace_op(workspace, chrome, op).await?;
    Ok(())
}

/// The focus this window shows when the active tab is the daemon's.
fn shown_focus(workspace: &Workspace<LiveDaemon>, chrome: &Chrome) -> Option<ShownFocus> {
    let project = workspace.project_id()?.to_owned();
    let tab = active_daemon_tab(chrome)?;
    let pane = daemon_pane_id(chrome, chrome.tab_focus(tab));
    Some((project, tab.id.clone(), pane))
}

/// Send the focus hints when the shown focus moved since `last`, so the
/// next window on this workspace opens where this one left off. Focus,
/// zoom and the active tab stay this window's; only the hint travels.
pub(super) async fn send_focus_hints_if_changed(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    last: &mut Option<ShownFocus>,
) -> Result<(), FrameError> {
    if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return Ok(());
    }
    let Some(focus) = shown_focus(workspace, chrome) else {
        return Ok(());
    };
    if last.as_ref() == Some(&focus) {
        return Ok(());
    }
    let Some(model) = workspace.workspace_model() else {
        return Ok(());
    };
    let op = WorkspaceOp::WorkspaceSetFocusHints {
        workspace: model.workspace.id.clone(),
        project_id: Some(focus.0.clone()),
        tab: Some(focus.1.clone()),
        pane: focus.2.clone(),
        node: None,
    };
    if send_workspace_op(workspace, chrome, op).await? {
        *last = Some(focus);
    }
    Ok(())
}
