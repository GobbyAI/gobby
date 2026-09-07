//! Button presses, drags and releases: what each means on a chrome region.

use crossterm::event::{KeyModifiers, MouseButton};

use crate::app::PaneId;
use crate::ui::chrome::Tab;
use crate::ui::hit::Hit;
use crate::ui::{Chrome, WorkspaceView};

use super::{focus_active_tab, MouseGesture, MouseOutcome, Placement, TAB_DRAG_THRESHOLD};

/// A button went down on `hit`.
///
/// Left Down inside a pane focuses it (herdr `FocusPane`); alt makes that an
/// observe-only focus. A click on the pane that already has focus is left
/// alone so selection can start there, and a slot whose pane has left the
/// roster is stale until the next chrome sync, so it is not focused either.
///
/// On the tab bar a left Down activates the tab and focuses its pane (herdr
/// `FocusTab`), starting the drag that `up` may finish as a reorder; a scroll
/// arrow moves the bar one tab and stops it following the active tab; the
/// new-tab button asks for a spawn placed in a fresh tab. Every other region
/// is ignored until its section lands.
pub(super) fn down<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    hit: Hit,
    button: MouseButton,
    modifiers: KeyModifiers,
    column: u16,
) -> MouseOutcome {
    match (hit, button) {
        (Hit::Pane { slot, .. }, MouseButton::Left) => {
            let Some(pane) = chrome.pane_for_slot(slot) else {
                return MouseOutcome::Ignore;
            };
            if chrome.focused_pane() == Some(pane) || !on_roster(ws, pane) {
                return MouseOutcome::Ignore;
            }
            MouseOutcome::Focus {
                pane,
                observe_only: modifiers.contains(KeyModifiers::ALT),
            }
        }
        (Hit::Tab(index), MouseButton::Left) => {
            chrome.active_tab = index;
            chrome.tab_scroll_follow_active = true;
            chrome.gesture = Some(MouseGesture::TabDrag {
                index,
                origin_col: column,
                moved: false,
            });
            focus_active_tab(chrome, modifiers.contains(KeyModifiers::ALT))
        }
        (Hit::TabScrollLeft, MouseButton::Left) => {
            chrome.tab_scroll = chrome.tab_scroll.saturating_sub(1);
            chrome.tab_scroll_follow_active = false;
            MouseOutcome::Handled
        }
        (Hit::TabScrollRight, MouseButton::Left) => {
            chrome.tab_scroll = chrome.tab_scroll.saturating_add(1);
            chrome.tab_scroll_follow_active = false;
            MouseOutcome::Handled
        }
        (Hit::NewTab, MouseButton::Left) => MouseOutcome::Spawn {
            placement: Placement::Tab,
        },
        _ => MouseOutcome::Ignore,
    }
}

/// The pointer moved with a button held. A tab drag that has travelled
/// `TAB_DRAG_THRESHOLD` columns from its press becomes a move; every other
/// gesture waits for its section.
pub(super) fn drag(chrome: &mut Chrome, column: u16) -> MouseOutcome {
    match &mut chrome.gesture {
        Some(MouseGesture::TabDrag {
            origin_col, moved, ..
        }) => {
            if column.abs_diff(*origin_col) >= TAB_DRAG_THRESHOLD {
                *moved = true;
            }
            MouseOutcome::Handled
        }
        _ => MouseOutcome::Ignore,
    }
}

/// A button came up; `released` is the gesture the press started, already
/// taken off the chrome. A moved tab dropped on another tab takes that tab's
/// place (herdr `MoveTab`) and stays active; released anywhere else it stays
/// put, and a release without movement was the click the press handled.
pub(super) fn up(chrome: &mut Chrome, hit: Hit, released: Option<MouseGesture>) -> MouseOutcome {
    match released {
        Some(MouseGesture::TabDrag {
            index, moved: true, ..
        }) => {
            if let Hit::Tab(target) = hit {
                let count = chrome.tabs.len();
                if target != index && index < count && target < count {
                    move_tab(&mut chrome.tabs, index, target);
                    chrome.active_tab = target;
                }
            }
            MouseOutcome::Handled
        }
        Some(MouseGesture::TabDrag { .. }) => MouseOutcome::Handled,
        _ => MouseOutcome::Ignore,
    }
}

/// Move `tabs[from]` to `to`, sliding the tabs between them one step so every
/// other tab keeps its order (herdr's remove-and-insert `move_tab`).
fn move_tab(tabs: &mut [Tab], from: usize, to: usize) {
    if from < to {
        for index in from..to {
            tabs.swap(index, index + 1);
        }
    } else {
        for index in (to..from).rev() {
            tabs.swap(index, index + 1);
        }
    }
}

/// Whether `pane` still backs a roster terminal: the same set the keyboard
/// cycle (`focus_relative_live_pane`) focuses through.
fn on_roster<W: WorkspaceView>(ws: &W, pane: PaneId) -> bool {
    ws.roster_terminal_ids()
        .iter()
        .any(|terminal_id| ws.pane_for_terminal(terminal_id) == Some(pane))
}
