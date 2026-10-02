//! SIGTERM across an in-process upgrade (plan gterm-host-handover 1.3).
//!
//! `block` runs before the host starts a thread, so every thread inherits
//! SIGTERM blocked. XNU holds a SIGTERM that every thread blocks on the main
//! thread and delivers it only when that thread unmasks, so the main thread
//! takes SIGTERM: `serve` runs the host on its own thread and jobs on main.
//! `start` installs the handler, which sets the host's `draining`, and then
//! has main unmask. An upgrade execs on main through `blocked`, which masks
//! SIGTERM there as well and only then rechecks `draining`: a SIGTERM before
//! the mask aborts the attempt, and one after it stays pending across
//! `execve` until the new image's own `start`, or across a failed one until
//! the job ends.

use std::ffi::CString;
use std::io;
use std::os::fd::RawFd;
use std::os::unix::ffi::OsStrExt;
use std::path::Path;
use std::sync::atomic::AtomicBool;
use std::sync::{mpsc, Arc, OnceLock};

type Job = Box<dyn FnOnce() + Send>;

/// Queues jobs for the main thread, which takes SIGTERM.
static OWNER: OnceLock<mpsc::Sender<Job>> = OnceLock::new();

/// Blocks SIGTERM on the calling thread and on every thread it starts.
pub(crate) fn block() {
    crate::platform::set_sigterm_mask(libc::SIG_BLOCK);
}

/// Unblocks SIGTERM on the calling thread. A child inherits the mask, so
/// every child the host starts unblocks it (`platform::unmask_sigterm`),
/// except the gterm images it probes or execs, which block it themselves.
pub(crate) fn unblock() {
    crate::platform::set_sigterm_mask(libc::SIG_UNBLOCK);
}

/// Runs `host` on its own thread, then exits with its result, while the
/// calling main thread runs jobs, with SIGTERM masked until `start`.
pub(crate) fn serve(host: impl FnOnce() + Send + 'static) -> ! {
    let (jobs, queue) = mpsc::channel::<Job>();
    // `run_host` serves once, so this sets it.
    let _ = OWNER.set(jobs);
    std::thread::Builder::new()
        .name("gterm-host".into())
        .spawn(move || {
            // The panic has printed; exit as a panicking main would, since
            // main never returns to end the process.
            let code = match std::panic::catch_unwind(std::panic::AssertUnwindSafe(host)) {
                Ok(()) => 0,
                Err(_) => 101,
            };
            std::process::exit(code)
        })
        .expect("host thread");
    for job in queue {
        job();
    }
    unreachable!("OWNER keeps the job queue open")
}

/// Installs the handler that sets `draining`, then has main unmask; a
/// SIGTERM already pending arrives then.
pub(crate) fn start(draining: &Arc<AtomicBool>) -> io::Result<()> {
    signal_hook::flag::register(libc::SIGTERM, Arc::clone(draining))?;
    if let Some(owner) = OWNER.get() {
        let _ = owner.send(Box::new(unblock));
    }
    Ok(())
}

/// Runs `job` on main with SIGTERM masked there too, so any later SIGTERM
/// stays pending until the job ends; `None` without an owner.
pub(crate) async fn blocked<T: Send + 'static>(
    job: impl FnOnce() -> T + Send + 'static,
) -> Option<T> {
    let (reply, answer) = tokio::sync::oneshot::channel();
    let job: Job = Box::new(move || {
        block();
        let _ = reply.send(job());
        unblock();
    });
    OWNER.get()?.send(job).ok()?;
    answer.await.ok()
}

/// Step 6: clears close-on-exec on every carried descriptor and execs the
/// pin with the running environment. `commit` runs after every allocation,
/// just before the first descriptor changes; when it refuses, nothing changes
/// and this returns `None`. Returns the errno only when `execve` fails, with
/// close-on-exec restored. `libc::execv` rather than `CommandExt::exec`, which
/// resets SIGPIPE first and would leave it changed in a host that keeps
/// running after a failure.
pub(crate) fn exec(
    program: &Path,
    argv: &[String],
    state_path: &Path,
    fds: &[RawFd],
    commit: impl FnOnce() -> bool,
) -> Option<i32> {
    let arg = |bytes: &[u8]| CString::new(bytes).ok();
    let program_c = arg(program.as_os_str().as_bytes());
    let args: Option<Vec<CString>> = argv
        .iter()
        .map(|value| arg(value.as_bytes()))
        .chain([
            arg(b"--resume-state"),
            arg(state_path.as_os_str().as_bytes()),
        ])
        .collect();
    let (Some(program_c), Some(args)) = (program_c, args) else {
        return Some(libc::EINVAL);
    };
    let mut pointers: Vec<*const libc::c_char> = args.iter().map(|value| value.as_ptr()).collect();
    pointers.push(std::ptr::null());
    let set_cloexec = |cloexec: bool| {
        for &fd in fds {
            // SAFETY: F_SETFD changes only this descriptor's close-on-exec flag.
            unsafe {
                libc::fcntl(
                    fd,
                    libc::F_SETFD,
                    if cloexec { libc::FD_CLOEXEC } else { 0 },
                )
            };
        }
    };
    if !commit() {
        return None;
    }
    set_cloexec(false);
    // SAFETY: both arrays are NUL-terminated and outlive the call, which
    // returns only on failure.
    unsafe { libc::execv(program_c.as_ptr(), pointers.as_ptr()) };
    let errno = std::io::Error::last_os_error()
        .raw_os_error()
        .unwrap_or(libc::EIO);
    set_cloexec(true);
    Some(errno)
}
