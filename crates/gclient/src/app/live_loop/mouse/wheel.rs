//! Wheel notches: what a scroll over each chrome region means.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseEvent, MouseEventKind};

use crate::ui::dialogs::Dialog;
use crate::ui::hit::{hit_test, sidebar_section_at, Hit, SidebarSection};
use crate::ui::sidebar::{section_metrics, TERMINAL_ROW};
use crate::ui::{Chrome, Mode, WorkspaceView};

use super::super::modal_input::{keybind_help_key, navigator_key};
use super::super::projects::project_dialog_key;
use super::{focus_active_tab, forward, on_roster, MouseOutcome, MOUSE_SCROLL_LINES};

/// A vertical notch while the keybinding help, the navigator, or the Alerts,
/// Open worktree or Destroy orphaned terminals dialog is up does what that
/// overlay's arrow keys do, so it stops where they stop: the help and the
/// alert log scroll `MOUSE_SCROLL_LINES` rows, and the navigator and the two
/// pick lists move their selection one row. A notch outside the popup does
/// nothing. `None` leaves every other event, and every other mode, to the
/// mode's own router.
pub(super) fn overlay<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    mouse: &MouseEvent,
) -> Option<MouseOutcome> {
    let key = KeyEvent::from(match mouse.kind {
        MouseEventKind::ScrollUp => KeyCode::Up,
        MouseEventKind::ScrollDown => KeyCode::Down,
        _ => return None,
    });
    let rows = match (chrome.mode, &chrome.dialog) {
        (Mode::KeybindHelp, _) | (Mode::ProjectDialog, Some(Dialog::Alerts { .. })) => {
            MOUSE_SCROLL_LINES
        }
        (Mode::Navigator, _)
        | (
            Mode::ProjectDialog,
            Some(Dialog::OpenWorktree { .. } | Dialog::DestroyOrphans { .. }),
        ) => 1,
        _ => return None,
    };
    let inside = matches!(
        hit_test(&chrome.view, mouse.column, mouse.row),
        Hit::Dialog | Hit::DialogButton(_)
    );
    if inside {
        for _ in 0..rows {
            match chrome.mode {
                Mode::KeybindHelp => keybind_help_key(chrome, &key),
                Mode::Navigator => navigator_key(ws, chrome, &key),
                _ => project_dialog_key(chrome, &key),
            };
        }
    }
    Some(MouseOutcome::Handled)
}

/// A wheel notch over `hit`.
///
/// Inside a pane whose app tracks the mouse the notch is reported to the app,
/// any of the four directions, unless shift is held (herdr
/// `forward_pane_reported_wheel`). Everything below is for vertical notches;
/// a horizontal one over anything else is left alone.
///
/// Over the tab bar a notch switches tabs, up = previous and down = next,
/// wrapping at either end (herdr's tab-bar wheel), and focuses the new tab's
/// pane the way a click would. Over the sidebar it scrolls the list under
/// the pointer by `MOUSE_SCROLL_LINES` rows (herdr `scroll_workspace_list`):
/// a row or scrollbar names its list, anything else goes by the section
/// rules above the pointer. A list that fits stays put.
///
/// Over a pane that was not reported to, its border or its scrollbar, the
/// notch goes by the pane's modes (herdr `forward_pane_wheel`): an
/// alternate-screen pane gets `MOUSE_SCROLL_LINES` arrow keys, up or down
/// (herdr alternate-scroll), delivered where a key would be; any other
/// native pane scrolls its scrollback `MOUSE_SCROLL_LINES` rows, up into
/// history and down toward the live edge, clamped to the depth the daemon
/// has confirmed so far, and a notch that would not move it is consumed. A
/// tmux pane has no daemon-side scroll offset — the daemon skips the host
/// verb for it and would answer with the client's own number — so its notch
/// is consumed rather than pretending to scroll. Focus never moves: in gclient
/// focus takes the lease, and a scroll is not a claim on the pane. A slot
/// whose pane has left the roster is stale until the next chrome sync and is
/// left alone.
pub(super) fn wheel<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    hit: Hit,
    mouse: &MouseEvent,
) -> MouseOutcome {
    if let Hit::Pane { slot, col, row } = hit {
        if !mouse.modifiers.contains(KeyModifiers::SHIFT) {
            let pane = chrome
                .pane_for_slot(slot)
                .filter(|pane| on_roster(ws, *pane));
            if let Some(outcome) = pane.and_then(|pane| {
                forward::report(ws, pane, mouse, KeyModifiers::empty(), (col, row))
            }) {
                return match outcome {
                    MouseOutcome::Write { pane, bytes } if chrome.focused_pane() != Some(pane) => {
                        MouseOutcome::FocusWrite { pane, bytes }
                    }
                    outcome => outcome,
                };
            }
        }
    }
    let up = match mouse.kind {
        MouseEventKind::ScrollUp => true,
        MouseEventKind::ScrollDown => false,
        _ => return MouseOutcome::Ignore,
    };
    let row = mouse.row;
    match hit {
        Hit::Tab(_) | Hit::TabScrollLeft | Hit::TabScrollRight | Hit::NewTab | Hit::TabBarEmpty => {
            let count = chrome.tabs().tabs.len();
            if count == 0 {
                return MouseOutcome::Handled;
            }
            let step = if up { count - 1 } else { 1 };
            chrome.activate_tab((chrome.active_index() + step) % count);
            focus_active_tab(chrome, false)
        }
        Hit::Machine(_)
        | Hit::Project(_)
        | Hit::Worktree(_)
        | Hit::WorktreeGlyph(_)
        | Hit::GroupToggle(_)
        | Hit::Agent(_)
        | Hit::SidebarScrollbar { .. }
        | Hit::SidebarEmpty
        | Hit::SidebarDivider => {
            let section = match hit {
                Hit::Machine(_) => SidebarSection::Machines,
                Hit::Project(_)
                | Hit::Worktree(_)
                | Hit::WorktreeGlyph(_)
                | Hit::GroupToggle(_) => SidebarSection::Projects,
                Hit::Agent(id) if id.starts_with(TERMINAL_ROW) => SidebarSection::Terminals,
                Hit::Agent(_) => SidebarSection::Agents,
                Hit::SidebarScrollbar { section, .. } => section,
                _ => sidebar_section_at(&chrome.view, row),
            };
            let metrics = section_metrics(ws, chrome, section);
            let max = metrics.max_offset_from_bottom;
            if max == 0 {
                return MouseOutcome::Handled;
            }
            let current = max - metrics.offset_from_bottom;
            let next = if up {
                current.saturating_sub(MOUSE_SCROLL_LINES)
            } else {
                (current + MOUSE_SCROLL_LINES).min(max)
            };
            chrome.sidebar.set_scroll(section, next);
            MouseOutcome::Handled
        }
        Hit::Pane { slot, .. } | Hit::PaneBorder(slot) | Hit::PaneScrollbar { slot, .. } => {
            let Some(pane) = chrome
                .pane_for_slot(slot)
                .filter(|pane| on_roster(ws, *pane))
            else {
                return MouseOutcome::Ignore;
            };
            let state = ws.pane(pane);
            let modes = state.latest_frame().map(|frame| &frame.modes);
            if modes.is_some_and(|modes| modes.alternate_on) {
                let key: &[u8] = if up { b"\x1b[A" } else { b"\x1b[B" };
                return MouseOutcome::Write {
                    pane,
                    bytes: key.repeat(MOUSE_SCROLL_LINES),
                };
            }
            if !state.backend.is_native() {
                return MouseOutcome::Handled;
            }
            let step = MOUSE_SCROLL_LINES as u32;
            let current = state.scroll_offset;
            let next = if up {
                clamp_scroll_rows(state.max_scroll, current.saturating_add(step))
            } else {
                current.saturating_sub(step)
            };
            if next == current {
                MouseOutcome::Handled
            } else {
                MouseOutcome::Scroll { pane, rows: next }
            }
        }
        _ => MouseOutcome::Ignore,
    }
}

/// Clamp a requested scroll offset against the last ceiling the daemon
/// confirmed, the way the web's `clampScrollRows` does.
///
/// `max_scroll` is zero both before the first `ScrollOffsetApplied` and when
/// the host holds no scrollback at all, so a zero ceiling means unknown, not
/// empty: send what was asked and let the reply decide. Clamping against an
/// unknown ceiling deadlocks the wheel, because the request that would teach
/// gclient the depth is the one the clamp suppresses.
fn clamp_scroll_rows(max_scroll: u32, requested: u32) -> u32 {
    if max_scroll > 0 {
        requested.min(max_scroll)
    } else {
        requested
    }
}

#[cfg(test)]
#[path = "wheel/tests.rs"]
mod tests;
