//! Wheel notches: what a scroll over each chrome region means.

use crate::ui::hit::Hit;
use crate::ui::Chrome;

use super::{focus_active_tab, MouseOutcome};

/// A wheel notch over `hit`; `up` is a notch away from the user.
///
/// Over the tab bar a notch switches tabs, up = previous and down = next,
/// wrapping at either end (herdr's tab-bar wheel), and focuses the new tab's
/// pane the way a click would. Panes and the sidebar wait for their sections.
pub(super) fn wheel(chrome: &mut Chrome, hit: Hit, up: bool) -> MouseOutcome {
    match hit {
        Hit::Tab(_) | Hit::TabScrollLeft | Hit::TabScrollRight | Hit::NewTab | Hit::TabBarEmpty => {
            let count = chrome.tabs.len();
            if count == 0 {
                return MouseOutcome::Handled;
            }
            let step = if up { count - 1 } else { 1 };
            chrome.active_tab = (chrome.active_tab + step) % count;
            chrome.tab_scroll_follow_active = true;
            focus_active_tab(chrome, false)
        }
        _ => MouseOutcome::Ignore,
    }
}
