//! Button presses, drags and releases: what each means on a chrome region.

use crossterm::event::{KeyModifiers, MouseButton, MouseEvent};
use gobby_terminal::layout::{self, Node, ScrollMetrics};
use ratatui::layout::{Direction, Rect};

use crate::app::PaneId;
use crate::ui::chrome::Tab;
use crate::ui::hit::{Hit, SidebarSection};
use crate::ui::menu_bar::MenuBarMenu;
use crate::ui::pane_layout::metrics_for;
use crate::ui::scrollbar::{
    scrollbar_offset_from_drag_row, scrollbar_offset_from_row, scrollbar_thumb_grab_offset,
};
use crate::ui::sidebar::{section_metrics, ALL_MACHINES};
use crate::ui::sidebar_rows::displayed_project_ids;
use crate::ui::{Action, Chrome, WorkspaceView};

use super::super::menu::{open_menu, ContextMenuKind};
use super::super::modal_input::persist_prefs;
use super::{
    focus_active_tab, forward, links, on_roster, select, MouseGesture, MouseOutcome, Placement,
    PROJECT_DRAG_THRESHOLD, TAB_DRAG_THRESHOLD,
};

/// A button went down on `hit`. Outside a pane the right button opens the
/// tab menu on a tab, the row menus on a project card, a worktree row and an
/// agent row, and the global menu on empty chrome (the bare menu bar, the
/// bare tab bar, the sidebar's empty rows, the empty state); only the left
/// button means anything else.
///
/// On the menu bar a title opens its menu under the title's cell.
///
/// Inside a pane the right button is `right_down`'s. A ctrl+left press first
/// asks `links::resolve` for a URL under the pointer and opens that instead,
/// whichever pane has focus. Then a pane whose app tracks the mouse gets the
/// press (`forward::press`), after being focused when it was not, unless
/// shift is held (herdr `shift_bypasses_mouse_reporting`) or alt asks for an
/// observe-only focus of another pane, which never writes. Otherwise a left
/// press focuses the pane (herdr `FocusPane`), alt making that observe-only,
/// or starts a selection on the pane that already has focus
/// (`select::down`). A slot whose pane has left the roster is stale until
/// the next chrome sync, so it is ignored.
///
/// On the tab bar it activates the tab and focuses its pane (herdr
/// `FocusTab`), starting the drag that `up` may finish as a reorder; a
/// scroll arrow moves the bar one tab and stops it following the active tab;
/// the new-tab button asks for a spawn placed in a fresh tab.
///
/// In the sidebar a project card focuses its project (herdr
/// `FocusWorkspace`) and starts the drag `up` may finish as a reorder; a
/// worktree row asks for a shell in that worktree; a card's group toggle
/// folds its worktree rows; an agent row reveals its terminal and asks for
/// that entry's prompt; the edge starts its resize drag and applies the
/// press at once (herdr resizes on the press too); a scrollbar thumb starts
/// a thumb drag and the track beside it jumps the list there.
///
/// A split border starts the drag that resizes the panes either side of it
/// (herdr `SetSplitRatio`); a pane scrollbar thumb starts a thumb drag and
/// the track beside it jumps the scrollback there.
///
/// The focused pane's metadata is a button while it reads Read-only or
/// Uncertain, on its edge or leading the status line: a press accepts a
/// pending take-back, and otherwise takes control again. Focus alone is a
/// condition, not a button. Every other region is ignored until its section
/// lands.
pub(super) fn down<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    hit: Hit,
    button: MouseButton,
    mouse: &MouseEvent,
) -> MouseOutcome {
    let observe_only = mouse.modifiers.contains(KeyModifiers::ALT);
    let sidebar_area = chrome.view.sidebar_rect;
    match hit {
        Hit::Pane { slot, col, row } => {
            let Some(pane) = chrome.pane_for_slot(slot) else {
                return MouseOutcome::Ignore;
            };
            if !on_roster(ws, pane) {
                return MouseOutcome::Ignore;
            }
            if button == MouseButton::Right {
                return right_down(ws, chrome, pane, slot, mouse, (col, row));
            }
            if button == MouseButton::Left && mouse.modifiers.contains(KeyModifiers::CONTROL) {
                if let Some(url) = links::resolve(ws, pane, row, col) {
                    return MouseOutcome::OpenLink(url);
                }
            }
            let focused = chrome.focused_pane() == Some(pane);
            if !mouse.modifiers.contains(KeyModifiers::SHIFT) && (focused || !observe_only) {
                let strip = KeyModifiers::empty();
                if let Some(outcome) =
                    forward::press(ws, chrome, pane, slot, mouse, strip, (col, row))
                {
                    return outcome;
                }
            }
            if button != MouseButton::Left {
                return MouseOutcome::Ignore;
            }
            if observe_only && !focused {
                return MouseOutcome::Focus {
                    pane,
                    observe_only: true,
                };
            }
            let selection = select::down(ws, chrome, pane, slot, col, row, mouse);
            if focused || !matches!(selection, MouseOutcome::Handled) {
                return selection;
            }
            // Focus on press so a drag selects in the clicked pane, but wait
            // for release before taking its input lease.
            MouseOutcome::Focus {
                pane,
                observe_only: true,
            }
        }
        Hit::Tab(index) if button == MouseButton::Right => {
            open_menu(
                ws,
                chrome,
                ContextMenuKind::Tab(index),
                (mouse.column, mouse.row),
            );
            MouseOutcome::Handled
        }
        Hit::MenuTitle(index) if button == MouseButton::Left => {
            let cell = chrome
                .view
                .menu_title_hit_areas
                .iter()
                .find(|(drawn, _)| *drawn == index)
                .map(|(_, cell)| *cell);
            let (Some(cell), Some(menu)) = (cell, MenuBarMenu::ALL.get(index)) else {
                return MouseOutcome::Ignore;
            };
            open_menu(
                ws,
                chrome,
                ContextMenuKind::MenuBar(*menu),
                (cell.x, cell.bottom()),
            );
            MouseOutcome::Handled
        }
        Hit::MenuBarEmpty | Hit::TabBarEmpty | Hit::Empty | Hit::SidebarEmpty
            if button == MouseButton::Right =>
        {
            open_menu(
                ws,
                chrome,
                ContextMenuKind::Global,
                (mouse.column, mouse.row),
            );
            MouseOutcome::Handled
        }
        Hit::Project(project_id) if button == MouseButton::Right => {
            open_menu(
                ws,
                chrome,
                ContextMenuKind::Project(project_id),
                (mouse.column, mouse.row),
            );
            MouseOutcome::Handled
        }
        Hit::Worktree(worktree_id) if button == MouseButton::Right => {
            open_menu(
                ws,
                chrome,
                ContextMenuKind::Worktree(worktree_id),
                (mouse.column, mouse.row),
            );
            MouseOutcome::Handled
        }
        Hit::Agent(entry_id) if button == MouseButton::Right => {
            open_menu(
                ws,
                chrome,
                ContextMenuKind::Agent(entry_id),
                (mouse.column, mouse.row),
            );
            MouseOutcome::Handled
        }
        _ if button != MouseButton::Left => MouseOutcome::Ignore,
        Hit::Tab(index) => {
            chrome.activate_tab(index);
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
        Hit::Project(project_id) => {
            // The tab set swaps at once so the frame after the press shows
            // the project's tabs; the live loop's focus swap is idempotent.
            chrome.focus_project(&project_id);
            chrome.gesture = Some(MouseGesture::ProjectDrag {
                project_id: project_id.clone(),
                origin_row: mouse.row,
                moved: false,
            });
            MouseOutcome::FocusProject(project_id)
        }
        Hit::Worktree(worktree_id) => MouseOutcome::OpenWorktree(worktree_id),
        Hit::GroupToggle(project_id) => {
            chrome.sidebar.toggle_group(&project_id);
            MouseOutcome::Handled
        }
        Hit::Machine(machine) => {
            // The row's filter: this machine alone, or every machine from
            // the local row; a second click returns to local.
            let is_local = machine == ws.sidebar().local_machine;
            let target = if is_local {
                ALL_MACHINES
            } else {
                machine.as_str()
            };
            chrome.sidebar.machine_filter = match chrome.sidebar.machine_filter.as_deref() {
                Some(current) if current == target => None,
                _ => Some(target.to_string()),
            };
            MouseOutcome::Handled
        }
        Hit::ProjectsFilter => MouseOutcome::Action(Action::ToggleProjectsFilter),
        // The band has room for one control, so both session axes live in
        // its menu.
        Hit::SessionsView => {
            open_menu(
                ws,
                chrome,
                ContextMenuKind::SessionsView,
                (mouse.column, mouse.row),
            );
            MouseOutcome::Handled
        }
        Hit::Agent(entry_id) => {
            // A blocked row only reveals its terminal, which already shows the
            // question; `respond` stays on prefix+a and the row menu.
            MouseOutcome::FocusAgent(entry_id)
        }
        Hit::SidebarDivider => {
            chrome.gesture = Some(MouseGesture::SidebarDrag);
            chrome
                .sidebar
                .set_width_from_column(sidebar_area, mouse.column);
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
        Hit::ControlIndicator => {
            let Some(pane) = chrome.focused_pane().filter(|pane| on_roster(ws, *pane)) else {
                return MouseOutcome::Ignore;
            };
            // The indicator is drawn only for Read-only and Uncertain, so a
            // press always asks for control back.
            MouseOutcome::Action(if ws.pane(pane).take_back {
                Action::TakeBack
            } else {
                Action::TakeControl
            })
        }
        // A toast is a control: a left click does what its key does, which is
        // open the row the alert named and clear the stack either way. The
        // other buttons keep whatever they meant over the pane underneath.
        Hit::Toast if button == MouseButton::Left => {
            MouseOutcome::Action(Action::OpenNotificationTarget)
        }
        _ => MouseOutcome::Ignore,
    }
}

/// The right button went down at pane cell `cell` of `pane`. It passes
/// through to the pane's app when the configured passthrough modifier is
/// held, exactly, or the pane's own flag is set and no modifier is held
/// (herdr `handle_right_click_passthrough`); the modifier is hidden from the
/// app, and a pane whose app does not track the mouse gets nothing. Any other
/// right-click opens the pane's context menu at the pointer.
fn right_down<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    pane: PaneId,
    slot: layout::PaneId,
    mouse: &MouseEvent,
    cell: (u16, u16),
) -> MouseOutcome {
    let configured = chrome
        .prefs
        .right_click_passthrough_modifier
        .key_modifiers()
        .filter(|held| mouse.modifiers == *held);
    let flagged = mouse.modifiers.is_empty() && ws.pane(pane).right_click_passthrough;
    let Some(strip) = configured.or_else(|| flagged.then(KeyModifiers::empty)) else {
        open_menu(
            ws,
            chrome,
            ContextMenuKind::Pane(pane),
            (mouse.column, mouse.row),
        );
        return MouseOutcome::Handled;
    };
    forward::press(ws, chrome, pane, slot, mouse, strip, cell).unwrap_or(MouseOutcome::Handled)
}

/// The pointer moved with a button held. A tab drag that has travelled
/// `TAB_DRAG_THRESHOLD` columns from its press becomes a move, a project
/// drag `PROJECT_DRAG_THRESHOLD` rows; the sidebar edge follows the pointer;
/// a scrollbar thumb, sidebar or pane, keeps the row it was
/// grabbed by under the pointer; a split border follows the pointer along
/// its split, the first pane taking the share of the split's area the
/// pointer sits at, clamped to `0.1..=0.9` (herdr `SetSplitRatio`); a
/// selection extends to the pointer; a captured button reports the drag to
/// the pane holding it (`forward::captured`), which also takes the release.
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
        Some(MouseGesture::ProjectDrag {
            origin_row, moved, ..
        }) => {
            if mouse.row.abs_diff(*origin_row) >= PROJECT_DRAG_THRESHOLD {
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
        Some(MouseGesture::Select { slot }) => {
            let slot = *slot;
            select::drag(ws, chrome, slot, mouse)
        }
        Some(MouseGesture::Forwarding { slot, strip }) => {
            let (slot, strip) = (*slot, *strip);
            forward::captured(ws, chrome, slot, strip, mouse)
        }
        _ => MouseOutcome::Ignore,
    }
}

/// A button came up; `released` is the gesture the press started, already
/// taken off the chrome. A moved tab dropped on another tab takes that tab's
/// place (herdr `MoveTab`) and stays active, and one dropped on the new-tab
/// cells after the last drawn tab takes that tab's place; a moved project
/// card dropped on another card takes its place in the sidebar's project
/// order, which `prefs.toml` keeps; released anywhere else either stays
/// put, and a release without movement was the click the press handled. A
/// sidebar edge release keeps the width it reached as the preferred width; a
/// split border or scrollbar release keeps what the drag reached.
pub(super) fn up<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    hit: Hit,
    released: Option<MouseGesture>,
    mouse: &MouseEvent,
) -> MouseOutcome {
    match released {
        Some(MouseGesture::TabDrag {
            index,
            origin_col,
            moved,
        }) if moved || mouse.column.abs_diff(origin_col) >= TAB_DRAG_THRESHOLD => {
            let target = match hit {
                Hit::Tab(target) => Some(target),
                // The ' + ' cells start at the last drawn tab's right edge,
                // so a drop there lands in that tab's place.
                Hit::NewTab => chrome
                    .view
                    .tab_hit_areas
                    .iter()
                    .rev()
                    .find(|(_, rect)| rect.width > 0)
                    .map(|(index, _)| *index),
                _ => None,
            };
            if let Some(target) = target {
                let tabs = &chrome.tabs().tabs;
                if target != index && index < tabs.len() && target < tabs.len() {
                    if !tabs[index].is_local() {
                        // The daemon orders its tabs: take the position of
                        // the tab now at `target` in the workspace order.
                        let position = ws
                            .workspace_model()
                            .and_then(|model| model.tab(&tabs[target].id))
                            .map_or(target as u32, |row| row.position);
                        return MouseOutcome::MoveTab {
                            tab: tabs[index].id.clone(),
                            position,
                        };
                    }
                    let set = chrome.tabs_mut();
                    move_tab(&mut set.tabs, index, target);
                    chrome.activate_tab(target);
                }
            }
            MouseOutcome::Handled
        }
        Some(MouseGesture::ProjectDrag {
            project_id,
            moved: true,
            ..
        }) => {
            if let Hit::Project(target) = hit {
                if let Some(order) = reordered_projects(ws, chrome, &project_id, &target) {
                    chrome.prefs.project_order = order.clone();
                    chrome.sidebar.project_order = order;
                    persist_prefs(ws.gobby_home(), chrome);
                }
            }
            MouseOutcome::Handled
        }
        Some(MouseGesture::SidebarDrag) => {
            chrome.prefs.sidebar_width = chrome.sidebar.width;
            MouseOutcome::Handled
        }
        Some(MouseGesture::Select { slot }) => {
            let selection = select::up(chrome);
            if selection != MouseOutcome::Handled
                || mouse
                    .modifiers
                    .intersects(KeyModifiers::ALT | KeyModifiers::SHIFT | KeyModifiers::CONTROL)
                || !matches!(hit, Hit::Pane { slot: released_slot, .. } if released_slot == slot)
            {
                return selection;
            }
            let Some(pane) = chrome.pane_for_slot(slot) else {
                return selection;
            };
            let state = ws.pane(pane);
            if state.is_held() || state.take_back {
                selection
            } else {
                MouseOutcome::TakeFreeControl { pane }
            }
        }
        Some(MouseGesture::Forwarding { slot, strip }) => {
            forward::captured(ws, chrome, slot, strip, mouse)
        }
        Some(MouseGesture::SplitDrag { border }) => {
            // The drag moved the layout; a daemon tab now sends the ratio it
            // reached, named through a pane directly under the split.
            let Some(tab) = chrome.active_tab().filter(|tab| !tab.is_local()) else {
                return MouseOutcome::Handled;
            };
            let Some(split) = chrome.view.split_borders.get(border) else {
                return MouseOutcome::Handled;
            };
            let Some(Node::Split {
                ratio,
                first,
                second,
                ..
            }) = tab.layout.node_at(&split.path)
            else {
                return MouseOutcome::Handled;
            };
            match (first.as_ref(), second.as_ref()) {
                (Node::Pane(slot), _) | (_, Node::Pane(slot)) => MouseOutcome::ResizeSplit {
                    slot: *slot,
                    ratio: *ratio,
                },
                _ => MouseOutcome::Handled,
            }
        }
        Some(
            MouseGesture::TabDrag { .. }
            | MouseGesture::ProjectDrag { .. }
            | MouseGesture::SidebarScrollbarDrag { .. }
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

/// The projects as displayed with `moved` dropped where `target` sits, every
/// card between them sliding one step (herdr's remove-and-insert workspace
/// reorder). None when either card has left the sidebar or both are the
/// same card.
fn reordered_projects<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    moved: &str,
    target: &str,
) -> Option<Vec<String>> {
    let mut order = displayed_project_ids(ws, chrome);
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
    chrome.view.sidebar_scrollbar_hit_areas[section.index()]
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
