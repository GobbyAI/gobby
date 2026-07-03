//! Lightweight stderr progress rendering for long-running CLI work.

use std::io::{self, IsTerminal, Write};

const DEFAULT_BAR_WIDTH: usize = 20;
const DEFAULT_LINE_WIDTH: usize = 80;

pub struct ProgressBar<W = io::Stderr> {
    total: usize,
    current: usize,
    enabled: bool,
    bar_width: usize,
    line_width: usize,
    writer: W,
    render_error: Option<io::Error>,
}

impl ProgressBar<io::Stderr> {
    pub fn new(total: usize, quiet: bool) -> Self {
        Self::with_writer(total, quiet, io::stderr().is_terminal(), io::stderr())
    }
}

impl<W> ProgressBar<W>
where
    W: Write,
{
    pub fn with_writer(total: usize, quiet: bool, is_terminal: bool, writer: W) -> Self {
        Self {
            total,
            current: 0,
            enabled: !quiet && is_terminal && total > 0,
            bar_width: DEFAULT_BAR_WIDTH,
            line_width: DEFAULT_LINE_WIDTH,
            writer,
            render_error: None,
        }
    }

    pub fn tick(&mut self, item: impl AsRef<str>) {
        self.current += 1;
        self.draw(item);
    }

    pub fn draw(&mut self, item: impl AsRef<str>) {
        if !self.enabled {
            return;
        }

        let line = self.render_line(item.as_ref());
        if let Err(error) = write!(self.writer, "{line}") {
            self.record_render_error(error);
            return;
        }
        if let Err(error) = self.writer.flush() {
            self.record_render_error(error);
        }
    }

    pub fn finish(&mut self) {
        if self.enabled {
            if let Err(error) = write!(self.writer, "\r\x1b[K") {
                self.record_render_error(error);
                return;
            }
            if let Err(error) = self.writer.flush() {
                self.record_render_error(error);
            }
        }
    }

    pub fn is_enabled(&self) -> bool {
        self.enabled
    }

    pub fn position(&self) -> usize {
        self.current
    }

    pub fn render_error(&self) -> Option<&io::Error> {
        self.render_error.as_ref()
    }

    pub fn into_writer(self) -> W {
        self.writer
    }

    fn render_line(&self, item: &str) -> String {
        let filled = (self.current.min(self.total) * self.bar_width) / self.total;
        let empty = self.bar_width - filled;
        let bar = format!("{}{}", "#".repeat(filled), "-".repeat(empty));
        let counter = format!("{}/{}", self.current, self.total);
        let prefix_width = self.bar_width + 6 + counter.chars().count();
        let max_item_width = self.line_width.saturating_sub(prefix_width);
        let display_item = truncate_left(item, max_item_width);
        format!("\r[{bar}] {counter} : {display_item}\x1b[K")
    }

    fn record_render_error(&mut self, error: io::Error) {
        if self.render_error.is_none() {
            self.render_error = Some(error);
        }
        self.enabled = false;
    }
}

fn truncate_left(value: &str, max_chars: usize) -> String {
    let value_chars = value.chars().count();
    if value_chars <= max_chars {
        return value.to_string();
    }
    if max_chars == 0 {
        return String::new();
    }
    if max_chars <= 3 {
        return ".".repeat(max_chars);
    }

    let tail_len = max_chars - 3;
    let tail = value
        .chars()
        .rev()
        .take(tail_len)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect::<String>();
    format!("...{tail}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Default)]
    struct FailingWriter {
        writes: usize,
    }

    impl Write for FailingWriter {
        fn write(&mut self, _buf: &[u8]) -> io::Result<usize> {
            self.writes += 1;
            Err(io::Error::new(io::ErrorKind::BrokenPipe, "closed"))
        }

        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    fn output_from(bar: ProgressBar<Vec<u8>>) -> String {
        String::from_utf8(bar.into_writer()).expect("progress output is utf8")
    }

    #[test]
    fn renders_progress_when_enabled() {
        let mut bar = ProgressBar::with_writer(4, false, true, Vec::new());

        bar.tick("src/lib.rs");

        assert!(bar.is_enabled());
        assert_eq!(bar.position(), 1);
        assert_eq!(
            output_from(bar),
            "\r[#####---------------] 1/4 : src/lib.rs\x1b[K"
        );
    }

    #[test]
    fn draw_renders_current_position_without_advancing() {
        let mut bar = ProgressBar::with_writer(4, false, true, Vec::new());

        bar.draw("src/lib.rs");

        assert_eq!(bar.position(), 0);
        assert_eq!(
            output_from(bar),
            "\r[--------------------] 0/4 : src/lib.rs\x1b[K"
        );
    }

    #[test]
    fn quiet_mode_suppresses_output_but_tracks_position() {
        let mut bar = ProgressBar::with_writer(3, true, true, Vec::new());

        bar.tick("src/lib.rs");
        bar.finish();

        assert!(!bar.is_enabled());
        assert_eq!(bar.position(), 1);
        assert_eq!(output_from(bar), "");
    }

    #[test]
    fn non_terminal_capture_suppresses_output() {
        let mut bar = ProgressBar::with_writer(3, false, false, Vec::new());

        bar.tick("src/lib.rs");
        bar.finish();

        assert!(!bar.is_enabled());
        assert_eq!(output_from(bar), "");
    }

    #[test]
    fn long_items_truncate_from_left() {
        let mut bar = ProgressBar::with_writer(1, false, true, Vec::new());
        let item = "a".repeat(100);

        bar.tick(item);

        let output = output_from(bar);
        assert!(output.starts_with("\r[####################] 1/1 : ..."));
        assert!(output.ends_with("\x1b[K"));
        assert!(output.len() <= DEFAULT_LINE_WIDTH + "\r\x1b[K".len());
    }

    #[test]
    fn finish_clears_rendered_line() {
        let mut bar = ProgressBar::with_writer(1, false, true, Vec::new());

        bar.tick("src/lib.rs");
        bar.finish();

        assert!(output_from(bar).ends_with("\r\x1b[K"));
    }

    #[test]
    fn zero_total_disables_progress() {
        let mut bar = ProgressBar::with_writer(0, false, true, Vec::new());

        bar.tick("src/lib.rs");

        assert!(!bar.is_enabled());
        assert_eq!(bar.position(), 1);
        assert_eq!(output_from(bar), "");
    }

    #[test]
    fn first_render_error_disables_progress_and_is_observable() {
        let mut bar = ProgressBar::with_writer(2, false, true, FailingWriter::default());

        bar.tick("src/lib.rs");

        assert!(!bar.is_enabled());
        assert_eq!(bar.position(), 1);
        assert_eq!(
            bar.render_error().map(io::Error::kind),
            Some(io::ErrorKind::BrokenPipe)
        );
        bar.tick("src/main.rs");
        assert_eq!(bar.into_writer().writes, 1);
    }
}
