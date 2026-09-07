//! Mouse dispatch shared by the live and scripted loops.
//!
//! `route_mouse` classifies one `RawInputEvent::Mouse` against the hit map the
//! last draw wrote into `ViewState` and says what the loop should do. Nothing
//! here reaches the daemon: the loop applies the outcome, so the lease model
//! stays in `control.rs` and this module stays pure. Gesture state lives on
//! `Chrome::gesture`: a press may start one, drags feed it, and a release
//! always ends it.

use crossterm::event::{MouseEvent, MouseEventKind};
use gobby_terminal::layout;

use crate::ui::hit::{hit_test, SidebarSection};
use crate::ui::{Action, Chrome, Mode, WorkspaceView};

use super::super::PaneId;

mod pointer;

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
    /// Bytes for a pane that reports mouse: a forwarded SGR report.
    Write { pane: PaneId, bytes: Vec<u8> },
    /// Not ours: later routers (copy-mode selection) may still claim it.
    Ignore,
}

/// Route one mouse event against the last drawn chrome.
///
/// Runs before every key router. With capture off the guard never arms the
/// terminal, so no mouse event should arrive; one that does is ignored rather
/// than routed. A release ends whatever gesture the press started, whichever
/// mode owns the screen; what the release itself does belongs to the
/// gesture's own surface. Modal modes own the whole screen while they are up,
/// and copy mode leaves the mouse to the selection router.
pub fn route_mouse<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    mouse: &MouseEvent,
) -> MouseOutcome {
    if !chrome.prefs.mouse_capture {
        return MouseOutcome::Ignore;
    }
    if let MouseEventKind::Up(_) = mouse.kind {
        chrome.gesture = None;
    }
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
        MouseEventKind::Down(button) => pointer::down(ws, chrome, hit, button, mouse.modifiers),
        _ => MouseOutcome::Ignore,
    }
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
        assert_eq!(route_mouse(&ws, &mut chrome, &drag), MouseOutcome::Ignore);
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
        assert_eq!(route_mouse(&ws, &mut chrome, &up), MouseOutcome::Ignore);
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
