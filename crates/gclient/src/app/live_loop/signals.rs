//! Process-signal listeners that steer the interactive loop.

use super::suspend::SuspendSignal;
use super::Workspace;
use crate::daemon::LiveDaemon;
use crate::frame_source::FrameError;

/// Register the process signal handlers the loop selects on. A failure to
/// register any listener is fatal to the launch, exactly as before: the loop
/// latches its exit and carries the error out.
pub(super) fn install(
    workspace: &mut Workspace<LiveDaemon>,
    loop_error: &mut Option<FrameError>,
) -> SignalSet {
    SignalSet {
        exit: install_exit_signals(workspace, loop_error),
        resize: install_resize_signal(workspace, loop_error),
        suspend: install_suspend_signal(workspace, loop_error),
    }
}

/// The three listeners, each `None` when its registration failed.
pub(super) struct SignalSet {
    pub(super) exit: Option<ExitSignals>,
    pub(super) resize: Option<ResizeSignal>,
    pub(super) suspend: Option<SuspendSignal>,
}

pub(super) async fn recv_exit_signal(signals: &mut Option<ExitSignals>) -> &'static str {
    match signals {
        Some(signals) => signals.recv().await,
        None => std::future::pending().await,
    }
}

pub(super) async fn recv_resize_signal(signal: &mut Option<ResizeSignal>) {
    match signal {
        Some(signal) => signal.recv().await,
        None => std::future::pending().await,
    }
}

pub(super) async fn recv_suspend_signal(signal: &mut Option<SuspendSignal>) {
    match signal {
        Some(signal) => signal.recv().await,
        None => std::future::pending().await,
    }
}

#[cfg(unix)]
pub(super) struct ExitSignals {
    interrupt: tokio::signal::unix::Signal,
    terminate: tokio::signal::unix::Signal,
    hangup: tokio::signal::unix::Signal,
}

#[cfg(unix)]
impl ExitSignals {
    fn new() -> std::io::Result<Self> {
        use tokio::signal::unix::{signal, SignalKind};

        Ok(Self {
            interrupt: signal(SignalKind::interrupt())?,
            terminate: signal(SignalKind::terminate())?,
            hangup: signal(SignalKind::hangup())?,
        })
    }

    async fn recv(&mut self) -> &'static str {
        loop {
            tokio::select! {
                _ = self.interrupt.recv() => return "SIGINT",
                _ = self.terminate.recv() => return "SIGTERM",
                // Registered so the default disposition (terminate) stays
                // off, then ignored: a hangup is not a reason to drop the
                // window, and a terminal that really went away still ends
                // the loop through input EOF or the failed draw.
                _ = self.hangup.recv() => {
                    tracing::info!(
                        lifecycle_stage = "sighup-ignored",
                        "SIGHUP ignored; the terminal is still attached"
                    );
                }
            }
        }
    }
}

#[cfg(not(unix))]
pub(super) struct ExitSignals;

#[cfg(not(unix))]
impl ExitSignals {
    fn new() -> std::io::Result<Self> {
        Ok(Self)
    }

    async fn recv(&mut self) -> &'static str {
        let _ = tokio::signal::ctrl_c().await;
        "SIGINT"
    }
}

#[cfg(unix)]
pub(super) struct ResizeSignal(tokio::signal::unix::Signal);

#[cfg(unix)]
impl ResizeSignal {
    fn new() -> std::io::Result<Self> {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::window_change()).map(Self)
    }

    async fn recv(&mut self) {
        let _ = self.0.recv().await;
    }
}

#[cfg(not(unix))]
pub(super) struct ResizeSignal;

#[cfg(not(unix))]
impl ResizeSignal {
    fn new() -> std::io::Result<Self> {
        Ok(Self)
    }

    async fn recv(&mut self) {
        std::future::pending::<()>().await;
    }
}

fn install_exit_signals(
    workspace: &mut Workspace<LiveDaemon>,
    loop_error: &mut Option<FrameError>,
) -> Option<ExitSignals> {
    match ExitSignals::new() {
        Ok(signals) => Some(signals),
        Err(error) => {
            workspace.latch_exit(error.to_string());
            *loop_error = Some(FrameError::Other(error.to_string()));
            None
        }
    }
}

fn install_resize_signal(
    workspace: &mut Workspace<LiveDaemon>,
    loop_error: &mut Option<FrameError>,
) -> Option<ResizeSignal> {
    match ResizeSignal::new() {
        Ok(signal) => Some(signal),
        Err(error) => {
            workspace.latch_exit(error.to_string());
            *loop_error = Some(FrameError::Other(error.to_string()));
            None
        }
    }
}

fn install_suspend_signal(
    workspace: &mut Workspace<LiveDaemon>,
    loop_error: &mut Option<FrameError>,
) -> Option<SuspendSignal> {
    match SuspendSignal::new() {
        Ok(signal) => Some(signal),
        Err(error) => {
            workspace.latch_exit(error.to_string());
            *loop_error = Some(FrameError::Other(error.to_string()));
            None
        }
    }
}
