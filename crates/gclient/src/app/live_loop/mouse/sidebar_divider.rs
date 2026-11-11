//! Dragging a sidebar section's label band to trade rows with the section
//! below it (#24895).

use super::{MouseGesture, MouseOutcome};
use crate::ui::Chrome;

/// Start a drag on the label band of section `index`, pressed on `row`,
/// recording the pair's rows as the last frame drew them.
pub(super) fn press(chrome: &mut Chrome, index: usize, row: u16) -> MouseOutcome {
    let [upper, lower] = [
        chrome.view.sidebar_section_rects[index],
        chrome.view.sidebar_section_rects[index + 1],
    ];
    chrome.gesture = Some(MouseGesture::SidebarSectionDrag {
        index,
        press_row: row,
        rows: [upper.height, lower.height],
    });
    MouseOutcome::Handled
}

/// Move the dragged boundary by the pointer's travel from the press: the upper
/// section grows or shrinks by the rows moved, and the pair keeps the sum it
/// had at the press.
pub(super) fn drag(chrome: &mut Chrome, index: usize, press_row: u16, rows: [u16; 2], row: u16) {
    let wanted = (i32::from(rows[0]) + i32::from(row) - i32::from(press_row)).max(0);
    let wanted = u16::try_from(wanted).unwrap_or(u16::MAX);
    chrome
        .sidebar
        .resize_section_pair(index, rows[0], rows[1], wanted);
}

#[cfg(test)]
#[path = "sidebar_divider/tests.rs"]
mod tests;
