//! Mouse dispatch shared by the live and scripted loops.
//!
//! `route_mouse` classifies one `RawInputEvent::Mouse` against the hit map the
//! last draw wrote into `ViewState` and says what the loop should do. Nothing
//! here reaches the daemon: the loop applies the outcome, so the lease model
//! stays in `control.rs` and this module stays pure. Gesture state lives on
//! `Chrome::gesture`: a press may start one, drags feed it, and a release
//! always ends it, handing it to `pointer::up` to finish.

use crossterm::event::{MouseEvent, MouseEventKind};
use gobby_terminal::layout;

use crate::ui::chrome::Tab;
use crate::ui::hit::{hit_test, SidebarSection};
use crate::ui::{Action, Chrome, Mode, WorkspaceView};

use super::super::PaneId;

mod pointer;
mod wheel;

/// A press-and-drag in progress, keyed by what went down under the pointer.
///
/// Each variant is started by the section that owns its surface: the tab bar,
/// the sidebar and its scrollbar, split borders and pane scrollbars, selection
/// and button forwarding. Every gesture ends on release.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MouseGesture {
    /// A tab is being dragged; `moved` flips once it travels past the threshold.
    TabDrag {
        index: usize,
        origin_col: u16,
        moved: bool,
    },
    /// A roster row is being dragged to reorder.
    RosterDrag {
        terminal_id: String,
        origin_row: u16,
        moved: bool,
    },
    /// A split border is being dragged to resize its panes.
    SplitDrag { border: usize },
    /// The sidebar edge is being dragged to resize the sidebar.
    SidebarDrag,
    /// The roster/attention divider is being dragged.
    SectionDrag,
    /// A sidebar section's scrollbar thumb is being dragged.
    SidebarScrollbarDrag {
        section: SidebarSection,
        grab_offset: u16,
    },
    /// A pane scrollbar thumb is being dragged.
    ScrollbarDrag {
        slot: layout::PaneId,
        grab_offset: u16,
    },
    /// A selection is being extended inside a pane.
    Select { slot: layout::PaneId },
    /// A button is held inside a pane that reports mouse; motion is forwarded.
    Forwarding { slot: layout::PaneId },
}

/// Where a mouse-spawned terminal lands.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Placement {
    Tab,
    SplitRight,
    SplitDown,
}

/// What the loop does with a mouse event.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MouseOutcome {
    /// Consumed by chrome; nothing else sees it.
    Handled,
    /// Focus `pane`; `observe_only` (alt+click) moves focus without taking
    /// control, the gobby lease escape.
    Focus { pane: PaneId, observe_only: bool },
    /// A keymap action, dispatched exactly as its chord would be.
    Action(Action),
    /// Spawn a terminal and place it.
    Spawn { placement: Placement },
    /// Bytes for a pane: a forwarded SGR report where it reports mouse, or
    /// the arrow keys a wheel notch means on an alternate screen.
    Write { pane: PaneId, bytes: Vec<u8> },
    /// Scroll `pane`'s viewport to `rows` above the live edge; the loop sends
    /// `SetScrollOffset` on the pane's frame source.
    Scroll { pane: PaneId, rows: u32 },
    /// An attention row was clicked: focus `pane` when the entry maps to one,
    /// then open the response dialog for `entry_id`.
    Attention {
        pane: Option<PaneId>,
        entry_id: String,
    },
    /// A roster row was dropped on another: the roster in its new order.
    Reorder { order: Vec<String> },
    /// Not ours: later routers (copy-mode selection) may still claim it.
    Ignore,
}

/// Columns a pressed tab travels before its drag becomes a move. herdr moves
/// on 1; one more keeps a click with a hair of jitter a click.
pub const TAB_DRAG_THRESHOLD: u16 = 2;

/// Rows a pressed roster row travels before its drag becomes a reorder
/// (herdr `WORKSPACE_DRAG_THRESHOLD`).
pub const ROSTER_DRAG_THRESHOLD: u16 = 1;

/// Rows one wheel notch moves a sidebar list (herdr `scroll_workspace_list`).
pub const MOUSE_SCROLL_LINES: usize = 3;

/// Route one mouse event against the last drawn chrome.
///
/// Runs before every key router. With capture off the guard never arms the
/// terminal, so no mouse event should arrive; one that does is ignored rather
/// than routed. A release ends whatever gesture the press started, whichever
/// mode owns the screen; what the release itself does belongs to the
/// gesture's own surface (`pointer::up`). Modal modes own the whole screen
/// while they are up, and copy mode leaves the mouse to the selection router.
pub fn route_mouse<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    mouse: &MouseEvent,
) -> MouseOutcome {
    if !chrome.prefs.mouse_capture {
        return MouseOutcome::Ignore;
    }
    let released = matches!(mouse.kind, MouseEventKind::Up(_))
        .then(|| chrome.gesture.take())
        .flatten();
    match chrome.mode {
        Mode::Copy => return MouseOutcome::Ignore,
        Mode::ConfirmClose
        | Mode::Rename
        | Mode::Respond
        | Mode::Settings
        | Mode::KeybindHelp
        | Mode::Navigator => return MouseOutcome::Handled,
        Mode::Terminal | Mode::Navigate | Mode::Prefix | Mode::Resize => {}
    }
    let hit = hit_test(&chrome.view, mouse.column, mouse.row);
    match mouse.kind {
        MouseEventKind::Down(button) => pointer::down(ws, chrome, hit, button, mouse),
        MouseEventKind::Drag(_) => pointer::drag(ws, chrome, mouse),
        MouseEventKind::Up(_) => pointer::up(ws, chrome, hit, released),
        MouseEventKind::ScrollUp => wheel::wheel(ws, chrome, hit, mouse.row, true),
        MouseEventKind::ScrollDown => wheel::wheel(ws, chrome, hit, mouse.row, false),
        _ => MouseOutcome::Ignore,
    }
}

/// Focus the active tab's focused pane. A tab whose slots have all gone is
/// still activated; there is just nothing to focus.
fn focus_active_tab(chrome: &Chrome, observe_only: bool) -> MouseOutcome {
    match chrome.active_tab().and_then(Tab::focused_pane) {
        Some(pane) => MouseOutcome::Focus { pane, observe_only },
        None => MouseOutcome::Handled,
    }
}

/// Whether `pane` still backs a roster terminal: the same set the keyboard
/// cycle (`focus_relative_live_pane`) focuses through. A slot whose pane has
/// left the roster is stale until the next chrome sync, and `ws.pane` on it
/// would panic.
pub(super) fn on_roster<W: WorkspaceView>(ws: &W, pane: PaneId) -> bool {
    ws.roster_terminal_ids()
        .iter()
        .any(|terminal_id| ws.pane_for_terminal(terminal_id) == Some(pane))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::Workspace;
    use crossterm::event::{KeyModifiers, MouseButton};
    use ratatui::layout::Rect;

    /// Two panes split in one tab, drawn once so the hit map is populated.
    /// Returns the workspace, the chrome, the focused pane and a cell inside
    /// each pane's content: `(ws, chrome, focused_cell, other_pane, other_cell)`.
    fn split_chrome() -> (Workspace, Chrome, (u16, u16), PaneId, (u16, u16)) {
        let mut ws = Workspace::scripted();
        let alpha = ws
            .open_terminal("term-alpha", "native", "epoch")
            .expect("open term-alpha");
        let beta = ws
            .open_terminal("term-beta", "native", "epoch")
            .expect("open term-beta");
        let mut chrome = Chrome::dark();
        chrome.open_pane(alpha, "alpha");
        chrome.open_pane(beta, "beta");
        chrome.compute_view(&ws, Rect::new(0, 0, 100, 30));
        let focused = chrome.focused_pane().expect("focused pane");
        let cell_of = |pane: PaneId| {
            let info = chrome
                .view
                .pane_infos
                .iter()
                .find(|info| chrome.pane_for_slot(info.id) == Some(pane))
                .expect("pane drawn");
            (info.inner_rect.x + 1, info.inner_rect.y + 1)
        };
        let other = if focused == alpha { beta } else { alpha };
        let focused_cell = cell_of(focused);
        let other_cell = cell_of(other);
        (ws, chrome, focused_cell, other, other_cell)
    }

    fn event(kind: MouseEventKind, column: u16, row: u16, modifiers: KeyModifiers) -> MouseEvent {
        MouseEvent {
            kind,
            column,
            row,
            modifiers,
        }
    }

    fn down(column: u16, row: u16, modifiers: KeyModifiers) -> MouseEvent {
        event(
            MouseEventKind::Down(MouseButton::Left),
            column,
            row,
            modifiers,
        )
    }

    #[test]
    fn route_mouse_ignores_when_capture_is_off() {
        let (ws, mut chrome, _, other, (col, row)) = split_chrome();
        assert_eq!(
            route_mouse(&ws, &mut chrome, &down(col, row, KeyModifiers::NONE)),
            MouseOutcome::Focus {
                pane: other,
                observe_only: false
            },
            "with capture on the same click focuses the other pane"
        );
        chrome.prefs.mouse_capture = false;
        assert_eq!(
            route_mouse(&ws, &mut chrome, &down(col, row, KeyModifiers::NONE)),
            MouseOutcome::Ignore
        );
    }

    #[test]
    fn route_mouse_focuses_the_other_pane_and_alt_observes() {
        let (ws, mut chrome, (fcol, frow), other, (col, row)) = split_chrome();
        assert_eq!(
            route_mouse(&ws, &mut chrome, &down(fcol, frow, KeyModifiers::NONE)),
            MouseOutcome::Ignore,
            "a click on the focused pane is left for selection"
        );
        assert_eq!(
            route_mouse(&ws, &mut chrome, &down(col, row, KeyModifiers::ALT)),
            MouseOutcome::Focus {
                pane: other,
                observe_only: true
            }
        );
        chrome.mode = Mode::Settings;
        assert_eq!(
            route_mouse(&ws, &mut chrome, &down(col, row, KeyModifiers::NONE)),
            MouseOutcome::Handled,
            "a modal owns the screen"
        );
        chrome.mode = Mode::Copy;
        assert_eq!(
            route_mouse(&ws, &mut chrome, &down(col, row, KeyModifiers::NONE)),
            MouseOutcome::Ignore,
            "copy mode leaves the mouse to the selection router"
        );
    }

    #[test]
    fn route_mouse_ignores_a_slot_whose_pane_left_the_roster() {
        let (_, mut chrome, _, _, (col, row)) = split_chrome();
        let empty = Workspace::scripted();
        assert_eq!(
            route_mouse(&empty, &mut chrome, &down(col, row, KeyModifiers::NONE)),
            MouseOutcome::Ignore,
            "a stale slot is not focused before the next chrome sync"
        );
    }

    #[test]
    fn route_mouse_release_ends_the_gesture() {
        let (ws, mut chrome, _, _, (col, row)) = split_chrome();
        chrome.gesture = Some(MouseGesture::SidebarDrag);
        let drag = event(
            MouseEventKind::Drag(MouseButton::Left),
            col,
            row,
            KeyModifiers::NONE,
        );
        assert_eq!(route_mouse(&ws, &mut chrome, &drag), MouseOutcome::Handled);
        assert_eq!(
            chrome.gesture,
            Some(MouseGesture::SidebarDrag),
            "a drag keeps the gesture alive"
        );
        let up = event(
            MouseEventKind::Up(MouseButton::Left),
            col,
            row,
            KeyModifiers::NONE,
        );
        assert_eq!(route_mouse(&ws, &mut chrome, &up), MouseOutcome::Handled);
        assert_eq!(chrome.gesture, None, "a release ends the gesture");
        chrome.gesture = Some(MouseGesture::SidebarDrag);
        chrome.mode = Mode::KeybindHelp;
        assert_eq!(route_mouse(&ws, &mut chrome, &up), MouseOutcome::Handled);
        assert_eq!(
            chrome.gesture, None,
            "a release under a modal still ends it"
        );
    }
}
