//! The pane's sole child waiter. It sees the exit with `waitid(WNOWAIT)`,
//! which leaves the child unreaped, then reaps under a per-pane lock that a
//! handover freezes, so an exit between capture and exec stays a zombie for
//! the next image instead of losing its status.

#![cfg(unix)]

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};

use tokio::sync::Notify;
use tracing::error;

use super::runtime::ChildExit;

#[derive(Default)]
struct ReapLock {
    frozen: bool,
    exit_pending: bool,
}

pub(crate) struct ChildReaper {
    pid: libc::pid_t,
    lock: Mutex<ReapLock>,
    pub(crate) result: Arc<Mutex<Option<ChildExit>>>,
    pub(crate) completed: Arc<AtomicBool>,
    pub(crate) notify: Arc<Notify>,
}

impl ChildReaper {
    pub(crate) fn new(pid: u32) -> Arc<Self> {
        Arc::new(Self {
            pid: pid as libc::pid_t,
            lock: Mutex::new(ReapLock::default()),
            result: Arc::new(Mutex::new(None)),
            completed: Arc::new(AtomicBool::new(false)),
            notify: Arc::new(Notify::new()),
        })
    }

    /// Starts the blocking waiter.
    pub(crate) fn start(self: &Arc<Self>) {
        let reaper = Arc::clone(self);
        tokio::task::spawn_blocking(move || match reaper.wait_exited() {
            Ok(()) => reaper.exit_seen(),
            Err(err) => {
                error!(pid = reaper.pid, err = %err, "pane child wait failed");
                reaper.finish(None);
            }
        });
    }

    /// Records an exit an earlier image reaped but never delivered.
    pub(crate) fn record(&self, exit: ChildExit) {
        self.finish(Some(exit));
    }

    pub(crate) fn freeze(&self) {
        self.lock().frozen = true;
    }

    /// Unfreezes and reaps an exit seen while frozen.
    pub(crate) fn unfreeze(&self) {
        let mut lock = self.lock();
        lock.frozen = false;
        if std::mem::take(&mut lock.exit_pending) {
            self.reap();
        }
    }

    fn lock(&self) -> MutexGuard<'_, ReapLock> {
        self.lock
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn wait_exited(&self) -> std::io::Result<()> {
        loop {
            // SAFETY: siginfo_t is plain data that waitid fills.
            let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
            // SAFETY: info is a valid out-pointer; WNOWAIT leaves the child
            // for the reap below.
            let rc = unsafe {
                libc::waitid(
                    libc::P_PID,
                    self.pid as libc::id_t,
                    &mut info,
                    libc::WEXITED | libc::WNOWAIT,
                )
            };
            if rc == 0 {
                return Ok(());
            }
            let err = std::io::Error::last_os_error();
            if err.kind() != std::io::ErrorKind::Interrupted {
                return Err(err);
            }
        }
    }

    fn exit_seen(&self) {
        let mut lock = self.lock();
        if lock.frozen {
            lock.exit_pending = true;
            return;
        }
        self.reap();
    }

    /// Reaps the exited child; the caller holds the reap lock.
    fn reap(&self) {
        let mut status = 0;
        let waited = loop {
            // SAFETY: status is a valid out-pointer; the child has exited, so
            // WNOHANG returns at once.
            let waited = unsafe { libc::waitpid(self.pid, &mut status, libc::WNOHANG) };
            if waited != -1
                || std::io::Error::last_os_error().kind() != std::io::ErrorKind::Interrupted
            {
                break waited;
            }
        };
        if waited != self.pid {
            error!(
                pid = self.pid,
                err = %std::io::Error::last_os_error(),
                "pane child reap failed"
            );
            self.finish(None);
            return;
        }
        self.finish(exit_from_status(status));
    }

    fn finish(&self, exit: Option<ChildExit>) {
        if let (Some(exit), Ok(mut retained)) = (exit, self.result.lock()) {
            *retained = Some(exit);
        }
        self.completed.store(true, Ordering::Release);
        self.notify.notify_waiters();
    }
}

fn exit_from_status(status: libc::c_int) -> Option<ChildExit> {
    if libc::WIFEXITED(status) {
        return Some(ChildExit {
            exit_code: Some(libc::WEXITSTATUS(status) as u32),
            signal: None,
        });
    }
    if !libc::WIFSIGNALED(status) {
        return None;
    }
    let signal = libc::WTERMSIG(status);
    // SAFETY: strsignal returns a static or thread-local string, or null.
    let name = unsafe { libc::strsignal(signal) };
    let signal = if name.is_null() {
        format!("Signal {signal}")
    } else {
        // SAFETY: name is a non-null NUL-terminated C string.
        unsafe { std::ffi::CStr::from_ptr(name) }
            .to_string_lossy()
            .into_owned()
    };
    Some(ChildExit {
        exit_code: None,
        signal: Some(signal),
    })
}
