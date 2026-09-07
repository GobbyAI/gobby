//! Attachment-local copy-mode and lease-gated paste helpers.

use crate::app::{anchor_selection, extend_selection, finish_selection, Workspace};
use crate::daemon::{Daemon, DaemonError};
use crate::ui::chrome::{Chrome, Mode};
use base64::Engine as _;
use crossterm::event::{MouseButton, MouseEventKind};
use gobby_terminal::layout::ScrollMetrics;
use gobby_terminal::raw_input::RawInputEvent;
use gobby_terminal::selection::Selection;
use std::io::{self, Write};
use unicode_width::UnicodeWidthStr;

pub const PASTE_MAX_BYTES: usize = 1024 * 1024;

/// Soft-wrap marker preserved in tmux `capture-pane -J` history payloads.
pub const SOFT_WRAP: char = '\u{23CE}';

/// Join visual wraps so a wide grapheme stays on its logical line.
pub fn extract_logical_line(text: &str, wrap_cols: usize) -> String {
    let mut logical = String::new();
    for visual in text.split_inclusive('\n') {
        let has_newline = visual.ends_with('\n');
        let line = visual.strip_suffix('\n').unwrap_or(visual);
        let line = line.strip_suffix('\r').unwrap_or(line);
        let explicitly_wrapped = line.ends_with(SOFT_WRAP);
        let line = line.strip_suffix(SOFT_WRAP).unwrap_or(line);
        logical.push_str(line);
        if !has_newline
            || !(explicitly_wrapped || (wrap_cols > 0 && UnicodeWidthStr::width(line) >= wrap_cols))
        {
            break;
        }
    }
    logical.replace(SOFT_WRAP, "")
}

/// Write finalized selected text as one OSC 52 clipboard transfer.
pub fn write_selection_osc52(
    writer: &mut impl Write,
    selection: &Selection,
    text: &str,
) -> io::Result<bool> {
    if !selection.is_finalized() {
        return Ok(false);
    }
    let encoded = base64::engine::general_purpose::STANDARD.encode(text.as_bytes());
    write!(writer, "\x1b]52;c;{encoded}\x07")?;
    Ok(true)
}

/// Copy the finalized selection retained by the client chrome from the pane's
/// Text of the finalized selection: the cells it covers in the pane's latest
/// frame, or the attach history before the first frame. `None` when nothing
/// is selected.
pub fn finalized_selection_text<D: Daemon>(
    workspace: &Workspace<D>,
    chrome: &Chrome,
) -> Option<String> {
    let selection = chrome
        .selection
        .as_ref()
        .filter(|selection| selection.is_finalized())?;
    let pane_id = chrome.pane_for_slot(selection.pane_id)?;
    let pane = workspace.pane(pane_id);
    let selected = if let Some(frame) = pane.latest_frame() {
        let metrics = ScrollMetrics {
            offset_from_bottom: pane.scroll_offset() as usize,
            max_offset_from_bottom: pane.max_scroll as usize,
            viewport_rows: frame.height as usize,
        };
        let mut selected = String::new();
        for row in 0..frame.height {
            let row_start = selected.len();
            for col in 0..frame.width {
                let index = usize::from(row) * usize::from(frame.width) + usize::from(col);
                if selection.contains(row, col, Some(metrics)) {
                    if let Some(cell) = frame.cells.get(index).filter(|cell| !cell.skip) {
                        selected.push_str(&cell.symbol);
                    }
                }
            }
            if selected.len() > row_start {
                selected.push('\n');
            }
        }
        selected.pop();
        extract_logical_line(&selected, usize::from(frame.width))
    } else {
        extract_logical_line(pane.attach_history()?, usize::from(pane.viewport().1))
    };
    (!selected.is_empty()).then_some(selected)
}

/// Write the finalized selection to the clipboard as OSC 52. Returns whether
/// anything was written.
pub fn copy_finalized_selection<D: Daemon>(
    workspace: &Workspace<D>,
    chrome: &Chrome,
    output: &mut impl Write,
) -> io::Result<bool> {
    match (
        chrome.selection.as_ref(),
        finalized_selection_text(workspace, chrome),
    ) {
        (Some(selection), Some(text)) => write_selection_osc52(output, selection, &text),
        _ => Ok(false),
    }
}

/// `copy_finalized_selection` that also keeps the text on `Chrome::last_copy`
/// for middle-click paste.
pub fn copy_selection<D: Daemon>(
    workspace: &Workspace<D>,
    chrome: &mut Chrome,
    output: &mut impl Write,
) -> io::Result<bool> {
    let Some(text) = finalized_selection_text(workspace, chrome) else {
        return Ok(false);
    };
    let Some(selection) = chrome.selection.as_ref() else {
        return Ok(false);
    };
    let copied = write_selection_osc52(output, selection, &text)?;
    if copied {
        chrome.last_copy = Some(text);
    }
    Ok(copied)
}

/// Update the retained selection for a copy-mode mouse gesture. Returns true
/// only when releasing the mouse finalized a visible selection.
pub fn route_mouse_selection<D: Daemon>(
    workspace: &Workspace<D>,
    chrome: &mut Chrome,
    event: &RawInputEvent,
) -> bool {
    let RawInputEvent::Mouse(mouse) = event else {
        return false;
    };
    if chrome.mode != Mode::Copy {
        return false;
    }
    match mouse.kind {
        MouseEventKind::Down(MouseButton::Left) => {
            let hit = chrome.view.pane_infos.iter().find_map(|info| {
                let inner = info.inner_rect;
                (mouse.column >= inner.x
                    && mouse.column < inner.x.saturating_add(inner.width)
                    && mouse.row >= inner.y
                    && mouse.row < inner.y.saturating_add(inner.height))
                .then_some((info.id, inner))
            });
            let Some((slot, inner)) = hit else {
                chrome.selection = None;
                return false;
            };
            let Some(pane_id) = chrome.pane_for_slot(slot) else {
                return false;
            };
            anchor_selection(
                chrome,
                workspace.pane(pane_id),
                slot,
                inner,
                mouse.column - inner.x,
                mouse.row - inner.y,
            );
            false
        }
        MouseEventKind::Drag(MouseButton::Left) | MouseEventKind::Up(MouseButton::Left) => {
            let Some(slot) = chrome.selection.as_ref().map(|selection| selection.pane_id) else {
                return false;
            };
            let Some(inner) = chrome
                .view
                .pane_infos
                .iter()
                .find(|info| info.id == slot)
                .map(|info| info.inner_rect)
            else {
                return false;
            };
            let Some(pane_id) = chrome.pane_for_slot(slot) else {
                return false;
            };
            extend_selection(
                chrome,
                workspace.pane(pane_id),
                inner,
                mouse.column,
                mouse.row,
            );
            matches!(mouse.kind, MouseEventKind::Up(MouseButton::Left)) && finish_selection(chrome)
        }
        _ => false,
    }
}

/// Consume bracketed-paste input through the lease-gated daemon paste path.
pub fn route_paste_event(
    workspace: &mut Workspace,
    chrome: &Chrome,
    event: &RawInputEvent,
) -> Result<bool, DaemonError> {
    let RawInputEvent::Paste(text) = event else {
        return Ok(false);
    };
    if let Some(pane_id) = chrome.focused_pane() {
        workspace.paste_to_pty(pane_id, text)?;
    }
    Ok(true)
}

/// herdr 0.8.0 `paste_payload`: one bracketed unit when the child asked for it.
pub fn paste_payload(text: &str, bracketed: bool) -> String {
    if bracketed {
        format!("\u{1b}[200~{text}\u{1b}[201~")
    } else {
        text.to_string()
    }
}
