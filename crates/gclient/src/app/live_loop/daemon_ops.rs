//! Workspace ops as jobs (plan A2.6 of gclient-daemon-resilience). Each sender
//! here names the daemon ids it changes and returns at once; the op runs
//! beside the loop and its refusal is raised where the outcome applies, so no
//! layout mutation ever sits between two frames.

use gobby_terminal::layout;
use tokio::sync::mpsc::UnboundedSender;
use tokio::sync::watch;

use crate::daemon::{
    Daemon, DaemonError, KillOutcome, LiveDaemon, WorkspaceErrorCode, WorkspaceOp,
};
use crate::frame_source::FrameError;
use crate::ui::dialogs::RenameKind;
use crate::ui::status::Toast;
use crate::ui::Chrome;

use super::super::{PaneId, Workspace};
use super::jobs::{spawn_job, JobKey, JobOutcome, JobResult, JobTag};
use super::menu::MenuAction;
use super::mouse::Placement;
use super::workspace_actions::{active_daemon_tab, daemon_pane_id, placement_op};

/// A close or focus hint refused `not_found` is done: the daemon reaps a
/// killed terminal's pane and closes an emptied tab itself, and the client's
/// follow-up names a row that is already gone.
fn idempotent(op: &WorkspaceOp) -> bool {
    matches!(
        op,
        WorkspaceOp::PaneClose { .. }
            | WorkspaceOp::TabClose { .. }
            | WorkspaceOp::WorkspaceSetFocusHints { .. }
    )
}

async fn run_op(daemon: &LiveDaemon, op: WorkspaceOp) -> Result<(), DaemonError> {
    let done = idempotent(&op);
    match daemon.workspace_op(op).await {
        Ok(_) => Ok(()),
        Err(DaemonError::Workspace(error))
            if done && error.code == WorkspaceErrorCode::NotFound =>
        {
            Ok(())
        }
        Err(error) => Err(error),
    }
}

fn op_tag(daemon: &LiveDaemon) -> JobTag {
    JobTag {
        id: 0,
        generation: daemon.generation(),
        key: JobKey::Op,
    }
}

/// Run `op` beside the loop. `placement` names a terminal whose placement
/// the op is expected to make; it is forgotten if the op fails.
fn issue_op(
    workspace: &Workspace<LiveDaemon>,
    outcomes: &UnboundedSender<JobOutcome>,
    op: WorkspaceOp,
    placement: Option<String>,
) {
    issue_op_after(workspace, outcomes, op, placement, Vec::new());
}

/// Run `op` once every barrier in `after` resolved, unless the connection
/// changed meanwhile: a close then names rows of a connection that is gone.
fn issue_op_after(
    workspace: &Workspace<LiveDaemon>,
    outcomes: &UnboundedSender<JobOutcome>,
    op: WorkspaceOp,
    placement: Option<String>,
    after: Vec<watch::Receiver<bool>>,
) {
    let daemon = workspace.daemon().clone();
    let tag = op_tag(&daemon);
    spawn_job(outcomes, tag.clone(), async move {
        await_barriers(after).await;
        let result = if daemon.generation() == tag.generation {
            run_op(&daemon, op).await
        } else {
            Ok(())
        };
        JobResult::Ops {
            unplaced: placement.filter(|_| result.is_err()),
            result,
        }
    });
}

/// Kill `panes`' terminals once every barrier in `after` was reached, then,
/// when every kill went through, run `then` (their row's or tab's close). A
/// refused kill keeps its pane, and with it the row and the tab.
pub(super) fn kill_live_panes(
    workspace: &mut Workspace<LiveDaemon>,
    outcomes: &UnboundedSender<JobOutcome>,
    panes: Vec<PaneId>,
    after: Vec<watch::Receiver<bool>>,
    then: Option<WorkspaceOp>,
) {
    if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return;
    }
    let mut kills = Vec::with_capacity(panes.len());
    for pane_id in panes {
        let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
        pane.terminating = true;
        kills.push((pane_id, pane.terminal_id.clone()));
    }
    let daemon = workspace.daemon().clone();
    spawn_job(outcomes, op_tag(&daemon), async move {
        await_barriers(after).await;
        let mut killed = false;
        let mut kept = Vec::new();
        let mut result = Ok(());
        for (pane, terminal_id) in kills {
            if result.is_err() {
                kept.push(pane);
                continue;
            }
            match daemon.terminate(&terminal_id).await {
                Ok(KillOutcome::Killed { .. }) => killed = true,
                Ok(KillOutcome::Refused { .. }) => kept.push(pane),
                Err(error) => {
                    kept.push(pane);
                    result = Err(error);
                }
            }
        }
        if let (Some(op), true) = (then, kept.is_empty()) {
            result = run_op(&daemon, op).await;
        }
        JobResult::Killed {
            killed,
            kept,
            result,
        }
    });
}

/// A kill's outcome. Only local pane bookkeeping and status change here, so
/// it applies whichever connection it ran on.
pub(super) fn apply_kills(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    killed: bool,
    kept: Vec<PaneId>,
    result: Result<(), DaemonError>,
) {
    for pane_id in kept {
        if let Some(pane) = workspace.panes.get_mut(&pane_id) {
            pane.terminating = false;
        }
    }
    // The relist reaps the killed terminals' panes.
    if killed {
        workspace.request_relist();
    }
    apply_ops(workspace, chrome, None, result);
}

/// Wait until every barrier was reached; a closed writer counts as reached.
pub(super) async fn await_barriers(after: Vec<watch::Receiver<bool>>) {
    for mut done in after {
        let _ = done.wait_for(|done| *done).await;
    }
}

/// Land an op: forget a placement it never made, and say why it failed. A
/// refusal is a warning; the layout stays as the daemon has it.
pub(super) fn apply_ops(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    unplaced: Option<String>,
    result: Result<(), DaemonError>,
) {
    if let Some(terminal_id) = unplaced {
        workspace.forget_placement(&terminal_id);
    }
    match result {
        Ok(()) => {}
        Err(DaemonError::Workspace(error)) => chrome.notify(Toast::warning(error.reason)),
        Err(error) => chrome.notify(Toast::error(FrameError::from(error).to_string())),
    }
}

/// Show `terminal_id`, already on the roster, per `placement`: through the
/// daemon when the window views its workspace, so the event that adds the
/// pane places it and this window follows; else on the chrome directly.
pub(super) fn place_live_terminal(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    placement: Placement,
    terminal_id: &str,
    worktree_id: Option<String>,
    project_override: Option<&str>,
) {
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
        issue_op(workspace, outcomes, op, Some(terminal_id.to_owned()));
        return;
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
}

/// Exchange two slots of the active tab: a `pane.swap` for a daemon tab,
/// the layout itself for a local one.
pub(super) fn swap_live_slots(
    workspace: &Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    first: layout::PaneId,
    second: layout::PaneId,
) {
    if active_daemon_tab(chrome).is_none() {
        if let Some(tab) = chrome.active_tab_mut() {
            tab.layout.swap_panes(first, second);
        }
        return;
    }
    let (Some(pane), Some(other)) = (
        daemon_pane_id(chrome, first),
        daemon_pane_id(chrome, second),
    ) else {
        return;
    };
    let op = WorkspaceOp::PaneSwap {
        pane,
        other,
        node: None,
    };
    issue_op(workspace, outcomes, op, None);
}

/// Close the active daemon tab's `slot` through the daemon; `false` for a
/// local tab, whose caller reaps the slot itself.
pub(super) fn close_daemon_pane(
    workspace: &Workspace<LiveDaemon>,
    chrome: &Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    slot: layout::PaneId,
    after: Vec<watch::Receiver<bool>>,
) -> bool {
    let Some(op) = pane_close_op(chrome, slot) else {
        return false;
    };
    issue_op_after(workspace, outcomes, op, None, after);
    true
}

/// The op closing `slot`'s row in the active daemon tab, if it has one.
pub(super) fn pane_close_op(chrome: &Chrome, slot: layout::PaneId) -> Option<WorkspaceOp> {
    active_daemon_tab(chrome)?;
    let pane = daemon_pane_id(chrome, slot)?;
    Some(WorkspaceOp::PaneClose { pane, node: None })
}

/// The op closing the active tab, if it is a daemon tab.
pub(super) fn tab_close_op(chrome: &Chrome) -> Option<WorkspaceOp> {
    let tab = active_daemon_tab(chrome)?.id.clone();
    Some(WorkspaceOp::TabClose { tab, node: None })
}

/// Close the active tab through the daemon; `false` for a local tab.
pub(super) fn close_daemon_tab(
    workspace: &Workspace<LiveDaemon>,
    chrome: &Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    after: Vec<watch::Receiver<bool>>,
) -> bool {
    let Some(op) = tab_close_op(chrome) else {
        return false;
    };
    issue_op_after(workspace, outcomes, op, None, after);
    true
}

/// Rename the active daemon tab or its focused pane through the daemon; an
/// empty value clears the title or label. `false` leaves a local tab's
/// rename, and every project rename, to `apply_rename`.
pub(super) fn rename_daemon_target(
    workspace: &Workspace<LiveDaemon>,
    chrome: &Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    kind: &RenameKind,
    value: &str,
) -> bool {
    let Some(tab) = active_daemon_tab(chrome) else {
        return false;
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
                return true;
            };
            WorkspaceOp::PaneRename {
                pane,
                label: value,
                node: None,
            }
        }
        RenameKind::Project(_) => return false,
    };
    issue_op(workspace, outcomes, op, None);
    true
}

/// The chrome-only menu items that move a daemon tab's rows: the swap with
/// the focused pane and the label clear. `false` for any other item, or a
/// local tab.
pub(super) fn apply_daemon_menu_action(
    workspace: &Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    action: &MenuAction,
) -> bool {
    if active_daemon_tab(chrome).is_none() {
        return false;
    }
    let slot_of = |chrome: &Chrome, pane| chrome.active_tab().and_then(|tab| tab.slot_for(pane));
    match action {
        MenuAction::SwapWithFocused(pane) => {
            if let (Some(focused), Some(slot)) = (chrome.focus_slot(), slot_of(chrome, *pane)) {
                if focused != slot {
                    swap_live_slots(workspace, chrome, outcomes, focused, slot);
                }
            }
            true
        }
        MenuAction::ClearPaneName(pane) => {
            if let Some(pane) = slot_of(chrome, *pane).and_then(|slot| daemon_pane_id(chrome, slot))
            {
                let op = WorkspaceOp::PaneRename {
                    pane,
                    label: None,
                    node: None,
                };
                issue_op(workspace, outcomes, op, None);
            }
            true
        }
        _ => false,
    }
}

/// `pane.resize` for the split a pane directly under it sits in.
pub(super) fn resize_daemon_split(
    workspace: &Workspace<LiveDaemon>,
    chrome: &Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    slot: layout::PaneId,
    ratio: f32,
) {
    let Some(pane) = daemon_pane_id(chrome, slot) else {
        return;
    };
    let op = WorkspaceOp::PaneResize {
        pane,
        ratio: f64::from(ratio),
        node: None,
    };
    issue_op(workspace, outcomes, op, None);
}

/// `tab.move` to `position` in the workspace's tab order.
pub(super) fn move_daemon_tab(
    workspace: &Workspace<LiveDaemon>,
    outcomes: &UnboundedSender<JobOutcome>,
    tab: String,
    position: u32,
) {
    let op = WorkspaceOp::TabMove {
        tab,
        position,
        workspace: None,
        node: None,
    };
    issue_op(workspace, outcomes, op, None);
}

pub(super) fn move_active_daemon_tab(
    workspace: &Workspace<LiveDaemon>,
    chrome: &Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    direction: isize,
) {
    let tabs = &chrome.tabs().tabs;
    let Some(target_index) = chrome.active_index().checked_add_signed(direction) else {
        return;
    };
    let Some((source, target)) = tabs.get(chrome.active_index()).zip(tabs.get(target_index)) else {
        return;
    };
    if source.is_local() || target.is_local() {
        return;
    }
    let Some(position) = workspace
        .workspace_model()
        .and_then(|model| model.tab(&target.id))
        .map(|tab| tab.position)
    else {
        return;
    };
    move_daemon_tab(workspace, outcomes, source.id.clone(), position);
}

/// Where a moved pane goes: a tab that exists, or one `TabCreate` makes.
enum MoveTarget {
    Tab(String),
    New(WorkspaceOp),
}

/// The new tab's id and its placeholder pane's id from a `TabCreate` reply.
fn created_tab(reply: &serde_json::Value) -> Result<(String, String), DaemonError> {
    let id = |kind: &str| {
        reply[kind][0]["id"]
            .as_str()
            .map(str::to_owned)
            .ok_or_else(|| DaemonError::Protocol {
                detail: format!("tab.create omitted the new {kind} id"),
            })
    };
    Ok((id("tabs")?, id("panes")?))
}

/// Move `pane` to `target`. A new tab's placeholder pane is closed once the
/// pane arrives, and the new tab is closed again if it never does. Reports
/// whether the pane moved and the first failure.
async fn move_pane(
    daemon: &LiveDaemon,
    pane: String,
    target: MoveTarget,
) -> (bool, Result<(), DaemonError>) {
    let (tab, placeholder) = match target {
        MoveTarget::Tab(tab) => (tab, None),
        MoveTarget::New(create) => {
            let created = match daemon.workspace_op(create).await {
                Ok(reply) => created_tab(&reply.result),
                Err(error) => Err(error),
            };
            match created {
                Ok((tab, placeholder)) => (tab, Some(placeholder)),
                Err(error) => return (false, Err(error)),
            }
        }
    };
    let moved = WorkspaceOp::PaneMove {
        pane,
        tab: tab.clone(),
        beside: None,
        axis: None,
        node: None,
    };
    if let Err(error) = run_op(daemon, moved).await {
        if placeholder.is_some() {
            // The move's refusal is the one worth showing; an empty tab
            // left behind is visible on its own.
            let _ = run_op(daemon, WorkspaceOp::TabClose { tab, node: None }).await;
        }
        return (false, Err(error));
    }
    let Some(placeholder) = placeholder else {
        return (true, Ok(()));
    };
    let close = WorkspaceOp::PaneClose {
        pane: placeholder,
        node: None,
    };
    (true, run_op(daemon, close).await)
}

pub(super) fn move_focused_pane_to_tab(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    destination: Option<usize>,
) {
    let Some(source) = active_daemon_tab(chrome) else {
        return;
    };
    let Some(pane) = daemon_pane_id(chrome, chrome.tab_focus(source)) else {
        return;
    };
    let Some(terminal_id) = workspace
        .workspace_model()
        .and_then(|model| model.pane(&pane))
        .and_then(|row| row.terminal_id.clone())
    else {
        return;
    };
    let target = if let Some(index) = destination {
        let Some(target) = chrome.tabs().tabs.get(index).filter(|tab| !tab.is_local()) else {
            return;
        };
        if target.id == source.id {
            return;
        }
        MoveTarget::Tab(target.id.clone())
    } else {
        let Some(model) = workspace.workspace_model() else {
            return;
        };
        let Some(project_id) = workspace.project_id() else {
            return;
        };
        MoveTarget::New(WorkspaceOp::TabCreate {
            workspace: model.workspace.id.clone(),
            project_id: project_id.to_owned(),
            worktree_id: source.worktree_id.clone(),
            title: None,
            terminal_id: None,
            cwd: None,
            node: None,
        })
    };

    workspace.expect_placement(&terminal_id);
    let daemon = workspace.daemon().clone();
    spawn_job(outcomes, op_tag(&daemon), async move {
        let (moved, result) = move_pane(&daemon, pane, target).await;
        JobResult::Ops {
            unplaced: (!moved).then_some(terminal_id),
            result,
        }
    });
}
