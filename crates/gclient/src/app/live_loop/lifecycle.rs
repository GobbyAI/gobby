//! Terminal lifecycle for the live loop: spawning shells into tabs and
//! splits, closing panes and tabs, and terminating terminals.

use crate::daemon::{Daemon, KillOutcome, LiveDaemon, SpawnOutcome, SpawnRequest};
use crate::frame_source::FrameError;
use crate::ui::chrome::Tab;
use crate::ui::status::Toast;
use crate::ui::Chrome;
use gobby_terminal::layout;
use tokio::sync::mpsc::UnboundedSender;

use super::super::viewer_state::EMPTY_LOCAL_TAB_PREFIX;
use super::super::{PaneId, Workspace};
use super::control::{close_barrier, release_live_control};
use super::daemon_ops::{
    close_daemon_pane, close_daemon_tab, kill_live_panes, op_tag, pane_close_op, tab_close_op,
};
use super::jobs::{spawn_job, JobOutcome, JobResult, PaneAction};
use super::mouse::Placement;
use super::projection::close_slot;
use super::sync_live_chrome;
use super::workspace_actions::{daemon_pane_id, spawn_owned_live_shell};

/// The daemon row decides whether closing this pane may kill its terminal.
fn daemon_pane_is_adopted(
    workspace: &Workspace<LiveDaemon>,
    chrome: &Chrome,
    slot: layout::PaneId,
) -> Result<bool, FrameError> {
    if chrome.active_tab().is_none_or(Tab::is_local) {
        return Ok(false);
    }
    let pane_id = daemon_pane_id(chrome, slot)
        .ok_or_else(|| FrameError::Protocol("daemon pane has no slot mapping".into()))?;
    let pane = workspace
        .workspace_model()
        .and_then(|model| model.pane(&pane_id))
        .ok_or_else(|| FrameError::Protocol("daemon pane is missing from the workspace".into()))?;
    Ok(!pane.owns_terminal)
}

/// Close the focused pane: owned terminals are killed; adopted terminals
/// leave the daemon tab with their lease released and remain in the roster.
pub(super) async fn close_live_pane(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
) -> Result<(), FrameError> {
    if chrome
        .active_tab()
        .is_some_and(|tab| tab.is_local() && tab.slots.is_empty())
    {
        chrome.close_focused();
        return Ok(());
    }
    let Some(slot) = chrome.focus_slot() else {
        return Ok(());
    };
    let Some(pane_id) = chrome.focused_pane() else {
        close_daemon_pane(workspace, chrome, outcomes, slot, Vec::new());
        return Ok(());
    };
    // The close waits until the pane's accepted input (and its release) went out.
    if workspace.pane(pane_id).external || daemon_pane_is_adopted(workspace, chrome, slot)? {
        release_live_control(workspace, pane_id);
        let after = close_barrier(workspace, outcomes, pane_id)
            .into_iter()
            .collect();
        if !close_daemon_pane(workspace, chrome, outcomes, slot, after) {
            chrome.close_focused();
        }
        return Ok(());
    }
    // A refused kill keeps the pane; only a killed one's row is closed.
    let after = close_barrier(workspace, outcomes, pane_id)
        .into_iter()
        .collect();
    let then = pane_close_op(chrome, slot);
    kill_live_panes(workspace, outcomes, vec![pane_id], after, then);
    Ok(())
}

/// Close the active tab: kill owned terminals and release adopted ones.
pub(super) async fn close_live_tab(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
) -> Result<(), FrameError> {
    if chrome
        .active_tab()
        .is_some_and(|tab| tab.is_local() && tab.slots.is_empty())
    {
        chrome.close_focused();
        return Ok(());
    }
    let slots: Vec<(layout::PaneId, PaneId)> = chrome
        .active_tab()
        .map(|tab| {
            tab.slots
                .iter()
                .map(|(slot, pane)| (*slot, *pane))
                .collect()
        })
        .unwrap_or_default();
    let local = chrome.active_tab().is_some_and(Tab::is_local);
    let mut panes = Vec::with_capacity(slots.len());
    for (slot, pane_id) in slots {
        let preserve_terminal =
            workspace.pane(pane_id).external || daemon_pane_is_adopted(workspace, chrome, slot)?;
        panes.push((slot, pane_id, preserve_terminal));
    }
    if !local
        && panes
            .iter()
            .all(|(_, _, preserve_terminal)| *preserve_terminal)
    {
        let mut after = Vec::new();
        for &(_, pane_id, _) in &panes {
            release_live_control(workspace, pane_id);
            after.extend(close_barrier(workspace, outcomes, pane_id));
        }
        close_daemon_tab(workspace, chrome, outcomes, after);
        sync_live_chrome(workspace, chrome);
        return Ok(());
    }
    let mut owned_native = Vec::new();
    let mut owned_after = Vec::new();
    for &(slot, pane_id, preserve_terminal) in &panes {
        if preserve_terminal {
            release_live_control(workspace, pane_id);
            let after = close_barrier(workspace, outcomes, pane_id)
                .into_iter()
                .collect();
            if local || !close_daemon_pane(workspace, chrome, outcomes, slot, after) {
                let index = chrome.active_index();
                let viewer = &mut chrome.viewer;
                if let Some(tab) = chrome.project_tabs.set_mut().tabs.get_mut(index) {
                    close_slot(tab, viewer, slot);
                }
            }
        } else {
            // A killed pane leaves the roster and `sync_live_chrome` reaps
            // its slot; a refused kill keeps the pane, and so its tab.
            owned_native.push(pane_id);
            owned_after.extend(close_barrier(workspace, outcomes, pane_id));
        }
    }
    // A refused kill keeps its pane in the roster, and so its tab: the
    // daemon closes a tab only once every owned pane is gone. A local tab
    // goes when it empties.
    let then = tab_close_op(chrome);
    kill_live_panes(workspace, outcomes, owned_native, owned_after, then);
    sync_live_chrome(workspace, chrome);
    Ok(())
}

/// Spawn a terminal and show it where `placement` says: in a fresh tab, beside
/// the focused slot, or under it. A fresh tab opens in the focused checkout.
pub(super) async fn spawn_live_terminal(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    placement: Placement,
) -> Result<(), FrameError> {
    let cwd = workspace.focused_checkout_path();
    spawn_live_shell(workspace, chrome, outcomes, placement, cwd, None).await
}

pub(super) fn open_empty_tab(chrome: &mut Chrome) {
    let (layout, _) = layout::TileLayout::new();
    let mut tab = Tab::with_layout("", layout, Default::default());
    tab.id = format!("{EMPTY_LOCAL_TAB_PREFIX}{}", tab.id);
    let id = tab.id.clone();
    let project = chrome.project_tabs.key().to_owned();
    chrome.project_tabs.set_mut().tabs.push(tab);
    chrome.viewer.active_tab.insert(project, id);
}

/// `spawn_live_terminal` with an explicit working directory and the worktree
/// the tab is opened for. A daemon workspace op creates and owns its shell;
/// scripted local tabs retain the standalone spawn path.
pub(super) async fn spawn_live_shell(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    placement: Placement,
    cwd: Option<String>,
    worktree_id: Option<String>,
) -> Result<(), FrameError> {
    if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return Ok(());
    }
    if workspace.workspace_model().is_some() {
        let personal_cwd = (workspace.project_id()
            == Some(gobby_core::project::PERSONAL_PROJECT_ID))
        .then_some(cwd.clone())
        .flatten();
        if !spawn_owned_live_shell(
            workspace,
            chrome,
            outcomes,
            placement,
            worktree_id,
            personal_cwd,
        ) {
            chrome.notify(Toast::warning("Select a project before opening a shell"));
        }
        return Ok(());
    }
    if !chrome
        .active_tab()
        .is_some_and(|tab| tab.is_local() && !tab.id.starts_with(EMPTY_LOCAL_TAB_PREFIX))
    {
        chrome.notify(Toast::warning("Workspace is not ready to open a shell"));
        return Ok(());
    }
    let request = SpawnRequest {
        project_id: workspace.project_id().map(str::to_owned),
        cwd,
        ..SpawnRequest::default()
    };
    // One chained job: the create, then the roster that shows its pane. The
    // loop places the pane when that answer lands.
    let project = workspace.project_id().map(str::to_owned);
    let roster = workspace.issue_roster_fetch(project.clone().unwrap_or_default());
    let daemon = workspace.daemon().clone();
    spawn_job(outcomes, op_tag(&daemon), async move {
        let result = daemon.spawn(request).await;
        let roster = match &result {
            Ok(SpawnOutcome::Created { .. }) => Some(roster.await),
            _ => None,
        };
        JobResult::Spawned {
            then: PaneAction::Place {
                placement,
                worktree_id,
            },
            project,
            result,
            roster,
        }
    });
    Ok(())
}

pub(super) async fn terminate_live_terminal(
    workspace: &mut Workspace<LiveDaemon>,
    pane_id: PaneId,
) -> Result<(), FrameError> {
    if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return Ok(());
    }
    let terminal_id = workspace.pane(pane_id).terminal_id.clone();
    workspace
        .panes
        .get_mut(&pane_id)
        .expect("pane exists")
        .terminating = true;
    match workspace.daemon().terminate(&terminal_id).await? {
        KillOutcome::Killed { .. } => workspace.fetch_roster().await?,
        KillOutcome::Refused { .. } => {
            workspace
                .panes
                .get_mut(&pane_id)
                .expect("pane exists")
                .terminating = false;
        }
    }
    Ok(())
}
