//! Button presses: what a Down on each chrome region means.

use crossterm::event::{KeyModifiers, MouseButton};

use crate::ui::hit::Hit;
use crate::ui::Chrome;

use super::MouseOutcome;

/// A button went down on `hit`.
///
/// Left Down inside a pane focuses it (herdr `FocusPane`); alt makes that an
/// observe-only focus. A click on the pane that already has focus is left
/// alone so selection can start there. Every other region is ignored until
/// its section lands.
pub(super) fn down(
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
            if chrome.focused_pane() == Some(pane) {
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
