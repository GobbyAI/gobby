//! Mouse dispatch shared by the live and scripted loops.
//!
//! `route_mouse` classifies one `RawInputEvent::Mouse` against the hit map the
//! last draw wrote into `ViewState` and says what the loop should do. Nothing
//! here reaches the daemon: the loop applies the outcome, so the lease model
//! stays in `control.rs` and this module stays pure.

use crossterm::event::{MouseEvent, MouseEventKind};

use crate::ui::hit::hit_test;
use crate::ui::{Chrome, Mode};

use super::super::PaneId;

mod pointer;

/// What the loop does with a mouse event.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(in crate::app) enum MouseOutcome {
    /// Consumed by chrome; nothing else sees it.
    Handled,
    /// Focus `pane`; `observe_only` (alt+click) moves focus without taking
    /// control, the gobby lease escape.
    Focus { pane: PaneId, observe_only: bool },
    /// Not ours: later routers (copy-mode selection) may still claim it.
    Ignore,
}

/// Route one mouse event against the last drawn chrome.
///
/// Runs before every key router. With capture off the guard never arms the
/// terminal, so no mouse event should arrive; one that does is ignored rather
/// than routed. Modal modes own the whole screen while they are up, and copy
/// mode leaves the mouse to the selection router.
pub(in crate::app) fn route_mouse(chrome: &Chrome, mouse: &MouseEvent) -> MouseOutcome {
    if !chrome.prefs.mouse_capture {
        return MouseOutcome::Ignore;
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
        MouseEventKind::Down(button) => pointer::down(chrome, hit, button, mouse.modifiers),
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
    /// Returns the chrome, the focused pane and a cell inside each pane's
    /// content: `(focused_cell, other_pane, other_cell)`.
    fn split_chrome() -> (Chrome, (u16, u16), PaneId, (u16, u16)) {
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
        (chrome, focused_cell, other, other_cell)
    }

    fn down(column: u16, row: u16, modifiers: KeyModifiers) -> MouseEvent {
        MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column,
            row,
            modifiers,
        }
    }

    #[test]
    fn route_mouse_ignores_when_capture_is_off() {
        let (mut chrome, _, other, (col, row)) = split_chrome();
        assert_eq!(
            route_mouse(&chrome, &down(col, row, KeyModifiers::NONE)),
            MouseOutcome::Focus {
                pane: other,
                observe_only: false
            },
            "with capture on the same click focuses the other pane"
        );
        chrome.prefs.mouse_capture = false;
        assert_eq!(
            route_mouse(&chrome, &down(col, row, KeyModifiers::NONE)),
            MouseOutcome::Ignore
        );
    }

    #[test]
    fn route_mouse_focuses_the_other_pane_and_alt_observes() {
        let (mut chrome, (fcol, frow), other, (col, row)) = split_chrome();
        assert_eq!(
            route_mouse(&chrome, &down(fcol, frow, KeyModifiers::NONE)),
            MouseOutcome::Ignore,
            "a click on the focused pane is left for selection"
        );
        assert_eq!(
            route_mouse(&chrome, &down(col, row, KeyModifiers::ALT)),
            MouseOutcome::Focus {
                pane: other,
                observe_only: true
            }
        );
        chrome.mode = Mode::Settings;
        assert_eq!(
            route_mouse(&chrome, &down(col, row, KeyModifiers::NONE)),
            MouseOutcome::Handled,
            "a modal owns the screen"
        );
        chrome.mode = Mode::Copy;
        assert_eq!(
            route_mouse(&chrome, &down(col, row, KeyModifiers::NONE)),
            MouseOutcome::Ignore,
            "copy mode leaves the mouse to the selection router"
        );
    }
}
