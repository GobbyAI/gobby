//! Host-terminal mode lifecycle around Unix job-control suspension.

#[cfg(unix)]
pub(super) struct SuspendSignal(tokio::signal::unix::Signal);

#[cfg(unix)]
impl SuspendSignal {
    pub(super) fn new() -> std::io::Result<Self> {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::from_raw(libc::SIGTSTP))
            .map(Self)
    }

    pub(super) async fn recv(&mut self) {
        let _ = self.0.recv().await;
    }
}

#[cfg(not(unix))]
pub(super) struct SuspendSignal;

#[cfg(not(unix))]
impl SuspendSignal {
    pub(super) fn new() -> std::io::Result<Self> {
        Ok(Self)
    }

    pub(super) async fn recv(&mut self) {
        std::future::pending::<()>().await;
    }
}

#[cfg(unix)]
pub(super) fn suspend_process() -> std::io::Result<()> {
    // SAFETY: raising SIGSTOP has no memory-safety preconditions. It stops
    // this process until the shell sends SIGCONT, then returns here.
    if unsafe { libc::raise(libc::SIGSTOP) } == 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

#[cfg(not(unix))]
pub(super) fn suspend_process() -> std::io::Result<()> {
    Ok(())
}
