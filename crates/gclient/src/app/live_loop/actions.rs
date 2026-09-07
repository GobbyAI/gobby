//! Chrome actions for the live loop: keymap actions, relative focus,
//! terminal spawn/terminate, and chrome sync from the workspace.

use std::io::Write;

use crate::copy_mode::copy_selection;
use crate::daemon::{Daemon, KillOutcome, LiveDaemon, SpawnOutcome, SpawnRequest};
use crate::frame_source::FrameError;
use crate::ui::{Action, Chrome, Mode};

use super::super::attention::open_response_dialog;
use super::super::{PaneId, Workspace};
use super::control::{
    focus_live_pane, observe_live_pane, release_live_control, send_live_write,
    set_live_scroll_offset, take_live_control,
};
use super::mouse::{MouseOutcome, Placement};

pub(super) fn sync_live_chrome(workspace: &Workspace<LiveDaemon>, chrome: &mut Chrome) {
    for tab in &mut chrome.tabs {
        let stale: Vec<_> = tab
            .slots
            .iter()
            .filter_map(|(slot, pane_id)| (!workspace.panes.contains_key(pane_id)).then_some(*slot))
            .collect();
        if stale.len() == tab.slots.len() {
            tab.slots.clear();
            continue;
        }
        for slot in stale {
            tab.layout.focus_pane(slot);
            let _ = tab.layout.close_focused();
            tab.slots.remove(&slot);
        }
    }
    chrome.tabs.retain(|tab| !tab.slots.is_empty());
    if chrome.active_tab >= chrome.tabs.len() {
        chrome.active_tab = chrome.tabs.len().saturating_sub(1);
    }

    let shown: Vec<_> = chrome
        .tabs
        .iter()
        .flat_map(|tab| tab.slots.values().copied())
        .collect();
    for terminal_id in workspace.roster_terminal_ids() {
        let Some(pane_id) = workspace.pane_for_terminal(&terminal_id) else {
            continue;
        };
        if !shown.contains(&pane_id) {
            chrome.open_pane(pane_id, workspace.pane(pane_id).display_name());
        }
    }
}

/// Apply what `route_mouse` decided. Focus moves chrome first and then the
/// lease (it follows focus), or only the workspace focus for an observe-only
/// click; actions dispatch exactly as their chords would; a spawn goes through
/// the same request as `NewTerminal`; forwarded bytes go to the pane; a
/// scroll moves the pane's viewport on its frame source; an attention click
/// focuses its terminal and opens that entry's prompt; a roster drop saves
/// the new order. Returns whether the client should exit,
/// like the key routers.
pub(super) async fn apply_live_mouse_outcome(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcome: MouseOutcome,
) -> Result<bool, FrameError> {
    match outcome {
        MouseOutcome::Handled | MouseOutcome::Ignore => {}
        MouseOutcome::Focus { pane, observe_only } => {
            chrome.focus_pane(pane);
            if observe_only {
                observe_live_pane(workspace, pane).await?;
            } else {
                focus_live_pane(workspace, pane).await?;
            }
        }
        MouseOutcome::Action(Action::Quit) => return Ok(true),
        MouseOutcome::Action(action) => handle_live_action(workspace, chrome, action).await?,
        MouseOutcome::Spawn { placement } => {
            spawn_live_terminal(workspace, chrome, placement).await?;
        }
        MouseOutcome::Write { pane, bytes } => {
            send_live_write(workspace, pane, &bytes, false).await?;
        }
        MouseOutcome::Scroll { pane, rows } => {
            set_live_scroll_offset(workspace, pane, rows).await?;
        }
        MouseOutcome::Attention { pane, entry_id } => {
            if let Some(pane) = pane {
                focus_live_pane(workspace, pane).await?;
            }
            open_response_dialog(workspace, chrome, Some(&entry_id)).await?;
        }
        MouseOutcome::Copy => {
            let mut output = std::io::stdout();
            copy_selection(workspace, chrome, &mut output)?;
            output.flush()?;
        }
        MouseOutcome::Reorder { order } => workspace
            .set_tab_order(&order)
            .map_err(|error| FrameError::Other(error.to_string()))?,
    }
    Ok(false)
}

pub(super) async fn handle_live_action(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    action: Action,
) -> Result<(), FrameError> {
    match action {
        Action::NewTerminal => {
            spawn_live_terminal(workspace, chrome, Placement::SplitRight).await?;
        }
        Action::NewTab => spawn_live_terminal(workspace, chrome, Placement::Tab).await?,
        Action::CloseTerminal | Action::ClosePane => {
            if let Some(pane_id) = chrome.focused_pane() {
                terminate_live_terminal(workspace, pane_id).await?;
                sync_live_chrome(workspace, chrome);
            }
        }
        Action::TakeControl | Action::TakeBack => {
            if let Some(pane_id) = chrome.focused_pane() {
                take_live_control(workspace, pane_id).await?;
            }
        }
        Action::Respond => open_response_dialog(workspace, chrome, None).await?,
        Action::CopyMode => chrome.mode = Mode::Copy,
        // Both are bound in the default keymap and both render (chrome_render
        // draws the help table and the settings pane), but neither was ever
        // dispatched here, so the advertised keys did nothing in the real
        // client. The wildcard arm below swallows any action added later:
        // triage a new Action here rather than letting it go quietly inert.
        Action::Help => chrome.mode = Mode::KeybindHelp,
        Action::Settings => chrome.mode = Mode::Settings,
        Action::ReleaseControl | Action::Detach => {
            if let Some(pane_id) = chrome.focused_pane() {
                release_live_control(workspace, pane_id).await?;
            }
        }
        Action::PreviousTerminal | Action::CyclePanePrevious => {
            focus_relative_live_pane(workspace, chrome, -1).await?;
        }
        Action::NextTerminal | Action::CyclePaneNext => {
            focus_relative_live_pane(workspace, chrome, 1).await?;
        }
        Action::SwitchTerminal(index) if index > 0 => {
            let pane_id = workspace
                .roster_terminal_ids()
                .get(usize::from(index - 1))
                .and_then(|terminal_id| workspace.pane_for_terminal(terminal_id));
            if let Some(pane_id) = pane_id {
                chrome.focus_pane(pane_id);
                focus_live_pane(workspace, pane_id).await?;
            }
        }
        _ => {}
    }
    Ok(())
}

pub(super) async fn focus_relative_live_pane(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    delta: isize,
) -> Result<(), FrameError> {
    let pane_ids: Vec<_> = workspace
        .roster_terminal_ids()
        .iter()
        .filter_map(|terminal_id| workspace.pane_for_terminal(terminal_id))
        .collect();
    if pane_ids.is_empty() {
        return Ok(());
    }
    let current = chrome
        .focused_pane()
        .and_then(|pane_id| pane_ids.iter().position(|candidate| *candidate == pane_id))
        .unwrap_or(0);
    let next = (current as isize + delta).rem_euclid(pane_ids.len() as isize) as usize;
    chrome.focus_pane(pane_ids[next]);
    focus_live_pane(workspace, pane_ids[next]).await
}

/// Spawn a terminal and show it where `placement` says: in a fresh tab, beside
/// the focused slot, or under it. The chrome sync afterwards still covers a
/// terminal the roster has not reported yet.
pub(super) async fn spawn_live_terminal(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    placement: Placement,
) -> Result<(), FrameError> {
    if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return Ok(());
    }
    let request = SpawnRequest {
        project_id: workspace.project_id().map(str::to_owned),
        ..SpawnRequest::default()
    };
    match workspace.daemon().spawn(request).await? {
        SpawnOutcome::Created { terminal_id, .. } => {
            workspace.pending_spawns.insert(terminal_id.clone());
            workspace.fetch_roster().await?;
            workspace.attach_ready_panes().await?;
            if let Some(pane) = workspace.pane_for_terminal(&terminal_id) {
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
            sync_live_chrome(workspace, chrome);
        }
        SpawnOutcome::Refused { reason } => chrome.status_message = Some(reason),
    }
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
