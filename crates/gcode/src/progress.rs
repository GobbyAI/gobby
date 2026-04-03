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

        let bar: String = "=".repeat(filled) + &" ".repeat(empty);
        let counter = format!("{}/{}", self.current, self.total);

        // Truncate path from the left to fit remaining terminal width
        // Layout: \r[{bar}] {counter} : {path}\x1b[K
        // Fixed prefix width: 1 + bar_width + 1 + 1 + counter + 3 = bar_width + 6 + counter.len()
        let prefix_width = self.bar_width + 6 + counter.len();
        let max_path = 80usize.saturating_sub(prefix_width);
        let display_path = if file_path.len() > max_path && max_path > 3 {
            let start = file_path
                .char_indices()
                .rev()
                .nth(max_path - 4)
                .map(|(i, _)| i)
                .unwrap_or(0);
            format!("...{}", &file_path[start..])
        } else {
            file_path.to_string()
        };

        // \r overwrites the line, \x1b[K clears to end of line
        eprint!("\r[{bar}] {counter} : {display_path}\x1b[K");
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
