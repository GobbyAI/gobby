//! Dragging the blank row under a sidebar section to trade rows with the
//! section below it (#24895).

use super::{MouseGesture, MouseOutcome};
use crate::ui::Chrome;

/// Start a drag on the divider under section `index`, recording the pair's
/// rows as the last frame drew them.
pub(super) fn press(chrome: &mut Chrome, index: usize) -> MouseOutcome {
    let [upper, lower] = [
        chrome.view.sidebar_section_rects[index],
        chrome.view.sidebar_section_rects[index + 1],
    ];
    chrome.gesture = Some(MouseGesture::SidebarSectionDrag {
        index,
        top: upper.y,
        rows: [upper.height, lower.height],
    });
    MouseOutcome::Handled
}

/// Move the dragged divider to `row`: the upper section ends on that row, and
/// the pair keeps the sum it had at the press.
pub(super) fn drag(chrome: &mut Chrome, index: usize, top: u16, rows: [u16; 2], row: u16) {
    let wanted = row.saturating_add(1).saturating_sub(top);
    chrome
        .sidebar
        .resize_section_pair(index, rows[0], rows[1], wanted);
}

#[cfg(test)]
#[path = "sidebar_divider/tests.rs"]
mod tests;
