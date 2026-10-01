//! Layout mutations as daemon workspace ops (plan gclient-workspaces 4.2).
//! A daemon tab never changes locally: each mutation names explicit daemon
//! ids, and the chrome moves when the daemon's `workspace_event` is
//! projected. A local tab (opened without the daemon) keeps the chrome-only
//! paths, which the callers fall back to when these return `false`.

use std::collections::{HashMap, HashSet};

use gobby_terminal::layout;
use ratatui::layout::Direction;

use crate::daemon::{Daemon, DaemonError, LayoutAxis, LiveDaemon, WorkspaceErrorCode, WorkspaceOp};
use crate::frame_source::FrameError;
use crate::ui::chrome::Tab;
use crate::ui::dialogs::RenameKind;
use crate::ui::status::Toast;
use crate::ui::Chrome;

use super::super::viewer_state::EMPTY_LOCAL_TAB_PREFIX;
use super::super::Workspace;
use super::menu::MenuAction;
use super::mouse::Placement;

/// Send `op`; `true` when the daemon accepted it. A refusal lands on the
/// status line and the layout stays as the daemon has it; any other
/// failure propagates. A close or focus hint refused `not_found` is done:
/// the daemon reaps a killed terminal's pane and closes an emptied tab
/// itself, and the client's follow-up names a row that is already gone.
pub(super) async fn send_workspace_op(
    workspace: &Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    op: WorkspaceOp,
) -> Result<bool, FrameError> {
    let idempotent = matches!(
        op,
        WorkspaceOp::PaneClose { .. }
            | WorkspaceOp::TabClose { .. }
            | WorkspaceOp::WorkspaceSetFocusHints { .. }
    );
    match workspace.daemon().workspace_op(op).await {
        Ok(_) => Ok(true),
        Err(DaemonError::Workspace(error))
            if idempotent && error.code == WorkspaceErrorCode::NotFound =>
        {
            Ok(true)
        }
        Err(DaemonError::Workspace(error)) => {
            chrome.notify(Toast::warning(error.reason));
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

type LocalSplit = (layout::PaneId, layout::PaneId, LayoutAxis, f64);

fn first_local_slot(node: &layout::Node) -> layout::PaneId {
    match node {
        layout::Node::Pane(slot) => *slot,
        layout::Node::Split { first, .. } => first_local_slot(first),
    }
}

fn local_split_steps(node: &layout::Node, steps: &mut Vec<LocalSplit>) {
    if let layout::Node::Split {
        direction,
        ratio,
        first,
        second,
    } = node
    {
        let axis = match direction {
            Direction::Horizontal => LayoutAxis::Horizontal,
            Direction::Vertical => LayoutAxis::Vertical,
        };
        steps.push((
            first_local_slot(first),
            first_local_slot(second),
            axis,
            f64::from(*ratio),
        ));
        local_split_steps(first, steps);
        local_split_steps(second, steps);
    }
}

async fn rollback_adopted_tab(
    workspace: &mut Workspace<LiveDaemon>,
    workspace_id: &str,
    tab_id: String,
) -> Result<(), FrameError> {
    match workspace
        .daemon()
        .workspace_op(WorkspaceOp::TabClose {
            tab: tab_id,
            node: None,
        })
        .await
    {
        Ok(_) => {}
        Err(DaemonError::Workspace(error)) if error.code == WorkspaceErrorCode::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    let node = workspace.attach_target().node.clone();
    let snapshot = workspace
        .daemon()
        .workspace_snapshot(node.as_deref(), workspace_id)
        .await?;
    workspace.apply_workspace_snapshot(snapshot);
    Ok(())
}

struct LocalAdoption {
    old_tab: String,
    new_tab: String,
    focused_pane: String,
    workspace_id: String,
    next_position: Option<u32>,
}

async fn adopt_local_tab(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
) -> Result<Option<LocalAdoption>, FrameError> {
    let tab = chrome
        .active_tab()
        .expect("local tab selected for adoption");
    let old_tab = tab.id.clone();
    let title = (!tab.title.is_empty()).then(|| tab.title.clone());
    let worktree_id = tab.worktree_id.clone();
    let focused_slot = chrome.tab_focus(tab);
    let first_slot = first_local_slot(tab.layout.root());
    let mut steps = Vec::new();
    local_split_steps(tab.layout.root(), &mut steps);
    let mut terminals = HashMap::new();
    let mut seen = HashSet::new();
    for slot in tab.layout.pane_ids() {
        let terminal_id = tab
            .slots
            .get(&slot)
            .and_then(|pane| workspace.panes.get(pane))
            .map(|pane| pane.terminal_id.clone());
        let Some(terminal_id) = terminal_id else {
            chrome.notify(Toast::warning(
                "A local pane disappeared; reopen the tab to split it",
            ));
            return Ok(None);
        };
        if !seen.insert(terminal_id.clone()) {
            chrome.notify(Toast::warning(
                "The same terminal appears twice in this tab",
            ));
            return Ok(None);
        }
        terminals.insert(slot, terminal_id);
    }
    let workspace_id = workspace
        .workspace_model()
        .map(|model| model.workspace.id.clone())
        .ok_or_else(|| FrameError::Protocol("workspace vanished during tab adoption".into()))?;
    let project_id = workspace
        .project_id()
        .ok_or_else(|| FrameError::Protocol("project vanished during tab adoption".into()))?
        .to_owned();
    let next_position = chrome
        .tabs()
        .tabs
        .iter()
        .skip(chrome.active_index() + 1)
        .find(|tab| !tab.is_local())
        .and_then(|tab| workspace.workspace_model()?.tab(&tab.id))
        .map(|row| row.position);
    let first_terminal = terminals[&first_slot].clone();
    let reply = match workspace
        .daemon()
        .workspace_op(WorkspaceOp::TabCreate {
            workspace: workspace_id.clone(),
            project_id,
            worktree_id,
            title,
            terminal_id: Some(first_terminal),
            cwd: None,
            node: None,
        })
        .await
    {
        Ok(reply) => reply,
        Err(DaemonError::Workspace(error)) => {
            chrome.notify(Toast::warning(error.reason));
            return Ok(None);
        }
        Err(error) => return Err(error.into()),
    };
    let new_tab = reply.result["tabs"][0]["id"]
        .as_str()
        .ok_or_else(|| FrameError::Protocol("adopted tab.create omitted tab id".into()))?
        .to_owned();
    let built: Result<String, DaemonError> = async {
        let first_pane = reply.result["panes"][0]["id"]
            .as_str()
            .ok_or_else(|| DaemonError::Protocol {
                detail: "adopted tab.create omitted pane id".into(),
            })?
            .to_owned();
        let mut pane_ids = HashMap::from([(first_slot, first_pane)]);
        for (base, added, axis, ratio) in steps {
            let pane = pane_ids.get(&base).ok_or_else(|| DaemonError::Protocol {
                detail: "local split lost its base pane".into(),
            })?;
            let reply = workspace
                .daemon()
                .workspace_op(WorkspaceOp::PaneSplit {
                    pane: pane.clone(),
                    axis,
                    terminal_id: Some(terminals[&added].clone()),
                    cwd: None,
                    node: None,
                })
                .await?;
            let new_pane = reply.result["panes"][0]["id"]
                .as_str()
                .ok_or_else(|| DaemonError::Protocol {
                    detail: "adopted pane.split omitted pane id".into(),
                })?
                .to_owned();
            if (ratio - 0.5).abs() > f64::EPSILON {
                workspace
                    .daemon()
                    .workspace_op(WorkspaceOp::PaneResize {
                        pane: pane.clone(),
                        ratio,
                        node: None,
                    })
                    .await?;
            }
            pane_ids.insert(added, new_pane);
        }
        pane_ids
            .get(&focused_slot)
            .cloned()
            .ok_or_else(|| DaemonError::Protocol {
                detail: "local tab lost its focused pane".into(),
            })
    }
    .await;
    let focused_pane = match built {
        Ok(pane) => pane,
        Err(error) => {
            rollback_adopted_tab(workspace, &workspace_id, new_tab).await?;
            if let DaemonError::Workspace(ref refusal) = error {
                chrome.notify(Toast::warning(refusal.reason.clone()));
                return Ok(None);
            }
            return Err(error.into());
        }
    };
    Ok(Some(LocalAdoption {
        old_tab,
        new_tab,
        focused_pane,
        workspace_id,
        next_position,
    }))
}

async fn spawn_in_adopted_local_tab(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    placement: Placement,
    cwd: Option<String>,
) -> Result<bool, FrameError> {
    let Some(adopted) = adopt_local_tab(workspace, chrome).await? else {
        return Ok(true);
    };
    let axis = match placement {
        Placement::SplitRight => LayoutAxis::Horizontal,
        Placement::SplitDown => LayoutAxis::Vertical,
        Placement::Tab => unreachable!("a local tab migration only serves splits"),
    };
    let spawned: Result<String, DaemonError> = async {
        let reply = workspace
            .daemon()
            .workspace_op(WorkspaceOp::PaneSplit {
                pane: adopted.focused_pane.clone(),
                axis,
                terminal_id: None,
                cwd,
                node: None,
            })
            .await?;
        reply.result["panes"][0]["terminal_id"]
            .as_str()
            .map(str::to_owned)
            .ok_or_else(|| DaemonError::Protocol {
                detail: "owned pane.split omitted terminal id".into(),
            })
    }
    .await;
    let terminal_id = match spawned {
        Ok(terminal_id) => terminal_id,
        Err(error) => {
            rollback_adopted_tab(workspace, &adopted.workspace_id, adopted.new_tab).await?;
            if let DaemonError::Workspace(ref refusal) = error {
                chrome.notify(Toast::warning(refusal.reason.clone()));
                return Ok(true);
            }
            return Err(error.into());
        }
    };
    if let Some(position) = adopted.next_position {
        if let Err(error) = workspace
            .daemon()
            .workspace_op(WorkspaceOp::TabMove {
                tab: adopted.new_tab.clone(),
                position,
                workspace: None,
                node: None,
            })
            .await
        {
            chrome.notify(Toast::warning(format!(
                "Shell opened, but tab order could not be saved: {error}"
            )));
        }
    }
    let node = workspace.attach_target().node.clone();
    let snapshot = match workspace
        .daemon()
        .workspace_snapshot(node.as_deref(), &adopted.workspace_id)
        .await
    {
        Ok(snapshot) => snapshot,
        Err(error) => {
            rollback_adopted_tab(workspace, &adopted.workspace_id, adopted.new_tab).await?;
            return Err(error.into());
        }
    };
    workspace.apply_workspace_snapshot(snapshot);
    let was_zoomed = chrome.viewer.zoomed.remove(&adopted.old_tab);
    let tab = chrome
        .active_tab_mut()
        .ok_or_else(|| FrameError::Protocol("local tab disappeared during adoption".into()))?;
    tab.id = adopted.new_tab.clone();
    chrome.viewer.focus.remove(&adopted.old_tab);
    if was_zoomed {
        chrome.viewer.zoomed.insert(adopted.new_tab.clone());
    }
    chrome
        .viewer
        .active_tab
        .insert(chrome.project_tabs.key().to_owned(), adopted.new_tab);
    workspace.pending_spawns.insert(terminal_id.clone());
    workspace.fetch_roster().await?;
    workspace.attach_ready_panes().await?;
    super::sync_live_chrome(workspace, chrome);
    if let Some(pane) = workspace.pane_for_terminal(&terminal_id) {
        chrome.focus_pane(pane);
        super::control::focus_live_pane(workspace, pane).await?;
    }
    Ok(true)
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
        if let Err(error) = send_workspace_op(
            workspace,
            chrome,
            WorkspaceOp::TabMove {
                tab: tab.to_owned(),
                position,
                workspace: None,
                node: None,
            },
        )
        .await
        {
            chrome.notify(Toast::warning(format!(
                "Shell opened, but tab order could not be saved: {error}"
            )));
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
            super::control::focus_live_pane(workspace, pane).await?;
        }
    }
    Ok(true)
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
    project_override: Option<&str>,
) -> Result<(), FrameError> {
    if let Some(op) = placement_op(
        workspace,
        chrome,
        placement,
        Some(terminal_id),
        worktree_id,
        None,
        project_override,
    ) {
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

pub(super) async fn move_active_daemon_tab(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    direction: isize,
) -> Result<(), FrameError> {
    let tabs = &chrome.tabs().tabs;
    let Some(target_index) = chrome.active_index().checked_add_signed(direction) else {
        return Ok(());
    };
    let Some((source, target)) = tabs.get(chrome.active_index()).zip(tabs.get(target_index)) else {
        return Ok(());
    };
    if source.is_local() || target.is_local() {
        return Ok(());
    }
    let Some(position) = workspace
        .workspace_model()
        .and_then(|model| model.tab(&target.id))
        .map(|tab| tab.position)
    else {
        return Ok(());
    };
    move_daemon_tab(workspace, chrome, source.id.clone(), position).await
}

pub(super) async fn move_focused_pane_to_tab(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    destination: Option<usize>,
) -> Result<(), FrameError> {
    let Some(source) = active_daemon_tab(chrome) else {
        return Ok(());
    };
    let Some(pane) = daemon_pane_id(chrome, chrome.tab_focus(source)) else {
        return Ok(());
    };
    let Some(terminal_id) = workspace
        .workspace_model()
        .and_then(|model| model.pane(&pane))
        .and_then(|row| row.terminal_id.clone())
    else {
        return Ok(());
    };
    let worktree_id = source.worktree_id.clone();
    let (target, placeholder) = if let Some(index) = destination {
        let Some(target) = chrome.tabs().tabs.get(index).filter(|tab| !tab.is_local()) else {
            return Ok(());
        };
        if target.id == source.id {
            return Ok(());
        }
        (target.id.clone(), None)
    } else {
        let Some(model) = workspace.workspace_model() else {
            return Ok(());
        };
        let Some(project_id) = workspace.project_id() else {
            return Ok(());
        };
        let create = WorkspaceOp::TabCreate {
            workspace: model.workspace.id.clone(),
            project_id: project_id.to_owned(),
            worktree_id,
            title: None,
            terminal_id: None,
            cwd: None,
            node: None,
        };
        let reply = match workspace.daemon().workspace_op(create).await {
            Ok(reply) => reply,
            Err(DaemonError::Workspace(error)) => {
                chrome.notify(Toast::warning(error.reason));
                return Ok(());
            }
            Err(error) => return Err(error.into()),
        };
        let tab = reply.result["tabs"][0]["id"]
            .as_str()
            .ok_or_else(|| FrameError::Protocol("tab.create omitted the new tab id".into()))?
            .to_owned();
        let first_pane = reply.result["panes"][0]["id"]
            .as_str()
            .ok_or_else(|| FrameError::Protocol("tab.create omitted the new pane id".into()))?
            .to_owned();
        (tab, Some(first_pane))
    };

    workspace.expect_placement(&terminal_id);
    let moved = send_workspace_op(
        workspace,
        chrome,
        WorkspaceOp::PaneMove {
            pane,
            tab: target.clone(),
            beside: None,
            axis: None,
            node: None,
        },
    )
    .await;
    if !matches!(moved, Ok(true)) {
        workspace.forget_placement(&terminal_id);
    }
    if !moved? {
        if placeholder.is_some() {
            send_workspace_op(
                workspace,
                chrome,
                WorkspaceOp::TabClose {
                    tab: target,
                    node: None,
                },
            )
            .await?;
        }
        return Ok(());
    }
    if let Some(placeholder) = placeholder {
        send_workspace_op(
            workspace,
            chrome,
            WorkspaceOp::PaneClose {
                pane: placeholder,
                node: None,
            },
        )
        .await?;
    }
    Ok(())
}
