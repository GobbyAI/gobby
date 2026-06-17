mod cli;
mod dispatch;

fn main() -> std::process::ExitCode {
    reset_sigpipe();
    dispatch::run_with_exit_code()
}

/// Restore the default `SIGPIPE` disposition so a closed stdout (e.g. piping to
/// `head`) terminates the process quietly instead of panicking inside `println!`.
///
/// The Rust runtime ignores `SIGPIPE` at startup, turning a closed downstream
/// pipe into an `EPIPE` that `print!`/`println!` escalate to a panic. Resetting
/// it to `SIG_DFL` makes every print site behave like a standard Unix CLI.
#[cfg(unix)]
fn reset_sigpipe() {
    // SAFETY: called once at startup before any threads are spawned; resetting a
    // signal to its default disposition is async-signal-safe.
    unsafe {
        libc::signal(libc::SIGPIPE, libc::SIG_DFL);
    }
}

#[cfg(not(unix))]
fn reset_sigpipe() {}
