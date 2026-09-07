//! Button presses: what a Down on each chrome region means.

use crossterm::event::{KeyModifiers, MouseButton};

use crate::app::PaneId;
use crate::ui::hit::Hit;
use crate::ui::{Chrome, WorkspaceView};

use super::MouseOutcome;

/// A button went down on `hit`.
///
/// Left Down inside a pane focuses it (herdr `FocusPane`); alt makes that an
/// observe-only focus. A click on the pane that already has focus is left
/// alone so selection can start there, and a slot whose pane has left the
/// roster is stale until the next chrome sync, so it is not focused either.
/// Every other region is ignored until its section lands.
pub(super) fn down<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    hit: Hit,
    button: MouseButton,
    modifiers: KeyModifiers,
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
        _ => MouseOutcome::Ignore,
    }
}

/// Whether `pane` still backs a roster terminal: the same set the keyboard
/// cycle (`focus_relative_live_pane`) focuses through.
fn on_roster<W: WorkspaceView>(ws: &W, pane: PaneId) -> bool {
    ws.roster_terminal_ids()
        .iter()
        .any(|terminal_id| ws.pane_for_terminal(terminal_id) == Some(pane))
}
