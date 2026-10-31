//! Closes descriptors inherited from the test runner before the harness starts.
//!
//! macOS has no `pipe2`, so a pipe exists briefly before `FD_CLOEXEC` is set. A test
//! process forked in that window inherits a sibling test's stdio pipe and keeps it
//! open for its whole life, and nextest flags the sibling as leaked. Closing the
//! inherited descriptors at load time, before any test runs, ends that copy at once.

/// Caps the descriptor scan. Pipes created by the runner sit at the low end of the
/// table, so the cap bounds startup cost without skipping them.
#[cfg(target_os = "macos")]
const MAX_SCANNED_FDS: libc::c_int = 65_536;

#[cfg(target_os = "macos")]
extern "C" fn close_inherited_descriptors() {
    // SAFETY: sysconf only reads a process limit and takes no pointers.
    let limit = unsafe { libc::sysconf(libc::_SC_OPEN_MAX) };
    let limit = match limit {
        ..=0 => MAX_SCANNED_FDS,
        limit => limit.min(libc::c_long::from(MAX_SCANNED_FDS)) as libc::c_int,
    };
    // Descriptors 0-2 are stdio and stay open.
    for fd in 3..limit {
        // SAFETY: close takes only a descriptor number; an unopened number returns EBADF.
        unsafe { libc::close(fd) };
    }
}

#[cfg(target_os = "macos")]
#[used]
#[unsafe(link_section = "__DATA,__mod_init_func")]
static CLOSE_INHERITED_DESCRIPTORS: extern "C" fn() = close_inherited_descriptors;
