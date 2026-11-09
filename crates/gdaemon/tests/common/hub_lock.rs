//! Hold deadline for the hub-wide gdaemon lease lock that the lease and lifecycle binaries share.

use std::sync::mpsc::{RecvTimeoutError, Sender, channel};
use std::time::Duration;

/// Longest a test body may hold the hub lock. Each body waits on bounded children, so none
/// should approach this. The watchdog covers a body that blocks in a synchronous call, where no
/// in-runtime timeout can fire.
pub const HOLD_DEADLINE: Duration = Duration::from_secs(300);

/// Dropping the guard cancels the watchdog.
pub struct HoldWatchdog {
    _cancel: Sender<()>,
}

/// Exit the test process once `deadline` passes, unless the guard drops first.
///
/// An exited process closes its hub connection, and the idle backend holding the advisory lock
/// then releases it to queued runs. A test blocked in a synchronous spawn never reaches its own
/// timeouts, so without this one body could hold the lock for the rest of the run.
pub fn arm(deadline: Duration) -> HoldWatchdog {
    let (cancel, cancelled) = channel::<()>();
    std::thread::spawn(move || {
        if cancelled.recv_timeout(deadline) == Err(RecvTimeoutError::Timeout) {
            eprintln!(
                "gdaemon-lease-tests lock held past {deadline:?}; exiting so the hub releases it"
            );
            std::process::exit(101);
        }
    });
    HoldWatchdog { _cancel: cancel }
}
