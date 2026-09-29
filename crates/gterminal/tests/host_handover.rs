//! Host handover (plan gterm-host-handover 1.2): frozen reaping, the resume
//! primitive, and the Stage/Commit restore of a carried state file.

#![cfg(unix)]

use std::path::Path;
use std::time::{Duration, Instant};

use gobby_terminal::pane::{ChildExit, PaneLaunchEnv, PaneRuntime};
use gobby_terminal::terminal_theme::TerminalTheme;

const WAIT: Duration = Duration::from_secs(10);

fn spawn_pane(cwd: &Path, script: &str) -> PaneRuntime {
    PaneRuntime::spawn_argv_command(
        24,
        80,
        cwd.to_path_buf(),
        &["sh".to_string(), "-c".to_string(), script.to_string()],
        &PaneLaunchEnv::default(),
        1 << 20,
        TerminalTheme::default(),
        None,
    )
    .expect("spawn pane")
}

/// Whether `pid` has exited and is still waiting to be reaped.
fn is_zombie(pid: u32) -> bool {
    // SAFETY: siginfo_t is plain data; waitid fills it or leaves it zeroed.
    let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
    // SAFETY: info is a valid out-pointer; WNOWAIT leaves the child unreaped.
    let rc = unsafe {
        libc::waitid(
            libc::P_PID,
            pid as libc::id_t,
            &mut info,
            libc::WEXITED | libc::WNOWAIT | libc::WNOHANG,
        )
    };
    // SAFETY: waitid succeeded, so si_pid is initialized.
    rc == 0 && unsafe { info.si_pid() } == pid as libc::pid_t
}

/// Whether `pid` is no longer this process's child to reap.
fn is_reaped(pid: u32) -> bool {
    let mut status = 0;
    // SAFETY: status is a valid out-pointer; WNOHANG never blocks.
    let rc = unsafe { libc::waitpid(pid as libc::pid_t, &mut status, libc::WNOHANG) };
    rc == -1 && std::io::Error::last_os_error().raw_os_error() == Some(libc::ECHILD)
}

fn wait_until(what: &str, mut ready: impl FnMut() -> bool) {
    let deadline = Instant::now() + WAIT;
    while !ready() {
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn rollback_reaps_exit_seen_while_frozen() {
    let dir = tempfile::tempdir().expect("tempdir");
    let runtime = spawn_pane(
        dir.path(),
        "while [ ! -e go ]; do sleep 0.02; done; exit 7",
    );
    let pid = runtime.child_pid().expect("child pid");
    let watch = runtime.child_exit_watch().expect("exit watch");

    runtime.freeze_reaping();
    std::fs::write(dir.path().join("go"), b"").expect("release child");
    wait_until("the frozen child to exit", || is_zombie(pid));
    std::thread::sleep(Duration::from_millis(200));
    assert!(is_zombie(pid), "a frozen pane leaves its exited child unreaped");
    assert_eq!(runtime.child_exit(), None, "no exit is recorded while frozen");

    runtime.unfreeze_reaping();
    let exit = tokio::time::timeout(WAIT, watch.wait())
        .await
        .expect("exit delivered after unfreeze");
    let expected = ChildExit {
        exit_code: Some(7),
        signal: None,
    };
    assert_eq!(exit, Some(expected.clone()), "the real status is recorded");
    assert_eq!(runtime.child_exit(), Some(expected));
    assert!(is_reaped(pid), "unfreeze reaped the child exactly once");
}
