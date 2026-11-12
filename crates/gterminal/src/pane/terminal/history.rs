//! Bounded, generation-checked history reads. No host state is held here.

use super::*;
use crate::protocol::SnapshotMode;

pub(crate) struct HistorySnapshot {
    pub text: String,
    pub truncated: bool,
    pub total_bytes: Option<u64>,
    pub dropped_bytes: Option<u64>,
}

pub(crate) struct HistoryCapture {
    terminal: Arc<PaneTerminal>,
}

impl Drop for HistoryCapture {
    fn drop(&mut self) {
        self.terminal.capture_active.store(false, Ordering::Release);
    }
}

impl PaneTerminal {
    pub(crate) fn begin_history_capture(self: &Arc<Self>) -> Result<HistoryCapture, &'static str> {
        self.capture_active
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| "capture_busy")?;
        Ok(HistoryCapture {
            terminal: self.clone(),
        })
    }
}

impl HistoryCapture {
    pub(crate) fn read_selection(
        &self,
        start_rows: u32,
        start_col: u16,
        end_rows: u32,
        end_col: u16,
    ) -> Result<String, &'static str> {
        let terminal = &self.terminal;
        #[cfg(test)]
        terminal.note_work_started();
        let (generation, first, last, cols) = {
            let _mutation = terminal
                .history_mutation
                .lock()
                .map_err(|_| "terminal_unavailable")?;
            let core = terminal
                .ghostty
                .core
                .lock()
                .map_err(|_| "terminal_unavailable")?;
            let edge = core
                .terminal
                .total_rows()
                .map_err(|_| "read_failed")?
                .saturating_sub(1) as u32;
            let a = (edge.saturating_sub(start_rows), start_col);
            let b = (edge.saturating_sub(end_rows), end_col);
            let (first, last) = if a <= b { (a, b) } else { (b, a) };
            (
                terminal.content_generation(),
                first,
                last,
                core.terminal.cols().map_err(|_| "read_failed")?,
            )
        };
        let mut start = first.0;
        let mut text = String::new();
        let mut previous_wrap = true;
        let mut remaining_rows = crate::protocol::MAX_WRITE_BYTES;
        loop {
            let end = start.saturating_add(31).min(last.0);
            let mut piece;
            let wrapped;
            {
                let _mutation = terminal
                    .history_mutation
                    .lock()
                    .map_err(|_| "terminal_unavailable")?;
                let core = terminal
                    .ghostty
                    .core
                    .lock()
                    .map_err(|_| "terminal_unavailable")?;
                if terminal.content_generation() != generation {
                    return Err("capture_invalidated");
                }
                piece = core
                    .terminal
                    .read_text_screen(
                        (if start == first.0 { first.1 } else { 0 }, start),
                        (
                            if end == last.0 {
                                last.1
                            } else {
                                cols.saturating_sub(1)
                            },
                            end,
                        ),
                        false,
                    )
                    .map_err(|_| "read_failed")?;
                let rows = core
                    .terminal
                    .screen_text_rows_range(start as usize, end as usize + 1)
                    .map_err(|_| "read_failed")?;
                wrapped = rows.last().is_some_and(|row| row.soft_wrapped);
                if end < last.0 {
                    let empty = rows
                        .iter()
                        .enumerate()
                        .rev()
                        .take_while(|(index, row)| {
                            let row_number = start + *index as u32;
                            let lower = if row_number == first.0 {
                                usize::from(first.1)
                            } else {
                                0
                            };
                            let upper = if row_number == last.0 {
                                usize::from(last.1) + 1
                            } else {
                                usize::from(cols)
                            };
                            row.cells
                                .iter()
                                .enumerate()
                                .filter(|(col, _)| *col >= lower && *col < upper)
                                .all(|(_, cell)| {
                                    cell.graphemes
                                        .iter()
                                        .all(|ch| *ch == 0 || *ch == u32::from(b' '))
                                })
                        })
                        .filter(|(_, row)| !row.soft_wrapped)
                        .count();
                    piece.extend(std::iter::repeat_n(
                        '\n',
                        empty.saturating_sub(usize::from(piece.is_empty())),
                    ));
                }
            }
            if start != first.0 && !previous_wrap {
                text.push('\n');
            }
            text.push_str(&piece);
            // Keep Unicode lookahead so the wire encoder detects truncation.
            if text.len() > crate::protocol::MAX_WRITE_BYTES {
                let mut cap = crate::protocol::MAX_WRITE_BYTES + 1;
                while !text.is_char_boundary(cap) {
                    cap += 1;
                }
                text.truncate(cap);
                break;
            }
            if end == last.0 {
                break;
            }
            remaining_rows = remaining_rows.saturating_sub((end - start + 1) as usize);
            if remaining_rows == 0 {
                return Err("capture_limit");
            }
            previous_wrap = wrapped;
            start = end + 1;
            std::thread::yield_now();
        }
        let _mutation = terminal
            .history_mutation
            .lock()
            .map_err(|_| "terminal_unavailable")?;
        if terminal.content_generation() != generation {
            return Err("capture_invalidated");
        }
        Ok(text)
    }

    pub(crate) fn read(
        &self,
        mode: SnapshotMode,
        max_lines: usize,
        max_bytes: usize,
    ) -> Result<HistorySnapshot, &'static str> {
        self.read_chunks(mode, max_lines, max_bytes, || {})
    }

    fn read_chunks(
        &self,
        mode: SnapshotMode,
        max_lines: usize,
        max_bytes: usize,
        mut between_chunks: impl FnMut(),
    ) -> Result<HistorySnapshot, &'static str> {
        let terminal = &self.terminal;
        let (generation, mut end, viewport_rows) = {
            let _mutation = terminal
                .history_mutation
                .lock()
                .map_err(|_| "terminal_unavailable")?;
            let core = terminal
                .ghostty
                .core
                .lock()
                .map_err(|_| "terminal_unavailable")?;
            (
                terminal.content_generation(),
                core.terminal
                    .total_rows()
                    .map_err(|_| "terminal_unavailable")?,
                core.terminal.rows().map_err(|_| "terminal_unavailable")? as usize,
            )
        };
        let mut text = String::new();
        let mut pieces = std::collections::VecDeque::new();
        let mut bytes = 0usize;
        let mut lines = 0usize;
        let mut newer_nonempty = false;
        // Formatting can erase an arbitrarily long soft-wrapped blank line.
        // Bound physical traversal too, refusing instead of scanning older rows.
        let mut remaining_rows = max_lines
            .saturating_add(max_bytes)
            .saturating_add(viewport_rows);
        while end > 0 && max_lines > 0 && max_bytes > 0 {
            // A chunk bounds each terminal-lock hold, even for huge histories.
            let start = end.saturating_sub(32.min(max_lines.saturating_add(1)).min(remaining_rows));
            let (mut piece, soft_wrapped, trailing_empty) = {
                let _mutation = terminal
                    .history_mutation
                    .lock()
                    .map_err(|_| "terminal_unavailable")?;
                let core = terminal
                    .ghostty
                    .core
                    .lock()
                    .map_err(|_| "terminal_unavailable")?;
                if terminal.content_generation() != generation {
                    return Err("capture_invalidated");
                }
                let cols = core.terminal.cols().map_err(|_| "terminal_unavailable")?;
                let piece = match mode {
                    SnapshotMode::Text => core.terminal.read_text_screen(
                        (0, start as u32),
                        (cols.saturating_sub(1), (end - 1) as u32),
                        false,
                    ),
                    SnapshotMode::Ansi => core.terminal.read_ansi_screen(
                        (0, start as u32),
                        (cols.saturating_sub(1), (end - 1) as u32),
                        false,
                        true,
                    ),
                }
                .map_err(|_| "terminal_unavailable")?;
                let rows = core
                    .terminal
                    .screen_text_rows_range(start, end)
                    .map_err(|_| "terminal_unavailable")?;
                let wrapped = rows.last().is_some_and(|row| row.soft_wrapped);
                let empty = rows
                    .iter()
                    .rev()
                    .take_while(|row| {
                        row.cells.iter().all(|cell| {
                            cell.graphemes
                                .iter()
                                .all(|ch| *ch == 0 || *ch == u32::from(b' '))
                        })
                    })
                    .filter(|row| !row.soft_wrapped)
                    .count();
                (piece, wrapped, empty)
            };
            // Selection formatting trims the empty bottom of the live screen.
            // At an internal boundary retain its hard newline; soft wraps join.
            let separator = newer_nonempty && !soft_wrapped;
            if newer_nonempty {
                // The formatter trims blank rows at the selection's end. They
                // are interior history here and must survive chunk boundaries.
                let missing = trailing_empty.saturating_sub(usize::from(piece.is_empty()));
                piece.extend(std::iter::repeat_n('\n', missing));
            }
            bytes = bytes
                .saturating_add(piece.len())
                .saturating_add(usize::from(separator));
            lines = lines
                .saturating_add(piece.bytes().filter(|byte| *byte == b'\n').count())
                .saturating_add(usize::from(separator));
            if separator {
                pieces.push_front("\n".to_owned());
            }
            newer_nonempty |= !piece.is_empty();
            pieces.push_front(piece);
            remaining_rows = remaining_rows.saturating_sub(end - start);
            end = start;
            between_chunks();
            if bytes > max_bytes || lines >= max_lines {
                break;
            }
            if remaining_rows == 0 && end > 0 {
                return Err("capture_limit");
            }
            std::thread::yield_now();
        }
        {
            let _mutation = terminal
                .history_mutation
                .lock()
                .map_err(|_| "terminal_unavailable")?;
            if terminal.content_generation() != generation {
                return Err("capture_invalidated");
            }
        }
        for piece in pieces {
            text.push_str(&piece);
        }
        let original_bytes = text.len();
        let all_lines: Vec<&str> = text.lines().collect();
        let line_start = all_lines.len().saturating_sub(max_lines);
        let limited = all_lines[line_start..].join("\n");
        let mut byte_start = limited.len().saturating_sub(max_bytes);
        while !limited.is_char_boundary(byte_start) {
            byte_start += 1;
        }
        let truncated = end > 0 || line_start > 0 || byte_start > 0;
        Ok(HistorySnapshot {
            text: limited[byte_start..].to_owned(),
            truncated,
            total_bytes: (!truncated).then_some(original_bytes as u64),
            dropped_bytes: (!truncated).then_some(0),
        })
    }
}

#[cfg(test)]
#[path = "history_tests.rs"]
mod tests;
