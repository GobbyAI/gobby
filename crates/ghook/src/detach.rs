//! Process detachment, cross-platform.
//!
//! Unix: single `setsid(2)` to escape the controlling terminal and the
//! parent's process group. Mirrors `start_new_session=True` in the
//! dispatcher (`hook_dispatcher.py:697`) — no double-fork, no daemonize
//! tricks.
//!
//! Windows: `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` would be the
//! parallel, but those flags apply at `CreateProcess` time, not to the
//! already-running current process. For a self-detaching binary, the
//! closest honest equivalent is `FreeConsole`. Keep the surface consistent
//! (calling `detach()` on Windows disables console I/O) but don't pretend
//! we're doing more than we are.

/// Detach the current process from its controlling TTY and process group.
///
/// Unix: `setsid()`. On failure (already a session leader, etc.) we log
/// nothing and continue — the dispatcher's Python parallel is also
/// best-effort (subprocess `start_new_session=True` just requests the
/// child leads a session).
///
/// Windows: `FreeConsole()` — best effort, no-op if not attached.
pub fn detach() {
    #[cfg(unix)]
    {
        // SAFETY: setsid is always safe to call. It fails only when the
        // caller is already a process-group leader, in which case we just
        // carry on.
        unsafe {
            libc::setsid();
        }
    }
    #[cfg(windows)]
    {
        // SAFETY: FreeConsole is safe to call even with no attached console.
        // Edition 2024 requires extern blocks to be marked unsafe.
        unsafe extern "system" {
            fn FreeConsole() -> i32;
        }
        unsafe {
            FreeConsole();
        }
    }
}
