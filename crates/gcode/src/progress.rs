//! Lightweight progress bar for indexing operations.
//!
//! Single-line overwriting display on stderr. No-op when stderr is not a TTY
//! or when quiet mode is enabled. Zero external dependencies.

use std::io::{IsTerminal, Write};

/// A simple progress bar that overwrites a single line on stderr.
pub struct ProgressBar {
    total: usize,
    current: usize,
    enabled: bool,
    bar_width: usize,
}

impl ProgressBar {
    /// Create a new progress bar. Renders only when stderr is a TTY and `quiet` is false.
    pub fn new(total: usize, quiet: bool) -> Self {
        let enabled = !quiet && total > 0 && std::io::stderr().is_terminal();
        Self {
            total,
            current: 0,
            enabled,
            bar_width: 20,
        }
    }

    /// Advance the progress bar and display the current file being indexed.
    pub fn tick(&mut self, file_path: &str) {
        self.current += 1;
        if !self.enabled {
            return;
        }

        let pct = self.current as f64 / self.total as f64;
        let filled = (pct * self.bar_width as f64) as usize;
        let empty = self.bar_width - filled;

        // Truncate long paths to keep the line reasonable
        let display_path = if file_path.len() > 40 {
            &file_path[file_path.len() - 40..]
        } else {
            file_path
        };

        let bar: String = "█".repeat(filled) + &"░".repeat(empty);

        // \r overwrites the line, \x1b[K clears to end of line
        eprint!(
            "\rIndexing {} {} {}/{}  \x1b[K",
            display_path, bar, self.current, self.total
        );
        let _ = std::io::stderr().flush();
    }

    /// Clear the progress line after completion.
    pub fn finish(&self) {
        if self.enabled {
            eprint!("\r\x1b[K");
            let _ = std::io::stderr().flush();
        }
    }
}
