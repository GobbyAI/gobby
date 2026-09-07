//! Button presses, drags and releases: what each means on a chrome region.

use crossterm::event::{KeyModifiers, MouseButton, MouseEvent};
use ratatui::layout::Rect;

use crate::app::PaneId;
use crate::ui::chrome::{attention_pane, Tab};
use crate::ui::hit::{Hit, SidebarSection};
use crate::ui::scrollbar::{
    scrollbar_offset_from_drag_row, scrollbar_offset_from_row, scrollbar_thumb_grab_offset,
};
use crate::ui::sidebar::section_metrics;
use crate::ui::{Chrome, WorkspaceView};

use super::{
    focus_active_tab, MouseGesture, MouseOutcome, Placement, ROSTER_DRAG_THRESHOLD,
    TAB_DRAG_THRESHOLD,
};

/// A button went down on `hit`. Only the left button means anything.
///
/// Inside a pane it focuses the pane (herdr `FocusPane`); alt makes that an
/// observe-only focus. A click on the pane that already has focus is left
/// alone so selection can start there, and a slot whose pane has left the
/// roster is stale until the next chrome sync, so it is not focused either.
///
/// On the tab bar it activates the tab and focuses its pane (herdr
/// `FocusTab`), starting the drag that `up` may finish as a reorder; a
/// scroll arrow moves the bar one tab and stops it following the active tab;
/// the new-tab button asks for a spawn placed in a fresh tab.
///
/// In the sidebar a roster row reveals and focuses its terminal (herdr
/// `FocusWorkspace`) and starts the drag `up` may finish as a reorder; an
/// attention row reveals its terminal and asks for that entry's prompt; the
/// toggle collapses or expands the sidebar; the edge and the section rule
/// start their resize drags and apply the press at once (herdr resizes on the
/// press too), unless the sidebar is collapsed to its rail; a scrollbar thumb
/// starts a thumb drag and the track beside it jumps the list there. Every
/// other region is ignored until its section lands.
pub(super) fn down<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    hit: Hit,
    button: MouseButton,
    mouse: &MouseEvent,
) -> MouseOutcome {
    if button != MouseButton::Left {
        return MouseOutcome::Ignore;
    }
    let observe_only = mouse.modifiers.contains(KeyModifiers::ALT);
    let sidebar_area = chrome.view.sidebar_rect;
    match hit {
        Hit::Pane { slot, .. } => {
            let Some(pane) = chrome.pane_for_slot(slot) else {
                return MouseOutcome::Ignore;
            };
            if chrome.focused_pane() == Some(pane) || !on_roster(ws, pane) {
                return MouseOutcome::Ignore;
            }
            MouseOutcome::Focus { pane, observe_only }
        }
        Hit::Tab(index) => {
            chrome.active_tab = index;
            chrome.tab_scroll_follow_active = true;
            chrome.gesture = Some(MouseGesture::TabDrag {
                index,
                origin_col: mouse.column,
                moved: false,
            });
            focus_active_tab(chrome, observe_only)
        }
        Hit::TabScrollLeft => {
            chrome.tab_scroll = chrome.tab_scroll.saturating_sub(1);
            chrome.tab_scroll_follow_active = false;
            MouseOutcome::Handled
        }
        Hit::TabScrollRight => {
            chrome.tab_scroll = chrome.tab_scroll.saturating_add(1);
            chrome.tab_scroll_follow_active = false;
            MouseOutcome::Handled
        }
        Hit::NewTab => MouseOutcome::Spawn {
            placement: Placement::Tab,
        },
        Hit::Roster(terminal_id) => {
            let Some(pane) = ws.pane_for_terminal(&terminal_id) else {
                return MouseOutcome::Ignore;
            };
            chrome.gesture = Some(MouseGesture::RosterDrag {
                terminal_id,
                origin_row: mouse.row,
                moved: false,
            });
            chrome.reveal_pane(pane, ws.pane(pane).display_name());
            MouseOutcome::Focus { pane, observe_only }
        }
        Hit::Attention(entry_id) => {
            let pane = attention_pane(ws, &entry_id);
            if let Some(pane) = pane {
                chrome.reveal_pane(pane, ws.pane(pane).display_name());
            }
            MouseOutcome::Attention { pane, entry_id }
        }
        Hit::SidebarToggle => {
            chrome.sidebar.collapsed = !chrome.sidebar.collapsed;
            MouseOutcome::Handled
        }
        Hit::SidebarDivider if !chrome.sidebar.collapsed => {
            chrome.gesture = Some(MouseGesture::SidebarDrag);
            chrome
                .sidebar
                .set_width_from_column(sidebar_area, mouse.column);
            MouseOutcome::Handled
        }
        Hit::SidebarSectionDivider if !chrome.sidebar.collapsed => {
            chrome.gesture = Some(MouseGesture::SectionDrag);
            chrome.sidebar.set_split_from_row(sidebar_area, mouse.row);
            MouseOutcome::Handled
        }
        Hit::SidebarScrollbar { section, row } => {
            let Some(track) = scrollbar_track(chrome, section) else {
                return MouseOutcome::Ignore;
            };
            let metrics = section_metrics(ws, chrome, section);
            match scrollbar_thumb_grab_offset(metrics, track, row) {
                Some(grab_offset) => {
                    chrome.gesture = Some(MouseGesture::SidebarScrollbarDrag {
                        section,
                        grab_offset,
                    });
                }
                None => {
                    let offset = scrollbar_offset_from_row(metrics, track, row);
                    *chrome.sidebar.scroll_mut(section) = metrics.max_offset_from_bottom - offset;
                }
            }
            MouseOutcome::Handled
        }
        _ => MouseOutcome::Ignore,
    }
}

/// The pointer moved with a button held. A tab drag that has travelled
/// `TAB_DRAG_THRESHOLD` columns from its press becomes a move, a roster drag
/// `ROSTER_DRAG_THRESHOLD` rows; the sidebar edge and section rule follow
/// the pointer; a scrollbar thumb keeps the row it was grabbed by under the
/// pointer. Every other gesture waits for its section.
pub(super) fn drag<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    mouse: &MouseEvent,
) -> MouseOutcome {
    let sidebar_area = chrome.view.sidebar_rect;
    match &mut chrome.gesture {
        Some(MouseGesture::TabDrag {
            origin_col, moved, ..
        }) => {
            if mouse.column.abs_diff(*origin_col) >= TAB_DRAG_THRESHOLD {
                *moved = true;
            }
            MouseOutcome::Handled
        }
        Some(MouseGesture::RosterDrag {
            origin_row, moved, ..
        }) => {
            if mouse.row.abs_diff(*origin_row) >= ROSTER_DRAG_THRESHOLD {
                *moved = true;
            }
            MouseOutcome::Handled
        }
        Some(MouseGesture::SidebarDrag) => {
            chrome
                .sidebar
                .set_width_from_column(sidebar_area, mouse.column);
            MouseOutcome::Handled
        }
        Some(MouseGesture::SectionDrag) => {
            chrome.sidebar.set_split_from_row(sidebar_area, mouse.row);
            MouseOutcome::Handled
        }
        Some(MouseGesture::SidebarScrollbarDrag {
            section,
            grab_offset,
        }) => {
            let (section, grab_offset) = (*section, *grab_offset);
            let Some(track) = scrollbar_track(chrome, section) else {
                return MouseOutcome::Handled;
            };
            let metrics = section_metrics(ws, chrome, section);
            let offset = scrollbar_offset_from_drag_row(metrics, track, mouse.row, grab_offset);
            *chrome.sidebar.scroll_mut(section) = metrics.max_offset_from_bottom - offset;
            MouseOutcome::Handled
        }
        _ => MouseOutcome::Ignore,
    }
}

/// A button came up; `released` is the gesture the press started, already
/// taken off the chrome. A moved tab dropped on another tab takes that tab's
/// place (herdr `MoveTab`) and stays active; a moved roster row dropped on
/// another row takes its place in the roster, which the loop saves as the
/// pane order; released anywhere else either stays put, and a release
/// without movement was the click the press handled. A sidebar edge release
/// keeps the width it reached as the preferred width.
pub(super) fn up<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    hit: Hit,
    released: Option<MouseGesture>,
) -> MouseOutcome {
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
        Some(MouseGesture::RosterDrag {
            terminal_id,
            moved: true,
            ..
        }) => {
            if let Hit::Roster(target) = hit {
                if let Some(order) = reordered_roster(ws, &terminal_id, &target) {
                    return MouseOutcome::Reorder { order };
                }
            }
            MouseOutcome::Handled
        }
        Some(MouseGesture::SidebarDrag) => {
            chrome.prefs.sidebar_width = chrome.sidebar.width;
            MouseOutcome::Handled
        }
        Some(
            MouseGesture::TabDrag { .. }
            | MouseGesture::RosterDrag { .. }
            | MouseGesture::SectionDrag
            | MouseGesture::SidebarScrollbarDrag { .. },
        ) => MouseOutcome::Handled,
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

/// The roster's pane-backed rows with `moved` dropped where `target` sits,
/// every row between them sliding one step (herdr's remove-and-insert
/// workspace reorder). None when either row has left the roster or both are
/// the same row.
fn reordered_roster<W: WorkspaceView>(ws: &W, moved: &str, target: &str) -> Option<Vec<String>> {
    let mut order: Vec<String> = ws
        .roster_terminal_ids()
        .into_iter()
        .filter(|id| ws.pane_for_terminal(id).is_some())
        .collect();
    let from = order.iter().position(|id| id == moved)?;
    let to = order.iter().position(|id| id == target)?;
    if from == to {
        return None;
    }
    let id = order.remove(from);
    order.insert(to, id);
    Some(order)
}

/// The scrollbar lane the last frame drew beside `section`, if it showed one.
fn scrollbar_track(chrome: &Chrome, section: SidebarSection) -> Option<Rect> {
    match section {
        SidebarSection::Roster => chrome.view.roster_scrollbar_hit_area,
        SidebarSection::Attention => chrome.view.attention_scrollbar_hit_area,
    }
}

/// Whether `pane` still backs a roster terminal: the same set the keyboard
/// cycle (`focus_relative_live_pane`) focuses through.
fn on_roster<W: WorkspaceView>(ws: &W, pane: PaneId) -> bool {
    ws.roster_terminal_ids()
        .iter()
        .any(|terminal_id| ws.pane_for_terminal(terminal_id) == Some(pane))
}
