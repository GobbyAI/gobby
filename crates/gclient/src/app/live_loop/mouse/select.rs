//! Pointer selection inside a pane: press, drag and release copies; a click
//! that never moves clears; a double-click takes the token under the pointer
//! and a triple-click the row (herdr `copy_on_select`, word and line
//! selection).
//!
//! Terminal mode arrives through `pointer::down` on the focused pane; copy
//! mode arrives through `copy_mode::route_mouse_selection`. Both drive the
//! same `anchor_selection`, `extend_selection` and `finish_selection`, so
//! there is one selection implementation. Copying is the loop's job: a
//! finalized selection surfaces as `MouseOutcome::Copy`.

use std::time::{Duration, Instant};

use crossterm::event::{KeyModifiers, MouseEvent};
use gobby_terminal::layout;
use gobby_terminal::protocol::{CellData, FrameData, MouseTracking};
use gobby_terminal::selection::Selection;
use ratatui::layout::Rect;

use crate::app::{Pane, PaneId};
use crate::ui::pane_layout::metrics_for;
use crate::ui::{Chrome, WorkspaceView};

use super::{MouseGesture, MouseOutcome};

/// Presses on one screen cell this close together are one click run.
pub const DOUBLE_CLICK_MS: u64 = 400;

/// Punctuation that stays inside a double-click token, beside alphanumerics.
const TOKEN_PUNCTUATION: &str = "_-./~:@#%+=?&";

/// The run of presses on one screen cell that makes a double- or triple-click.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ClickRun {
    /// When the latest press landed.
    pub at: Instant,
    /// Screen cell the run is on.
    pub cell: (u16, u16),
    /// Presses so far, 1 to 3.
    pub count: u8,
}

/// Left button down on the focused pane, at `col`/`row` inside `slot`'s
/// inner rect. A pane that reports the mouse keeps the press for forwarding
/// unless shift is held (herdr `shift_bypasses_mouse_reporting`). Otherwise
/// the first press of a run anchors a drag selection, the second selects the
/// token under the pointer and the third the row; those two are finalized at
/// once, so the loop copies them.
pub(super) fn down<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    pane: PaneId,
    slot: layout::PaneId,
    col: u16,
    row: u16,
    mouse: &MouseEvent,
) -> MouseOutcome {
    let state = ws.pane(pane);
    let reporting = state
        .latest_frame()
        .is_some_and(|frame| frame.modes.mouse_tracking() != MouseTracking::Off);
    if reporting && !mouse.modifiers.contains(KeyModifiers::SHIFT) {
        return MouseOutcome::Ignore;
    }
    let Some(inner) = inner_rect(chrome, slot) else {
        return MouseOutcome::Ignore;
    };
    let now = Instant::now();
    let cell = (mouse.column, mouse.row);
    let count = match chrome.last_click {
        Some(run)
            if run.cell == cell
                && now.duration_since(run.at) <= Duration::from_millis(DOUBLE_CLICK_MS) =>
        {
            run.count % 3 + 1
        }
        _ => 1,
    };
    chrome.last_click = Some(ClickRun {
        at: now,
        cell,
        count,
    });
    let span = match (count, state.latest_frame()) {
        (2, Some(frame)) => token_span(frame, row, col),
        (3, Some(frame)) => row_span(frame, row),
        _ => {
            anchor_selection(chrome, state, slot, inner, col, row);
            chrome.gesture = Some(MouseGesture::Select { slot });
            return MouseOutcome::Handled;
        }
    };
    let Some((start, end)) = span else {
        chrome.selection = None;
        return MouseOutcome::Handled;
    };
    let metrics = metrics_for(state.scroll_offset, state.max_scroll, inner.height);
    let mut selection = Selection::anchor(slot, row, start, Some(metrics));
    selection.drag(inner.x + end, inner.y + row, inner, Some(metrics));
    selection.force_dragging();
    selection.finish();
    chrome.selection = Some(selection);
    MouseOutcome::Copy
}

/// The selection follows the pointer while the button stays down.
pub(super) fn drag<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    slot: layout::PaneId,
    mouse: &MouseEvent,
) -> MouseOutcome {
    if let Some((pane, inner)) = chrome.pane_for_slot(slot).zip(inner_rect(chrome, slot)) {
        extend_selection(chrome, ws.pane(pane), inner, mouse.column, mouse.row);
    }
    MouseOutcome::Handled
}

/// The button came up: a dragged selection is finalized for the loop to copy;
/// a click that never moved leaves nothing selected.
pub(super) fn up(chrome: &mut Chrome) -> MouseOutcome {
    if finish_selection(chrome) {
        MouseOutcome::Copy
    } else {
        MouseOutcome::Handled
    }
}

/// Start a selection for `slot` at `col`/`row` inside `inner`, replacing any
/// earlier one.
pub fn anchor_selection(
    chrome: &mut Chrome,
    pane: &Pane,
    slot: layout::PaneId,
    inner: Rect,
    col: u16,
    row: u16,
) {
    let metrics = metrics_for(pane.scroll_offset, pane.max_scroll, inner.height);
    chrome.selection = Some(Selection::anchor(slot, row, col, Some(metrics)));
}

/// Extend the selection in progress to the screen cell `column`/`row`,
/// clamped to `inner`.
pub fn extend_selection(chrome: &mut Chrome, pane: &Pane, inner: Rect, column: u16, row: u16) {
    let metrics = metrics_for(pane.scroll_offset, pane.max_scroll, inner.height);
    if let Some(selection) = chrome.selection.as_mut() {
        selection.drag(column, row, inner, Some(metrics));
    }
}

/// Finish the selection in progress. Returns whether it covers anything; a
/// press that never moved leaves no selection behind.
pub fn finish_selection(chrome: &mut Chrome) -> bool {
    let done = chrome.selection.as_mut().is_some_and(Selection::finish);
    if !done {
        chrome.selection = None;
    }
    done
}

fn inner_rect(chrome: &Chrome, slot: layout::PaneId) -> Option<Rect> {
    chrome
        .view
        .pane_infos
        .iter()
        .find(|info| info.id == slot)
        .map(|info| info.inner_rect)
}

/// Cells of frame row `row`, when the frame has it.
pub(super) fn frame_row(frame: &FrameData, row: u16) -> Option<&[CellData]> {
    if row >= frame.height {
        return None;
    }
    let width = usize::from(frame.width);
    let start = usize::from(row) * width;
    frame.cells.get(start..start + width)
}

/// Whether `cell` is part of a token: alphanumeric or `TOKEN_PUNCTUATION`.
fn is_token(cell: &CellData) -> bool {
    !cell.skip
        && !cell.symbol.is_empty()
        && cell
            .symbol
            .chars()
            .all(|c| c.is_alphanumeric() || TOKEN_PUNCTUATION.contains(c))
}

/// Columns of the token under `col` on frame row `row`, inclusive. A wide
/// character's continuation cell is read as its owner on the left.
fn token_span(frame: &FrameData, row: u16, col: u16) -> Option<(u16, u16)> {
    let cells = frame_row(frame, row)?;
    let cell = |c: u16| cells.get(usize::from(c));
    let owner = |c: u16| cell(c).map(|found| if found.skip { c.saturating_sub(1) } else { c });
    let origin = owner(col)?;
    if !cell(origin).is_some_and(is_token) {
        return None;
    }
    let mut start = origin;
    while let Some(prev) = start.checked_sub(1).and_then(owner) {
        if !cell(prev).is_some_and(is_token) {
            break;
        }
        start = prev;
    }
    let mut end = origin;
    while let Some(next) = end.checked_add(1) {
        if !cell(next).is_some_and(|found| found.skip || is_token(found)) {
            break;
        }
        end = next;
    }
    Some((start, end))
}

/// Columns of the text on frame row `row`, inclusive, without its blank tail.
fn row_span(frame: &FrameData, row: u16) -> Option<(u16, u16)> {
    let cells = frame_row(frame, row)?;
    let end = cells
        .iter()
        .rposition(|cell| !cell.skip && !cell.symbol.trim().is_empty())?;
    Some((0, u16::try_from(end).ok()?))
}

#[cfg(test)]
mod tests {
    use super::*;
    use gobby_terminal::protocol::PaneModes;

    /// A one-row frame; `|` marks a wide character's continuation cell.
    fn frame(row: &str) -> FrameData {
        let cells: Vec<CellData> = row
            .chars()
            .map(|c| CellData {
                symbol: if c == '|' {
                    String::new()
                } else {
                    c.to_string()
                },
                fg: 0,
                bg: 0,
                modifier: 0,
                skip: c == '|',
                hyperlink: None,
            })
            .collect();
        FrameData {
            width: cells.len() as u16,
            height: 1,
            cells,
            cursor: None,
            hyperlinks: Vec::new(),
            graphics: Vec::new(),
            modes: PaneModes::default(),
        }
    }

    #[test]
    fn token_span_expands_over_token_characters_and_wide_cells() {
        let frame = frame("cd ~/a-b_c.d 日|本| x");
        assert_eq!(token_span(&frame, 0, 0), Some((0, 1)));
        assert_eq!(token_span(&frame, 0, 2), None);
        assert_eq!(token_span(&frame, 0, 7), Some((3, 11)));
        assert_eq!(token_span(&frame, 0, 14), Some((13, 16)));
        assert_eq!(token_span(&frame, 0, 18), Some((18, 18)));
        assert_eq!(token_span(&frame, 0, 40), None);
        assert_eq!(token_span(&frame, 1, 0), None);
    }

    #[test]
    fn row_span_drops_the_blank_tail() {
        assert_eq!(row_span(&frame("hi there   "), 0), Some((0, 7)));
        assert_eq!(row_span(&frame("     "), 0), None);
        assert_eq!(row_span(&frame("x"), 1), None);
    }
}
