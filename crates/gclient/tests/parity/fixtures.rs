//! Shared render helpers for the parity suite.
//!
//! herdr's tests render into a `TestBackend` and assert row text. These
//! helpers keep that shape: build a terminal, draw once, read rows or cells
//! back. State mapping from herdr (agent -> roster entry / terminal row,
//! workspace -> gclient workspace, attention -> attention prompt) also lives
//! here so every module builds gclient state the same way.

// Helpers are shared by modules ported independently; a helper unused by one
// module is still part of the suite's vocabulary.
#![allow(dead_code)]

use ratatui::backend::TestBackend;
use ratatui::buffer::Cell;
use ratatui::layout::Rect;
use ratatui::{Frame, Terminal};

/// herdr commit the keep-set was extracted from.
pub const UPSTREAM_COMMIT: &str = "346411fa21afd297f5ed3b3fa56f9e3fbf7654b7";

/// A test terminal of the given size.
pub fn terminal(width: u16, height: u16) -> Terminal<TestBackend> {
    Terminal::new(TestBackend::new(width, height)).expect("test backend")
}

/// Draws one frame.
pub fn draw(terminal: &mut Terminal<TestBackend>, render: impl FnOnce(&mut Frame)) {
    terminal.draw(|frame| render(frame)).expect("draw frame");
}

/// Builds a terminal of the given size and draws one frame into it.
pub fn render(width: u16, height: u16, render: impl FnOnce(&mut Frame)) -> Terminal<TestBackend> {
    let mut terminal = terminal(width, height);
    draw(&mut terminal, render);
    terminal
}

/// One cell of the last drawn frame.
pub fn cell(terminal: &Terminal<TestBackend>, x: u16, y: u16) -> &Cell {
    &terminal.backend().buffer()[(x, y)]
}

/// Text of row `y` across the full width, trailing spaces kept.
pub fn row_text(terminal: &Terminal<TestBackend>, y: u16) -> String {
    let buffer = terminal.backend().buffer();
    (0..buffer.area.width)
        .map(|x| buffer[(x, y)].symbol())
        .collect()
}

/// Text of every row, top to bottom.
pub fn rows(terminal: &Terminal<TestBackend>) -> Vec<String> {
    let height = terminal.backend().buffer().area.height;
    (0..height).map(|y| row_text(terminal, y)).collect()
}

/// Text of the rows inside `rect`, clipped to its columns.
pub fn rect_rows(terminal: &Terminal<TestBackend>, rect: Rect) -> Vec<String> {
    let buffer = terminal.backend().buffer();
    (rect.y..rect.y + rect.height)
        .map(|y| {
            (rect.x..rect.x + rect.width)
                .map(|x| buffer[(x, y)].symbol())
                .collect()
        })
        .collect()
}

/// The whole frame as newline-joined rows.
pub fn screen(terminal: &Terminal<TestBackend>) -> String {
    rows(terminal).join("\n")
}
