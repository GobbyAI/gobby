//! Chrome actions for the live loop: keymap actions, relative focus,
//! terminal spawn/terminate, and chrome sync from the workspace.

use std::io::{self, Write};
use std::process::{Command, Stdio};

use crate::copy_mode::copy_selection;
use crate::daemon::{Daemon, KillOutcome, LiveDaemon, SpawnOutcome, SpawnRequest};
use crate::frame_source::FrameError;
use crate::prefs::{load_prefs, prefs_path};
use crate::ui::chrome::attention_pane;
use crate::ui::dialogs::{CloseTarget, Dialog, RenameKind};
use crate::ui::navigator::NavigatorState;
use crate::ui::status::{Toast, ToastKind};
use crate::ui::{Action, Chrome, Mode};
use gobby_terminal::layout::{self, find_in_direction, NavDirection};
use ratatui::layout::Rect;

use super::super::attention::open_response_dialog;
use super::super::{PaneId, Workspace};
use super::control::{
    focus_live_pane, observe_live_pane, release_live_control, send_live_write,
    set_live_scroll_offset, take_live_control,
};
use super::menu::{apply_local_menu_action, ContextMenuKind, MenuAction};
use super::modal_input::{apply_rename, ModalOutcome};
use super::mouse::{MouseOutcome, Placement};

/// Reap the slots whose pane left the workspace and the tabs that emptied.
/// Panes are never opened here: the tab bar is restored from the snapshot or
/// seeded by `projects::restore_focused`, and grows only by user action.
pub(super) fn sync_live_chrome(workspace: &Workspace<LiveDaemon>, chrome: &mut Chrome) {
    let set = chrome.tabs_mut();
    for tab in &mut set.tabs {
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
    set.tabs.retain(|tab| !tab.slots.is_empty());
    if set.active_tab >= set.tabs.len() {
        set.active_tab = set.tabs.len().saturating_sub(1);
    }
}

/// Apply what `route_mouse` decided. Focus moves chrome first and then the
/// lease (it follows focus), or only the workspace focus for an observe-only
/// click; actions dispatch exactly as their chords would; a spawn goes through
/// the same request as `NewTerminal`; forwarded bytes go to the pane; a
/// scroll moves the pane's viewport on its frame source; an attention click
/// focuses its terminal and opens that entry's prompt; a link goes to
/// `Chrome::link_opener`, and a launch failure to a warning toast that names
/// it; a roster drop saves the new order. Returns whether the client should
/// exit, like the key routers.
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
        MouseOutcome::FocusWrite { pane, bytes } => {
            chrome.focus_pane(pane);
            focus_live_pane(workspace, pane).await?;
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
        MouseOutcome::OpenLink(url) => {
            if let Err(error) = open_link(&chrome.link_opener, &url) {
                chrome.toast = Some(Toast {
                    kind: ToastKind::Warning,
                    title: format!("Could not open link with {}", chrome.link_opener),
                    body: Some(error.to_string()),
                    target: None,
                });
            }
        }
        MouseOutcome::Reorder { order } => workspace
            .set_tab_order(&order)
            .map_err(|error| FrameError::Other(error.to_string()))?,
        MouseOutcome::Menu { kind, action } => {
            return apply_live_menu_action(workspace, chrome, kind, action).await;
        }
    }
    Ok(false)
}

/// Apply what a modal mode asked the loop for; `true` means quit.
pub(super) async fn apply_live_modal_outcome(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcome: ModalOutcome,
) -> Result<bool, FrameError> {
    match outcome {
        ModalOutcome::Consumed | ModalOutcome::Close | ModalOutcome::Passthrough => {}
        ModalOutcome::Focus(pane) => {
            chrome.focus_pane(pane);
            focus_live_pane(workspace, pane).await?;
        }
        ModalOutcome::Action(Action::Quit) => return Ok(true),
        ModalOutcome::Action(action) => handle_live_action(workspace, chrome, action).await?,
        ModalOutcome::Confirm(CloseTarget::Tab) => close_live_tab(workspace, chrome).await?,
        ModalOutcome::Confirm(CloseTarget::Pane | CloseTarget::Terminal) => {
            if let Some(pane) = chrome.focused_pane() {
                terminate_live_terminal(workspace, pane).await?;
                sync_live_chrome(workspace, chrome);
            }
        }
        ModalOutcome::Commit(kind, value) => apply_rename(workspace, chrome, kind, value),
        ModalOutcome::Menu { kind, action } => {
            return apply_live_menu_action(workspace, chrome, kind, action).await;
        }
    }
    Ok(false)
}

/// A context menu item. A keymap action runs as its chord would once the
/// menu's pane or tab is the focused one (the pane is observed, so the lease
/// stays the action's decision); `respond` focuses the entry's pane and opens
/// its dialog; the chrome-only items go through `apply_local_menu_action`.
/// Sidebar row items arrive with the projects sidebar (plan 5.3).
async fn apply_live_menu_action(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    kind: ContextMenuKind,
    action: MenuAction,
) -> Result<bool, FrameError> {
    match action {
        MenuAction::Act(action) => {
            focus_menu_target(workspace, chrome, &kind).await?;
            if action == Action::Quit {
                return Ok(true);
            }
            handle_live_action(workspace, chrome, action).await?;
        }
        MenuAction::Respond(entry_id) => {
            if let Some(pane) = attention_pane(workspace, &entry_id) {
                chrome.focus_pane(pane);
                focus_live_pane(workspace, pane).await?;
            }
            open_response_dialog(workspace, chrome, Some(&entry_id)).await?;
        }
        _ => {
            apply_local_menu_action(workspace, chrome, &action);
        }
    }
    Ok(false)
}

/// Make the menu's pane or tab the one keymap actions act on.
async fn focus_menu_target(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    kind: &ContextMenuKind,
) -> Result<(), FrameError> {
    match *kind {
        ContextMenuKind::Pane(pane) if chrome.focused_pane() != Some(pane) => {
            chrome.focus_pane(pane);
            observe_live_pane(workspace, pane).await?;
        }
        ContextMenuKind::Tab(index) if index != chrome.tabs().active_tab => {
            activate_live_tab(workspace, chrome, index).await?;
        }
        _ => {}
    }
    Ok(())
}

/// Hand `url` to `opener` detached: null stdio, and a thread reaps it so a
/// finished opener never lingers as a zombie. `Err` when the opener cannot
/// be spawned at all (missing binary, permission).
pub(super) fn open_link(opener: &str, url: &str) -> io::Result<()> {
    let mut child = Command::new(opener)
        .arg(url)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()?;
    std::thread::spawn(move || {
        let _ = child.wait();
    });
    Ok(())
}

pub(super) async fn handle_live_action(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    action: Action,
) -> Result<(), FrameError> {
    match action {
        Action::NewTerminal | Action::SplitVertical => {
            spawn_live_terminal(workspace, chrome, Placement::SplitRight).await?;
        }
        Action::SplitHorizontal => {
            spawn_live_terminal(workspace, chrome, Placement::SplitDown).await?;
        }
        Action::NewTab => spawn_live_terminal(workspace, chrome, Placement::Tab).await?,
        Action::CloseTerminal | Action::ClosePane => {
            if let Some(pane_id) = chrome.focused_pane() {
                terminate_live_terminal(workspace, pane_id).await?;
                sync_live_chrome(workspace, chrome);
            }
        }
        Action::CloseTab => {
            let Some((title, panes)) = chrome
                .active_tab()
                .map(|tab| (tab.title.clone(), tab.slots.len()))
            else {
                return Ok(());
            };
            if chrome.prefs.confirm_close {
                chrome.dialog = Some(Dialog::ConfirmClose {
                    target: CloseTarget::Tab,
                    title,
                    panes,
                });
                chrome.mode = Mode::ConfirmClose;
            } else {
                close_live_tab(workspace, chrome).await?;
            }
        }
        Action::TakeControl | Action::TakeBack => {
            if let Some(pane_id) = chrome.focused_pane() {
                take_live_control(workspace, pane_id).await?;
            }
        }
        Action::Respond => open_response_dialog(workspace, chrome, None).await?,
        Action::CopyMode => chrome.mode = Mode::Copy,
        Action::Help => chrome.mode = Mode::KeybindHelp,
        Action::Settings => chrome.mode = Mode::Settings,
        Action::ResizeMode => chrome.mode = Mode::Resize,
        Action::TerminalPicker | Action::Goto => {
            chrome.navigator = NavigatorState {
                search_focused: action == Action::Goto,
                ..NavigatorState::default()
            };
            chrome.mode = Mode::Navigator;
        }
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
        Action::SwitchTerminal(index) => {
            let pane_id = usize::from(index).checked_sub(1).and_then(|index| {
                let terminal_id = workspace.roster_terminal_ids().into_iter().nth(index)?;
                workspace.pane_for_terminal(&terminal_id)
            });
            if let Some(pane_id) = pane_id {
                chrome.focus_pane(pane_id);
                focus_live_pane(workspace, pane_id).await?;
            }
        }
        Action::Zoom => {
            if let Some(tab) = chrome.active_tab_mut() {
                tab.zoomed = !tab.zoomed;
            }
        }
        Action::ToggleSidebar => chrome.sidebar.collapsed = !chrome.sidebar.collapsed,
        Action::RenameTab => {
            if let Some(title) = chrome.active_tab().map(|tab| tab.title.clone()) {
                open_live_rename(chrome, RenameKind::Tab, title);
            }
        }
        Action::RenamePane | Action::RenameTerminal => {
            if let Some(pane_id) = chrome.focused_pane() {
                let kind = if action == Action::RenamePane {
                    RenameKind::Pane
                } else {
                    RenameKind::Terminal
                };
                let title = workspace.pane(pane_id).display_name().to_owned();
                open_live_rename(chrome, kind, title);
            }
        }
        Action::FocusPaneLeft => {
            focus_live_neighbour(workspace, chrome, NavDirection::Left).await?
        }
        Action::FocusPaneDown => {
            focus_live_neighbour(workspace, chrome, NavDirection::Down).await?
        }
        Action::FocusPaneUp => focus_live_neighbour(workspace, chrome, NavDirection::Up).await?,
        Action::FocusPaneRight => {
            focus_live_neighbour(workspace, chrome, NavDirection::Right).await?;
        }
        Action::NavigatePaneLeft => {
            navigate_live_neighbour(workspace, chrome, NavDirection::Left).await?;
        }
        Action::NavigatePaneDown => {
            navigate_live_neighbour(workspace, chrome, NavDirection::Down).await?;
        }
        Action::NavigatePaneUp => {
            navigate_live_neighbour(workspace, chrome, NavDirection::Up).await?
        }
        Action::NavigatePaneRight => {
            navigate_live_neighbour(workspace, chrome, NavDirection::Right).await?;
        }
        Action::SwapPaneLeft => swap_live_neighbour(chrome, NavDirection::Left),
        Action::SwapPaneDown => swap_live_neighbour(chrome, NavDirection::Down),
        Action::SwapPaneUp => swap_live_neighbour(chrome, NavDirection::Up),
        Action::SwapPaneRight => swap_live_neighbour(chrome, NavDirection::Right),
        Action::LastPane => {
            if let Some(pane_id) = chrome.last_focused {
                focus_live_shown_pane(workspace, chrome, pane_id).await?;
            }
        }
        Action::PreviousTab => activate_relative_live_tab(workspace, chrome, -1).await?,
        Action::NextTab => activate_relative_live_tab(workspace, chrome, 1).await?,
        Action::SwitchTab(index) => {
            if let Some(index) = usize::from(index).checked_sub(1) {
                activate_live_tab(workspace, chrome, index).await?;
            }
        }
        Action::NavigateUp | Action::NavigateDown => {
            let roster_len = workspace.roster_terminal_ids().len();
            if roster_len > 0 {
                let selected = chrome.sidebar.selected.min(roster_len - 1);
                chrome.sidebar.selected = if action == Action::NavigateUp {
                    selected.saturating_sub(1)
                } else {
                    (selected + 1).min(roster_len - 1)
                };
            }
            chrome.mode = Mode::Navigate;
        }
        Action::PreviousAttention | Action::NextAttention | Action::FocusAttention(_) => {
            if let Some(entry_id) = pick_attention_entry(workspace, chrome, action) {
                jump_live_attention(workspace, chrome, &entry_id).await?;
            }
        }
        Action::OpenNotificationTarget => {
            let pane_id = chrome
                .toast
                .take()
                .and_then(|toast| toast.target)
                .and_then(|terminal_id| workspace.pane_for_terminal(&terminal_id));
            if let Some(pane_id) = pane_id {
                focus_live_shown_pane(workspace, chrome, pane_id).await?;
            }
        }
        Action::ReloadConfig => reload_live_prefs(workspace, chrome),
        // The router answers `Quit` before dispatch; `CustomCommand` is held
        // in the keymap table for the plugin-menu decision (#20201) and never
        // bound.
        Action::Quit | Action::CustomCommand => {}
    }
    Ok(())
}

/// Focus `pane_id` where the chrome shows it, tab switch included, and move
/// the lease with it.
async fn focus_live_shown_pane(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    pane_id: PaneId,
) -> Result<(), FrameError> {
    if chrome.focus_pane(pane_id) {
        focus_live_pane(workspace, pane_id).await?;
    }
    Ok(())
}

/// Area the directional actions navigate: the last frame's terminal area,
/// or a nominal one before the first frame (the layout is ratios).
pub(super) fn live_layout_area(chrome: &Chrome) -> Rect {
    let area = chrome.view.terminal_area;
    if area.width > 0 && area.height > 0 {
        area
    } else {
        Rect::new(0, 0, 120, 40)
    }
}

/// The active tab's focused slot and its neighbour in `direction`.
fn live_neighbour_slots(
    chrome: &Chrome,
    direction: NavDirection,
) -> Option<(layout::PaneId, layout::PaneId)> {
    let panes = chrome.active_tab()?.layout.panes(live_layout_area(chrome));
    let focused = panes.iter().find(|pane| pane.is_focused)?;
    let neighbour = find_in_direction(focused, direction, &panes)?;
    Some((focused.id, neighbour))
}

async fn focus_live_neighbour(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    direction: NavDirection,
) -> Result<(), FrameError> {
    let pane_id =
        live_neighbour_slots(chrome, direction).and_then(|(_, slot)| chrome.pane_for_slot(slot));
    if let Some(pane_id) = pane_id {
        focus_live_shown_pane(workspace, chrome, pane_id).await?;
    }
    Ok(())
}

/// herdr keeps navigate mode across a directional focus made from it.
async fn navigate_live_neighbour(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    direction: NavDirection,
) -> Result<(), FrameError> {
    focus_live_neighbour(workspace, chrome, direction).await?;
    chrome.mode = Mode::Navigate;
    Ok(())
}

/// Exchange the focused slot with its neighbour; focus stays on the same
/// pane at its new position (herdr `swap_exact`).
fn swap_live_neighbour(chrome: &mut Chrome, direction: NavDirection) {
    if let Some((focused, neighbour)) = live_neighbour_slots(chrome, direction) {
        if let Some(tab) = chrome.active_tab_mut() {
            tab.layout.swap_panes(focused, neighbour);
        }
    }
}

async fn activate_relative_live_tab(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    delta: isize,
) -> Result<(), FrameError> {
    let len = chrome.tabs().tabs.len();
    if len == 0 {
        return Ok(());
    }
    let next = (chrome.tabs().active_tab as isize + delta).rem_euclid(len as isize) as usize;
    activate_live_tab(workspace, chrome, next).await
}

/// Make tab `index` active through its focused pane so the lease follows;
/// out of range is ignored.
async fn activate_live_tab(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    index: usize,
) -> Result<(), FrameError> {
    if let Some(pane_id) = chrome
        .tabs()
        .tabs
        .get(index)
        .and_then(|tab| tab.focused_pane())
    {
        focus_live_shown_pane(workspace, chrome, pane_id).await?;
    }
    Ok(())
}

fn open_live_rename(chrome: &mut Chrome, kind: RenameKind, value: String) {
    chrome.dialog = Some(Dialog::Rename {
        kind,
        cursor: value.chars().count(),
        value,
    });
    chrome.mode = Mode::Rename;
}

/// Terminate every pane of the active tab; `sync_live_chrome` then drops
/// the emptied tab.
pub(super) async fn close_live_tab(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
) -> Result<(), FrameError> {
    let panes: Vec<PaneId> = chrome
        .active_tab()
        .map(|tab| tab.slots.values().copied().collect())
        .unwrap_or_default();
    for pane_id in panes {
        terminate_live_terminal(workspace, pane_id).await?;
    }
    sync_live_chrome(workspace, chrome);
    Ok(())
}

/// The attention entry an action names: the nth, or the one after or before
/// the entry whose pane is focused (none focused: the first or the last).
fn pick_attention_entry(
    workspace: &Workspace<LiveDaemon>,
    chrome: &Chrome,
    action: Action,
) -> Option<String> {
    let entries = workspace.attention_entry_ids();
    let len = entries.len();
    if len == 0 {
        return None;
    }
    let current = chrome.focused_pane().and_then(|focused| {
        entries
            .iter()
            .position(|entry| attention_pane(workspace, entry) == Some(focused))
    });
    let index = match action {
        Action::FocusAttention(index) => usize::from(index).checked_sub(1)?,
        Action::NextAttention => current.map_or(0, |current| (current + 1) % len),
        Action::PreviousAttention => current.map_or(len - 1, |current| (current + len - 1) % len),
        _ => return None,
    };
    entries.get(index).cloned()
}

/// The 2.3 jump from the keyboard: focus the entry's pane (lease follows)
/// and open the response dialog when the entry is a prompt.
async fn jump_live_attention(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    entry_id: &str,
) -> Result<(), FrameError> {
    if let Some(pane_id) = attention_pane(&*workspace, entry_id) {
        focus_live_shown_pane(workspace, chrome, pane_id).await?;
    }
    open_response_dialog(workspace, chrome, Some(entry_id)).await?;
    Ok(())
}

/// Re-read the prefs file (1.1); a bad file keeps the current values and
/// names itself. The keymap reloads in 4.3.
fn reload_live_prefs(workspace: &Workspace<LiveDaemon>, chrome: &mut Chrome) {
    let home = workspace
        .gobby_home()
        .map(|home| home.to_path_buf())
        .unwrap_or_default();
    let path = prefs_path(&home);
    match load_prefs(&home) {
        Ok(prefs) => {
            chrome.apply_prefs(prefs);
            chrome.status_message = Some(format!("Reloaded {}", path.display()));
        }
        Err(error) => {
            chrome.toast = Some(Toast {
                kind: ToastKind::Warning,
                title: format!("Could not reload {}", path.display()),
                body: Some(error.to_string()),
                target: None,
            });
        }
    }
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
        cwd: match placement {
            Placement::Tab => workspace.focused_checkout_path(),
            Placement::SplitRight | Placement::SplitDown => None,
        },
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
