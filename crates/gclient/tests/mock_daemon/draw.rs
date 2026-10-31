use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use ratatui::backend::{Backend, ClearType, TestBackend, WindowSize};
use ratatui::buffer::Cell;
use ratatui::layout::{Position, Size};

/// Record draws while the live loop borrows its terminal and workspace.
pub(crate) struct DrawRecorder {
    inner: TestBackend,
    draws: Arc<AtomicUsize>,
    frames: Arc<Mutex<Vec<String>>>,
}

impl DrawRecorder {
    pub(crate) fn new(
        width: u16,
        height: u16,
    ) -> (Self, Arc<AtomicUsize>, Arc<Mutex<Vec<String>>>) {
        let draws = Arc::new(AtomicUsize::new(0));
        let frames = Arc::new(Mutex::new(Vec::new()));
        (
            Self {
                inner: TestBackend::new(width, height),
                draws: Arc::clone(&draws),
                frames: Arc::clone(&frames),
            },
            draws,
            frames,
        )
    }
}

impl Backend for DrawRecorder {
    type Error = <TestBackend as Backend>::Error;

    fn draw<'a, I>(&mut self, content: I) -> Result<(), Self::Error>
    where
        I: Iterator<Item = (u16, u16, &'a Cell)>,
    {
        self.inner.draw(content)?;
        self.draws.fetch_add(1, Ordering::SeqCst);
        let buffer = self.inner.buffer();
        let width = usize::from(buffer.area.width);
        let text = buffer
            .content()
            .chunks(width)
            .map(|row| row.iter().map(Cell::symbol).collect::<String>())
            .collect::<Vec<_>>()
            .join("\n");
        self.frames.lock().expect("recorded frames").push(text);
        Ok(())
    }

    fn hide_cursor(&mut self) -> Result<(), Self::Error> {
        self.inner.hide_cursor()
    }

    fn show_cursor(&mut self) -> Result<(), Self::Error> {
        self.inner.show_cursor()
    }

    fn get_cursor_position(&mut self) -> Result<Position, Self::Error> {
        self.inner.get_cursor_position()
    }

    fn set_cursor_position<P: Into<Position>>(&mut self, position: P) -> Result<(), Self::Error> {
        self.inner.set_cursor_position(position)
    }

    fn clear(&mut self) -> Result<(), Self::Error> {
        self.inner.clear()
    }

    fn clear_region(&mut self, clear_type: ClearType) -> Result<(), Self::Error> {
        self.inner.clear_region(clear_type)
    }

    fn size(&self) -> Result<Size, Self::Error> {
        self.inner.size()
    }

    fn window_size(&mut self) -> Result<WindowSize, Self::Error> {
        self.inner.window_size()
    }

    fn flush(&mut self) -> Result<(), Self::Error> {
        self.inner.flush()
    }
}
