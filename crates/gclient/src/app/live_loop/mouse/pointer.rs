//! Button presses, drags and releases: what each means on a chrome region.

use crossterm::event::{KeyModifiers, MouseButton, MouseEvent};
use gobby_terminal::layout::{self, ScrollMetrics};
use ratatui::layout::{Direction, Rect};

use crate::app::PaneId;
use crate::ui::chrome::{attention_pane, Tab};
use crate::ui::hit::{Hit, SidebarSection};
use crate::ui::pane_layout::metrics_for;
use crate::ui::scrollbar::{
    scrollbar_offset_from_drag_row, scrollbar_offset_from_row, scrollbar_thumb_grab_offset,
};
use crate::ui::sidebar::section_metrics;
use crate::ui::{Chrome, WorkspaceView};

use super::{
    focus_active_tab, on_roster, MouseGesture, MouseOutcome, Placement, ROSTER_DRAG_THRESHOLD,
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
/// starts a thumb drag and the track beside it jumps the list there.
///
/// A split border starts the drag that resizes the panes either side of it
/// (herdr `SetSplitRatio`); a pane scrollbar thumb starts a thumb drag and
/// the track beside it jumps the scrollback there. Every other region is
/// ignored until its section lands.
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
        Hit::SplitBorder(border) => {
            chrome.gesture = Some(MouseGesture::SplitDrag { border });
            MouseOutcome::Handled
        }
        Hit::PaneScrollbar { slot, row } => {
            let Some((pane, track, metrics)) = pane_scrollbar(ws, chrome, slot) else {
                return MouseOutcome::Ignore;
            };
            match scrollbar_thumb_grab_offset(metrics, track, row) {
                Some(grab_offset) => {
                    chrome.gesture = Some(MouseGesture::ScrollbarDrag { slot, grab_offset });
                    MouseOutcome::Handled
                }
                None => scroll_to(
                    pane,
                    metrics,
                    scrollbar_offset_from_row(metrics, track, row),
                ),
            }
        }
        _ => MouseOutcome::Ignore,
    }
}

/// The pointer moved with a button held. A tab drag that has travelled
/// `TAB_DRAG_THRESHOLD` columns from its press becomes a move, a roster drag
/// `ROSTER_DRAG_THRESHOLD` rows; the sidebar edge and section rule follow
/// the pointer; a scrollbar thumb, sidebar or pane, keeps the row it was
/// grabbed by under the pointer; a split border follows the pointer along
/// its split, the first pane taking the share of the split's area the
/// pointer sits at, clamped to `0.1..=0.9` (herdr `SetSplitRatio`). Every
/// other gesture waits for its section.
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
        Some(MouseGesture::SplitDrag { border }) => {
            let Some(split) = chrome.view.split_borders.get(*border) else {
                return MouseOutcome::Handled;
            };
            let ratio = match split.direction {
                Direction::Horizontal => {
                    f32::from(mouse.column.saturating_sub(split.area.x))
                        / f32::from(split.area.width.max(1))
                }
                Direction::Vertical => {
                    f32::from(mouse.row.saturating_sub(split.area.y))
                        / f32::from(split.area.height.max(1))
                }
            };
            let path = split.path.clone();
            if let Some(tab) = chrome.active_tab_mut() {
                tab.layout.set_ratio_at(&path, ratio.clamp(0.1, 0.9));
            }
            MouseOutcome::Handled
        }
        Some(MouseGesture::ScrollbarDrag { slot, grab_offset }) => {
            let (slot, grab_offset) = (*slot, *grab_offset);
            let Some((pane, track, metrics)) = pane_scrollbar(ws, chrome, slot) else {
                return MouseOutcome::Handled;
            };
            let rows = scrollbar_offset_from_drag_row(metrics, track, mouse.row, grab_offset);
            scroll_to(pane, metrics, rows)
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
/// keeps the width it reached as the preferred width; a split border or
/// scrollbar release keeps what the drag reached.
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
            | MouseGesture::SidebarScrollbarDrag { .. }
            | MouseGesture::SplitDrag { .. }
            | MouseGesture::ScrollbarDrag { .. },
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

/// The scrollbar lane the last frame drew beside `slot`, with the pane it
/// scrolls and where that pane's scrollback stood when the lane was drawn.
/// None when the lane was not drawn or the slot no longer shows a roster
/// pane.
fn pane_scrollbar<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    slot: layout::PaneId,
) -> Option<(PaneId, Rect, ScrollMetrics)> {
    let info = chrome.view.pane_infos.iter().find(|info| info.id == slot)?;
    let track = info.scrollbar_rect?;
    let pane = chrome
        .pane_for_slot(slot)
        .filter(|pane| on_roster(ws, *pane))?;
    let state = ws.pane(pane);
    let metrics = metrics_for(
        state.scroll_offset,
        state.max_scroll,
        info.inner_rect.height,
    );
    Some((pane, track, metrics))
}

/// Scroll `pane` to `rows` above the live edge, or consume the event when it
/// is already there.
fn scroll_to(pane: PaneId, metrics: ScrollMetrics, rows: usize) -> MouseOutcome {
    if rows == metrics.offset_from_bottom {
        MouseOutcome::Handled
    } else {
        MouseOutcome::Scroll {
            pane,
            rows: rows as u32,
        }
    }
}
