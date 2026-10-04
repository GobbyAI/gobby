//! Chrome actions for the live loop: keymap actions, relative focus,
//! terminal spawn/terminate, and action dispatch; the sidebar's own
//! actions live in `sidebar`.

use std::io::{self, Write};
use std::process::{Command, Stdio};

use crate::copy_mode::copy_or_request_selection;
use crate::daemon::{Daemon, KillOutcome, LiveDaemon, SpawnOutcome, SpawnRequest};
use crate::frame_source::FrameError;
use crate::prefs::{load_prefs, prefs_path};
use crate::startup::load_keymap;
use crate::ui::chrome::{attention_pane, Tab};
use crate::ui::dialogs::{CloseScope, CloseTarget, Dialog, RenameKind};
use crate::ui::keybind_help::KeybindHelpState;
use crate::ui::navigator::NavigatorState;
use crate::ui::sidebar::attention_order;
use crate::ui::status::Toast;
use crate::ui::tabs::tab_label;
use crate::ui::{Action, Chrome, Mode};
use crossterm::event::KeyEvent;
use gobby_terminal::layout::{self, find_in_direction, NavDirection};
use ratatui::layout::Rect;
use tokio::sync::mpsc::UnboundedSender;

use super::super::attention::{open_response_dialog, route_response_input};
use super::super::viewer_state::EMPTY_LOCAL_TAB_PREFIX;
use super::super::{PaneId, Workspace};
use super::control::{
    close_barrier, focus_live_pane, observe_live_pane, release_live_control, send_live_report,
    set_live_scroll_offset, take_live_control,
};
use super::daemon_ops::{
    close_daemon_pane, close_daemon_tab, kill_live_panes, move_active_daemon_tab, move_daemon_tab,
    move_focused_pane_to_tab, pane_close_op, place_live_terminal, rename_daemon_target,
    resize_daemon_split, swap_live_slots, tab_close_op,
};
use super::jobs::{issue_response, JobOutcome, LoopJobs};
use super::menu_dispatch::apply_live_menu_action;
use super::modal_input::{apply_rename, ModalOutcome};
use super::mouse::{MouseOutcome, Placement};
use super::orphans::destroy_orphans;
use super::projection::close_slot;
use super::projects::{
    close_live_terminal, close_project_confirmed, create_worktree, focus_agent, focus_project,
    focus_terminal, open_new_project_dialog, open_worktree, remove_worktree, submit_new_project,
};
use super::sync_live_chrome;
use super::workspace_actions::{daemon_pane_id, spawn_owned_live_shell};

mod sidebar;

pub(super) use sidebar::toggle_sidebar_pin;

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
    jobs: &mut LoopJobs,
    outcome: MouseOutcome,
) -> Result<bool, FrameError> {
    let outcomes = jobs.outcomes();
    match outcome {
        MouseOutcome::Handled | MouseOutcome::Ignore => {}
        MouseOutcome::Respond(code) => {
            if let Some(submission) = route_response_input(workspace, chrome, &KeyEvent::from(code))
            {
                issue_response(workspace.daemon(), jobs, submission);
            }
        }
        MouseOutcome::Focus { pane, observe_only } => {
            chrome.focus_pane(pane);
            if observe_only {
                observe_live_pane(workspace, pane);
            } else {
                focus_live_pane(workspace, pane);
            }
        }
        MouseOutcome::Action(Action::Quit) => return Ok(true),
        MouseOutcome::Action(action) => {
            handle_live_action(workspace, chrome, outcomes, action).await?
        }
        MouseOutcome::TakeFreeControl { pane } => workspace.request_control(pane, false),
        MouseOutcome::Spawn { placement } => {
            spawn_live_terminal(workspace, chrome, outcomes, placement).await?;
        }
        MouseOutcome::Write { pane, bytes } => {
            send_live_report(workspace, jobs, pane, &bytes)?;
        }
        MouseOutcome::FocusWrite { pane, bytes } => {
            chrome.focus_pane(pane);
            focus_live_pane(workspace, pane);
            send_live_report(workspace, jobs, pane, &bytes)?;
        }
        MouseOutcome::Scroll { pane, rows } => {
            set_live_scroll_offset(workspace, chrome, jobs, pane, rows);
        }
        MouseOutcome::Copy => {
            let mut output = std::io::stdout();
            copy_or_request_selection(workspace, chrome, &mut output).await?;
            output.flush()?;
        }
        MouseOutcome::OpenLink(url) => {
            if let Err(error) = open_link(&chrome.link_opener, &url) {
                chrome.notify(
                    Toast::warning(format!("Could not open link with {}", chrome.link_opener))
                        .with_body(error.to_string()),
                );
            }
        }
        MouseOutcome::FocusProject(project_id) => {
            focus_project(workspace, chrome, &project_id).await?;
        }
        MouseOutcome::FocusAgent(entry_id) => {
            focus_agent(workspace, chrome, outcomes, &entry_id).await?;
        }
        MouseOutcome::OpenWorktree(worktree_id) => {
            open_worktree(workspace, chrome, outcomes, &worktree_id).await?;
        }
        MouseOutcome::Menu { kind, action } => {
            return apply_live_menu_action(workspace, chrome, outcomes, kind, action).await;
        }
        MouseOutcome::Confirm(target) => {
            return apply_live_modal_outcome(
                workspace,
                chrome,
                outcomes,
                ModalOutcome::Confirm(target),
            )
            .await;
        }
        MouseOutcome::Modal(outcome) => {
            return apply_live_modal_outcome(workspace, chrome, outcomes, outcome).await;
        }
        MouseOutcome::MoveTab { tab, position } => {
            move_daemon_tab(workspace, outcomes, tab, position);
        }
        MouseOutcome::ResizeSplit { slot, ratio } => {
            resize_daemon_split(workspace, chrome, outcomes, slot, ratio);
        }
    }
    Ok(false)
}

/// Apply what a modal mode asked the loop for; `true` means quit.
pub(super) async fn apply_live_modal_outcome(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    outcome: ModalOutcome,
) -> Result<bool, FrameError> {
    match outcome {
        ModalOutcome::Consumed | ModalOutcome::Close | ModalOutcome::Passthrough => {}
        ModalOutcome::Focus(pane) => {
            chrome.focus_pane(pane);
            focus_live_pane(workspace, pane);
        }
        ModalOutcome::FocusProject(project_id) => {
            focus_project(workspace, chrome, &project_id).await?;
        }
        ModalOutcome::FocusTerminal(terminal_id) => {
            focus_terminal(workspace, chrome, outcomes, &terminal_id).await?;
        }
        ModalOutcome::OpenWorktree(worktree_id) => {
            open_worktree(workspace, chrome, outcomes, &worktree_id).await?;
        }
        ModalOutcome::Action(Action::Quit) => return Ok(true),
        ModalOutcome::Action(action) => {
            handle_live_action(workspace, chrome, outcomes, action).await?
        }
        ModalOutcome::Confirm(CloseTarget::Tab(tab_id)) => {
            if chrome.active_tab().is_some_and(|tab| tab.id == tab_id) {
                close_live_tab(workspace, chrome, outcomes).await?;
            }
        }
        ModalOutcome::Confirm(CloseTarget::Pane(pane)) => {
            if workspace.panes.contains_key(&pane) && chrome.focus_pane(pane) {
                close_live_pane(workspace, chrome, outcomes).await?;
            }
        }
        ModalOutcome::Confirm(CloseTarget::Terminal(pane)) => {
            close_live_terminal(workspace, chrome, outcomes, pane).await?;
        }
        ModalOutcome::Confirm(
            CloseTarget::Project(project_id) | CloseTarget::WorktreeGroup(project_id),
        ) => close_project_confirmed(workspace, chrome, &project_id).await?,
        ModalOutcome::Commit(kind, value) => {
            if !rename_daemon_target(workspace, chrome, outcomes, &kind, &value) {
                apply_rename(workspace, chrome, kind, value);
            }
        }
        ModalOutcome::InitProject(path) => submit_new_project(workspace, chrome, &path).await?,
        ModalOutcome::CreateWorktree {
            project_id,
            branch,
            base,
        } => {
            create_worktree(
                workspace,
                chrome,
                outcomes,
                &project_id,
                &branch,
                base.as_deref(),
            )
            .await?
        }
        ModalOutcome::RemoveWorktree(worktree_id) => {
            remove_worktree(workspace, chrome, &worktree_id).await?;
        }
        ModalOutcome::DestroyOrphans(rows) => destroy_orphans(workspace, chrome, rows).await?,
        ModalOutcome::Menu { kind, action } => {
            return apply_live_menu_action(workspace, chrome, outcomes, kind, action).await;
        }
    }
    Ok(false)
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
    outcomes: &UnboundedSender<JobOutcome>,
    action: Action,
) -> Result<(), FrameError> {
    match action {
        Action::NewTerminal | Action::SplitVertical => {
            spawn_live_terminal(workspace, chrome, outcomes, Placement::SplitRight).await?;
        }
        Action::SplitHorizontal => {
            spawn_live_terminal(workspace, chrome, outcomes, Placement::SplitDown).await?;
        }
        Action::NewTab => open_empty_tab(chrome),
        Action::NextWorkspace => {
            super::workspaces::switch_next_workspace(workspace, chrome).await?;
            sync_live_chrome(workspace, chrome);
        }
        Action::NewProject => open_new_project_dialog(chrome),
        Action::CloseTerminal => {
            if let Some(pane_id) = chrome.focused_pane() {
                if chrome.prefs.confirm_close {
                    chrome.dialog = Some(Dialog::ConfirmClose {
                        target: CloseTarget::Terminal(pane_id),
                        title: workspace.pane(pane_id).display_name().to_owned(),
                        scope: CloseScope::Panes(1),
                    });
                    chrome.mode = Mode::ConfirmClose;
                } else {
                    close_live_terminal(workspace, chrome, outcomes, pane_id).await?;
                }
            }
        }
        Action::ClosePane => {
            if chrome.prefs.confirm_close {
                if let Some(pane_id) = chrome.focused_pane() {
                    chrome.dialog = Some(Dialog::ConfirmClose {
                        target: CloseTarget::Pane(pane_id),
                        title: workspace.pane(pane_id).display_name().to_owned(),
                        scope: CloseScope::Panes(1),
                    });
                    chrome.mode = Mode::ConfirmClose;
                } else {
                    close_live_pane(workspace, chrome, outcomes).await?;
                }
            } else {
                close_live_pane(workspace, chrome, outcomes).await?;
            }
        }
        Action::CloseTab => {
            let Some((id, panes)) = chrome
                .active_tab()
                .map(|tab| (tab.id.clone(), tab.slots.len()))
            else {
                return Ok(());
            };
            let title = tab_label(&*workspace, &chrome.tabs().tabs, chrome.active_index());
            if chrome.prefs.confirm_close {
                chrome.dialog = Some(Dialog::ConfirmClose {
                    target: CloseTarget::Tab(id),
                    title,
                    scope: CloseScope::Panes(panes),
                });
                chrome.mode = Mode::ConfirmClose;
            } else {
                close_live_tab(workspace, chrome, outcomes).await?;
            }
        }
        Action::TakeControl | Action::TakeBack => {
            if let Some(pane_id) = chrome.focused_pane() {
                take_live_control(workspace, pane_id);
            }
        }
        Action::Respond => open_response_dialog(workspace, chrome, None).await?,
        Action::CopyMode => chrome.mode = Mode::Copy,
        // Help opens fresh: a search left from an earlier visit would hide
        // the legend.
        Action::Help => {
            chrome.keybind_help = KeybindHelpState::default();
            chrome.mode = Mode::KeybindHelp;
        }
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
                release_live_control(workspace, pane_id);
            }
        }
        Action::PreviousTerminal | Action::CyclePanePrevious => {
            focus_relative_live_pane(workspace, chrome, -1).await?;
        }
        Action::NextTerminal | Action::CyclePaneNext => {
            focus_relative_live_pane(workspace, chrome, 1).await?;
        }
        Action::SwitchProject(_) | Action::PreviousProject | Action::NextProject => {
            sidebar::switch_project(workspace, chrome, action).await?;
        }
        Action::ToggleSidebar
        | Action::ToggleGroup
        | Action::NavigateUp
        | Action::NavigateDown
        | Action::CycleMachineFilter
        | Action::ToggleAgentSort
        | Action::ToggleProjectsFilter
        | Action::ToggleSessionsScope => sidebar::apply_sidebar_action(workspace, chrome, action),
        Action::Zoom => chrome.toggle_zoom(),
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
        Action::SwapPaneLeft => {
            swap_live_neighbour(workspace, chrome, outcomes, NavDirection::Left).await?
        }
        Action::SwapPaneDown => {
            swap_live_neighbour(workspace, chrome, outcomes, NavDirection::Down).await?
        }
        Action::SwapPaneUp => {
            swap_live_neighbour(workspace, chrome, outcomes, NavDirection::Up).await?
        }
        Action::SwapPaneRight => {
            swap_live_neighbour(workspace, chrome, outcomes, NavDirection::Right).await?;
        }
        Action::LastPane => {
            if let Some(pane_id) = chrome.last_focused {
                focus_live_shown_pane(workspace, chrome, pane_id).await?;
            }
        }
        Action::PreviousTab => activate_relative_live_tab(workspace, chrome, -1).await?,
        Action::NextTab => activate_relative_live_tab(workspace, chrome, 1).await?,
        Action::MoveTabLeft => move_active_daemon_tab(workspace, chrome, outcomes, -1),
        Action::MoveTabRight => move_active_daemon_tab(workspace, chrome, outcomes, 1),
        Action::MovePaneToTab(index) => {
            if let Some(index) = usize::from(index).checked_sub(1) {
                move_focused_pane_to_tab(workspace, chrome, outcomes, Some(index));
            }
        }
        Action::MovePaneToNewTab => move_focused_pane_to_tab(workspace, chrome, outcomes, None),
        Action::SwitchTab(index) => {
            if let Some(index) = usize::from(index).checked_sub(1) {
                activate_live_tab(workspace, chrome, index).await?;
            }
        }
        Action::PreviousAttention | Action::NextAttention | Action::FocusAttention(_) => {
            if let Some(entry_id) = pick_attention_entry(workspace, chrome, action) {
                jump_live_attention(workspace, chrome, outcomes, &entry_id).await?;
            }
        }
        Action::OpenNotificationTarget => {
            let target = chrome.latest_alert_target().map(str::to_owned);
            chrome.dismiss_toasts();
            if let Some(terminal_id) = target {
                focus_terminal(workspace, chrome, outcomes, &terminal_id).await?;
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
        focus_live_pane(workspace, pane_id);
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
    let tab = chrome.active_tab()?;
    let panes = tab
        .layout
        .panes(live_layout_area(chrome), chrome.tab_focus(tab));
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
async fn swap_live_neighbour(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    direction: NavDirection,
) -> Result<(), FrameError> {
    if let Some((focused, neighbour)) = live_neighbour_slots(chrome, direction) {
        swap_live_slots(workspace, chrome, outcomes, focused, neighbour);
    }
    Ok(())
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
    let next = (chrome.active_index() as isize + delta).rem_euclid(len as isize) as usize;
    activate_live_tab(workspace, chrome, next).await
}

/// Make tab `index` active through its focused pane so the lease follows;
/// out of range is ignored.
pub(super) async fn activate_live_tab(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    index: usize,
) -> Result<(), FrameError> {
    if let Some(pane_id) = chrome
        .tabs()
        .tabs
        .get(index)
        .and_then(|tab| chrome.viewer.focused_pane(tab))
    {
        focus_live_shown_pane(workspace, chrome, pane_id).await?;
    } else {
        chrome.activate_tab(index);
    }
    Ok(())
}

pub(super) fn open_live_rename(chrome: &mut Chrome, kind: RenameKind, value: String) {
    chrome.dialog = Some(Dialog::Rename {
        kind,
        cursor: value.chars().count(),
        value,
    });
    chrome.mode = Mode::Rename;
}

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

/// The attention entry an action names: the nth, or the one after or before
/// the entry whose pane is focused (none focused: the first or the last).
fn pick_attention_entry(
    workspace: &Workspace<LiveDaemon>,
    chrome: &Chrome,
    action: Action,
) -> Option<String> {
    let entries = attention_order(workspace, chrome);
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
/// and open the response dialog when the entry waits on attention.
async fn jump_live_attention(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    entry_id: &str,
) -> Result<(), FrameError> {
    // The terminal already shows the question, so a jump only reveals it.
    focus_agent(workspace, chrome, outcomes, entry_id).await
}

/// Re-read the prefs file (1.1) and the keymap override file it names
/// (4.2); a bad file keeps the current values and names itself.
fn reload_live_prefs(workspace: &Workspace<LiveDaemon>, chrome: &mut Chrome) {
    let home = workspace
        .gobby_home()
        .map(|home| home.to_path_buf())
        .unwrap_or_default();
    let path = prefs_path(&home);
    match load_prefs(&home) {
        Ok(prefs) => {
            match load_keymap(&prefs, &home, chrome.nested) {
                Ok(keymap) => chrome.keymap = keymap,
                Err(error) => chrome.notify(
                    Toast::warning("Keymap overrides kept as loaded").with_body(error.to_string()),
                ),
            }
            chrome.apply_prefs(prefs);
            chrome.notify(Toast::success(format!("Reloaded {}.", path.display())));
        }
        Err(error) => chrome.notify(
            Toast::warning(format!("Could not reload {}", path.display()))
                .with_body(error.to_string()),
        ),
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
    focus_live_pane(workspace, pane_ids[next]);
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

fn open_empty_tab(chrome: &mut Chrome) {
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
        if !spawn_owned_live_shell(workspace, chrome, placement, worktree_id, personal_cwd).await? {
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
    let outcome = workspace.daemon().spawn(request).await?;
    finish_live_shell_spawn(
        workspace,
        chrome,
        outcomes,
        placement,
        worktree_id,
        None,
        outcome,
    )
    .await
}

pub(super) async fn finish_live_shell_spawn(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    placement: Placement,
    worktree_id: Option<String>,
    project_override: Option<&str>,
    outcome: SpawnOutcome,
) -> Result<(), FrameError> {
    match outcome {
        SpawnOutcome::Created { terminal_id, .. } => {
            workspace.pending_spawns.insert(terminal_id.clone());
            workspace.fetch_roster().await?;
            workspace.attach_ready_panes().await?;
            place_live_terminal(
                workspace,
                chrome,
                outcomes,
                placement,
                &terminal_id,
                worktree_id,
                project_override,
            );
            sync_live_chrome(workspace, chrome);
        }
        SpawnOutcome::Refused { reason } => chrome.notify(Toast::warning(reason)),
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
