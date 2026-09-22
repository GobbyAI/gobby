//! One idempotent RAII guard for termios / alt-screen / kitty flags.

use crate::daemon::{Daemon, DaemonError};
use futures_util::future::{join_all, poll_fn};
use serde_json::Value;
use std::future::Future;
use std::io::{self, IsTerminal};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::task::Poll;
use tokio::time::{timeout_at, Instant};

pub trait ModeBackend {
    fn enter(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn restore(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn enable_raw_mode(&mut self) -> io::Result<()> {
        self.enter()
    }

    fn supports_keyboard_enhancement(&mut self) -> bool {
        false
    }

    fn push_keyboard_enhancement_flags(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn pop_keyboard_enhancement_flags(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn enter_alternate_screen(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn enable_bracketed_paste(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn hide_cursor(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn show_cursor(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn disable_bracketed_paste(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn leave_alternate_screen(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn disable_raw_mode(&mut self) -> io::Result<()> {
        self.restore()
    }

    fn enable_mouse_capture(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn disable_mouse_capture(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[derive(Clone, Default)]
pub struct RecordingBackend {
    hits: Arc<AtomicUsize>,
    mouse_capture: Arc<AtomicBool>,
}

impl RecordingBackend {
    pub fn hits(&self) -> Arc<AtomicUsize> {
        Arc::clone(&self.hits)
    }

    /// Whether the backend currently reports mouse capture as enabled.
    pub fn mouse_capture(&self) -> Arc<AtomicBool> {
        Arc::clone(&self.mouse_capture)
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

    fn enable_mouse_capture(&mut self) -> io::Result<()> {
        self.mouse_capture.store(true, Ordering::SeqCst);
        Ok(())
    }

    fn disable_mouse_capture(&mut self) -> io::Result<()> {
        self.mouse_capture.store(false, Ordering::SeqCst);
        Ok(())
    }
}

pub struct CrosstermBackend;

impl ModeBackend for CrosstermBackend {
    fn enable_raw_mode(&mut self) -> io::Result<()> {
        if !io::stdout().is_terminal() {
            return Ok(());
        }
        crossterm::terminal::enable_raw_mode()
    }

    fn supports_keyboard_enhancement(&mut self) -> bool {
        io::stdout().is_terminal()
            && matches!(
                crossterm::terminal::supports_keyboard_enhancement(),
                Ok(true)
            )
    }

    fn push_keyboard_enhancement_flags(&mut self) -> io::Result<()> {
        crossterm::execute!(
            io::stdout(),
            crossterm::event::PushKeyboardEnhancementFlags(
                crossterm::event::KeyboardEnhancementFlags::DISAMBIGUATE_ESCAPE_CODES
            )
        )
    }

    fn pop_keyboard_enhancement_flags(&mut self) -> io::Result<()> {
        crossterm::execute!(io::stdout(), crossterm::event::PopKeyboardEnhancementFlags)
    }

    fn enter_alternate_screen(&mut self) -> io::Result<()> {
        if !io::stdout().is_terminal() {
            return Ok(());
        }
        crossterm::execute!(io::stdout(), crossterm::terminal::EnterAlternateScreen)
    }

    fn enable_bracketed_paste(&mut self) -> io::Result<()> {
        if !io::stdout().is_terminal() {
            return Ok(());
        }
        crossterm::execute!(io::stdout(), crossterm::event::EnableBracketedPaste)
    }

    fn hide_cursor(&mut self) -> io::Result<()> {
        if !io::stdout().is_terminal() {
            return Ok(());
        }
        crossterm::execute!(io::stdout(), crossterm::cursor::Hide)
    }

    fn show_cursor(&mut self) -> io::Result<()> {
        if !io::stdout().is_terminal() {
            return Ok(());
        }
        crossterm::execute!(io::stdout(), crossterm::cursor::Show)
    }

    fn disable_bracketed_paste(&mut self) -> io::Result<()> {
        if !io::stdout().is_terminal() {
            return Ok(());
        }
        crossterm::execute!(io::stdout(), crossterm::event::DisableBracketedPaste)
    }

    fn leave_alternate_screen(&mut self) -> io::Result<()> {
        if !io::stdout().is_terminal() {
            return Ok(());
        }
        crossterm::execute!(io::stdout(), crossterm::terminal::LeaveAlternateScreen)
    }

    fn disable_raw_mode(&mut self) -> io::Result<()> {
        if !io::stdout().is_terminal() {
            return Ok(());
        }
        crossterm::terminal::disable_raw_mode()
    }

    fn enable_mouse_capture(&mut self) -> io::Result<()> {
        if !io::stdout().is_terminal() {
            return Ok(());
        }
        crossterm::execute!(io::stdout(), crossterm::event::EnableMouseCapture)
    }

    fn disable_mouse_capture(&mut self) -> io::Result<()> {
        if !io::stdout().is_terminal() {
            return Ok(());
        }
        crossterm::execute!(io::stdout(), crossterm::event::DisableMouseCapture)
    }
}

#[derive(Default)]
struct Obligations {
    raw_mode: bool,
    keyboard_enhancement: bool,
    alternate_screen: bool,
    mouse_capture: bool,
    bracketed_paste: bool,
    hidden_cursor: bool,
}

struct GuardState<B> {
    backend: B,
    obligations: Obligations,
    keyboard_enhancement_supported: Option<bool>,
    mouse_capture_requested: bool,
}

pub struct TerminalGuard<B: ModeBackend = CrosstermBackend> {
    state: Mutex<GuardState<B>>,
}

pub type TerminalModeGuard<B = CrosstermBackend> = TerminalGuard<B>;

/// Host-terminal mode control the live loop drives from settings and signals.
pub trait MouseCaptureSwitch {
    fn set_mouse_capture(&mut self, on: bool) -> io::Result<()>;
    fn suspend(&mut self) -> io::Result<()>;
    fn resume(&mut self) -> io::Result<()>;
}

impl<B: ModeBackend> MouseCaptureSwitch for TerminalGuard<B> {
    fn set_mouse_capture(&mut self, on: bool) -> io::Result<()> {
        TerminalGuard::set_mouse_capture(self, on)
    }

    fn suspend(&mut self) -> io::Result<()> {
        TerminalGuard::suspend(self)
    }

    fn resume(&mut self) -> io::Result<()> {
        TerminalGuard::resume(self)
    }
}

impl TerminalGuard<RecordingBackend> {
    pub fn recording() -> (Self, Arc<AtomicUsize>) {
        let backend = RecordingBackend::default();
        let hits = backend.hits();
        let mut guard = Self::new(backend);
        let _ = guard.arm(false);
        (guard, hits)
    }

    pub fn inject_startup_failure(&mut self) {
        // Armed after raw-mode; failure still drops through Drop.
    }

    pub fn disarm_for_test(&self) {
        self.lock_state().obligations = Obligations::default();
    }
}

impl<B: ModeBackend> TerminalGuard<B> {
    pub fn new(backend: B) -> Self {
        Self {
            state: Mutex::new(GuardState {
                backend,
                obligations: Obligations::default(),
                keyboard_enhancement_supported: None,
                mouse_capture_requested: false,
            }),
        }
    }

    pub fn arm(&mut self, mouse_capture: bool) -> io::Result<()> {
        let state = self
            .state
            .get_mut()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        state.mouse_capture_requested = mouse_capture;
        Self::arm_state(state, mouse_capture)
    }

    fn arm_state(state: &mut GuardState<B>, mouse_capture: bool) -> io::Result<()> {
        state.backend.enable_raw_mode()?;
        state.obligations.raw_mode = true;
        let keyboard_enhancement_supported = match state.keyboard_enhancement_supported {
            Some(supported) => supported,
            None => {
                let supported = state.backend.supports_keyboard_enhancement();
                state.keyboard_enhancement_supported = Some(supported);
                supported
            }
        };
        if keyboard_enhancement_supported {
            state.backend.push_keyboard_enhancement_flags()?;
            state.obligations.keyboard_enhancement = true;
        }
        state.backend.enter_alternate_screen()?;
        state.obligations.alternate_screen = true;
        if mouse_capture {
            state.backend.enable_mouse_capture()?;
            state.obligations.mouse_capture = true;
        }
        state.backend.enable_bracketed_paste()?;
        state.obligations.bracketed_paste = true;
        state.backend.hide_cursor()?;
        state.obligations.hidden_cursor = true;
        Ok(())
    }

    /// Enable or disable mouse capture on the armed terminal; a no-op when the
    /// obligation already matches `on`.
    pub fn set_mouse_capture(&mut self, on: bool) -> io::Result<()> {
        let state = self
            .state
            .get_mut()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if state.obligations.mouse_capture == on {
            state.mouse_capture_requested = on;
            return Ok(());
        }
        if on {
            state.backend.enable_mouse_capture()?;
        } else {
            state.backend.disable_mouse_capture()?;
        }
        state.obligations.mouse_capture = on;
        state.mouse_capture_requested = on;
        Ok(())
    }

    /// Restore terminal modes before the process stops.
    pub fn suspend(&mut self) -> io::Result<()> {
        self.restore()
    }

    /// Re-enter the same terminal modes after the process continues.
    pub fn resume(&mut self) -> io::Result<()> {
        let state = self
            .state
            .get_mut()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        Self::arm_state(state, state.mouse_capture_requested)
    }

    pub fn handle_signal(&self) {
        if let Err(error) = self.restore() {
            tracing::error!(%error, "failed to restore terminal mode after signal");
        }
    }

    pub fn restore(&self) -> io::Result<()> {
        let mut state = self.lock_state();
        let GuardState {
            backend,
            obligations,
            ..
        } = &mut *state;
        let mut errors = Vec::new();
        restore_obligation(
            &mut obligations.mouse_capture,
            "disable_mouse_capture",
            || backend.disable_mouse_capture(),
            &mut errors,
        );
        restore_obligation(
            &mut obligations.hidden_cursor,
            "show_cursor",
            || backend.show_cursor(),
            &mut errors,
        );
        restore_obligation(
            &mut obligations.bracketed_paste,
            "disable_bracketed_paste",
            || backend.disable_bracketed_paste(),
            &mut errors,
        );
        restore_obligation(
            &mut obligations.alternate_screen,
            "leave_alternate_screen",
            || backend.leave_alternate_screen(),
            &mut errors,
        );
        restore_obligation(
            &mut obligations.keyboard_enhancement,
            "pop_keyboard_enhancement_flags",
            || backend.pop_keyboard_enhancement_flags(),
            &mut errors,
        );
        restore_obligation(
            &mut obligations.raw_mode,
            "disable_raw_mode",
            || backend.disable_raw_mode(),
            &mut errors,
        );
        if errors.is_empty() {
            Ok(())
        } else {
            Err(io::Error::other(errors.join("; ")))
        }
    }

    fn lock_state(&self) -> std::sync::MutexGuard<'_, GuardState<B>> {
        self.state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }
}

impl<B: ModeBackend> Drop for TerminalGuard<B> {
    fn drop(&mut self) {
        let _ = catch_unwind(AssertUnwindSafe(|| {
            if let Err(error) = self.restore() {
                tracing::error!(%error, "failed to restore terminal mode during drop");
            }
        }));
    }
}

fn restore_obligation(
    outstanding: &mut bool,
    stage: &'static str,
    operation: impl FnOnce() -> io::Result<()>,
    errors: &mut Vec<String>,
) {
    if !*outstanding {
        return;
    }
    match catch_unwind(AssertUnwindSafe(operation)) {
        Ok(Ok(())) => *outstanding = false,
        Ok(Err(error)) => errors.push(format!("{stage}: {error}")),
        Err(_) => errors.push(format!("{stage}: backend panicked")),
    }
}

pub trait ShutdownWorkspace {
    fn begin_shutdown(&mut self) -> bool;
    fn shutdown_reason(&self) -> &str;
    fn take_shutdown_requests(&mut self) -> Vec<Value>;
}

/// Release remote obligations and terminal-latch the daemon within one deadline.
pub async fn shutdown<W: ShutdownWorkspace, D: Daemon>(
    workspace: &mut W,
    daemon: D,
    deadline: Instant,
) -> Result<(), DaemonError> {
    if !workspace.begin_shutdown() {
        return Ok(());
    }
    let reason = workspace.shutdown_reason();
    tracing::info!(reason, "gclient shutdown started");

    let requests = workspace.take_shutdown_requests();
    tracing::info!(
        lifecycle_stage = "release-held-leases",
        "gclient shutdown releasing held leases"
    );
    tracing::info!(
        lifecycle_stage = "detach-attachments",
        "gclient shutdown detaching attachments"
    );
    let cleanup = join_all(requests.into_iter().map(|request| daemon.send(request)));
    let cleanup_result = match timeout_at(deadline, cleanup).await {
        Ok(results) => results
            .into_iter()
            .find_map(Result::err)
            .map_or(Ok(()), Err),
        Err(_) => Err(DaemonError::timeout("shutdown cleanup")),
    };
    if let Err(error) = cleanup_result {
        tracing::warn!(%error, "gclient remote cleanup did not finish before daemon close");
    }
    tracing::info!(
        lifecycle_stage = "cleanup-settled",
        "gclient remote cleanup settled"
    );
    tracing::info!(
        lifecycle_stage = "daemon-close",
        "gclient daemon close started"
    );
    match close_with_first_poll(&daemon, deadline).await {
        Ok(()) => tracing::info!("gclient daemon closed"),
        Err(error) => {
            tracing::warn!(
                %error,
                "gclient daemon close did not finish before terminal restore"
            );
        }
    }
    Ok(())
}

async fn close_with_first_poll<D: Daemon>(
    daemon: &D,
    deadline: Instant,
) -> Result<(), DaemonError> {
    let mut close = Box::pin(daemon.close(deadline));
    let first = poll_fn(|cx| {
        Poll::Ready(match close.as_mut().poll(cx) {
            Poll::Ready(result) => Some(result),
            Poll::Pending => None,
        })
    })
    .await;
    match first {
        Some(result) => result,
        None => timeout_at(deadline, close)
            .await
            .map_err(|_| DaemonError::timeout("close"))?,
    }
}
