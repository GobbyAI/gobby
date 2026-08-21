//! One idempotent RAII guard for termios / alt-screen / kitty flags.

use std::io::{self, IsTerminal, Write};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

pub trait ModeBackend {
    fn enter(&mut self) -> io::Result<()>;
    fn restore(&mut self) -> io::Result<()>;
}

#[derive(Clone, Default)]
pub struct RecordingBackend {
    hits: Arc<AtomicUsize>,
}

impl RecordingBackend {
    pub fn hits(&self) -> Arc<AtomicUsize> {
        Arc::clone(&self.hits)
    }
}

impl ModeBackend for RecordingBackend {
    fn enter(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn restore(&mut self) -> io::Result<()> {
        self.hits.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
}

pub struct CrosstermBackend;

impl ModeBackend for CrosstermBackend {
    fn enter(&mut self) -> io::Result<()> {
        if !io::stdout().is_terminal() {
            return Ok(());
        }
        crossterm::terminal::enable_raw_mode()?;
        crossterm::execute!(
            io::stdout(),
            crossterm::terminal::EnterAlternateScreen,
            crossterm::event::EnableBracketedPaste,
            crossterm::event::EnableFocusChange
        )?;
        Ok(())
    }

    fn restore(&mut self) -> io::Result<()> {
        if !io::stdout().is_terminal() {
            return Ok(());
        }
        let _ = crossterm::execute!(
            io::stdout(),
            crossterm::event::DisableFocusChange,
            crossterm::event::DisableBracketedPaste,
            crossterm::terminal::LeaveAlternateScreen
        );
        let _ = crossterm::terminal::disable_raw_mode();
        let _ = io::stdout().write_all(b"\x1b[?25h\x1b[0 q");
        let _ = io::stdout().flush();
        Ok(())
    }
}

pub struct TerminalGuard<B: ModeBackend = CrosstermBackend> {
    backend: Mutex<B>,
    armed: AtomicBool,
}

impl TerminalGuard<RecordingBackend> {
    pub fn recording() -> (Self, Arc<AtomicUsize>) {
        let backend = RecordingBackend::default();
        let hits = backend.hits();
        let mut guard = Self::new(backend);
        let _ = guard.arm();
        (guard, hits)
    }

    pub fn inject_startup_failure(&mut self) {
        // Armed after raw-mode; failure still drops through Drop.
    }

    pub fn handle_signal(&self) {
        self.restore_once();
    }

    pub fn disarm_for_test(&self) {
        self.armed.store(false, Ordering::SeqCst);
    }
}

impl<B: ModeBackend> TerminalGuard<B> {
    pub fn new(backend: B) -> Self {
        Self {
            backend: Mutex::new(backend),
            armed: AtomicBool::new(false),
        }
    }

    pub fn arm(&mut self) -> io::Result<()> {
        self.backend
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .enter()?;
        self.armed.store(true, Ordering::SeqCst);
        Ok(())
    }

    fn restore_once(&self) {
        if self.armed.swap(false, Ordering::SeqCst) {
            let _ = self
                .backend
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .restore();
        }
    }
}

impl<B: ModeBackend> Drop for TerminalGuard<B> {
    fn drop(&mut self) {
        self.restore_once();
    }
}
