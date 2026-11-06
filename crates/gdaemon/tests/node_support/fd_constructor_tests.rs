//! Inherited descriptors must close before the node-agent test harness starts.
use anyhow::{Context, Result};
use std::{
    fs::File,
    io::{BufRead, BufReader, Read, Write},
    os::fd::{AsRawFd, FromRawFd, OwnedFd},
    process::{Command, Stdio},
    sync::mpsc,
    thread,
    time::Duration,
};

const READY: &str = "FD_CONSTRUCTOR_CHILD_READY";

#[test]
#[ignore = "subprocess helper: stays alive until its parent releases stdin or kills it"]
fn inherited_fd_sleeper() -> Result<()> {
    let mut stdout = std::io::stdout().lock();
    writeln!(stdout, "{READY}")?;
    stdout.flush()?;
    std::io::stdin().read_exact(&mut [0])?;
    Ok(())
}

#[test]
fn constructor_closes_inherited_pipe_before_test_harness() -> Result<()> {
    let mut descriptors = [-1; 2];
    // SAFETY: pipe receives storage for exactly two descriptors. Success transfers
    // each fresh descriptor into one OwnedFd below, which closes it exactly once.
    if unsafe { libc::pipe(descriptors.as_mut_ptr()) } != 0 {
        return Err(std::io::Error::last_os_error()).context("create inherited pipe");
    }
    // SAFETY: successful pipe returned two distinct, valid owned descriptors.
    let reader = unsafe { OwnedFd::from_raw_fd(descriptors[0]) };
    // SAFETY: the write end is distinct from reader and has no other Rust owner.
    let writer = unsafe { OwnedFd::from_raw_fd(descriptors[1]) };
    // SAFETY: writer remains valid for the duration of this non-mutating fcntl.
    let flags = unsafe { libc::fcntl(writer.as_raw_fd(), libc::F_GETFD) };
    assert!(flags >= 0, "inspect inherited pipe flags");
    assert_eq!(flags & libc::FD_CLOEXEC, 0, "pipe must survive exec");

    let mut child = Command::new(std::env::current_exe()?)
        .args([
            "--exact",
            "node_agents::fd_constructor_tests::inherited_fd_sleeper",
            "--ignored",
            "--nocapture",
            "--quiet",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .context("spawn inheriting test harness")?;
    drop(writer);
    let stdout = child.stdout.take().context("child stdout")?;
    let (ready_tx, ready_rx) = mpsc::channel();
    let (eof_tx, eof_rx) = mpsc::channel();
    let observer = thread::spawn(move || {
        let ready = BufReader::new(stdout)
            .lines()
            .any(|line| line.is_ok_and(|line| line == READY));
        let _ = ready_tx.send(ready);
        let _ = eof_tx.send(File::from(reader).read(&mut [0]));
    });
    let ready = ready_rx.recv_timeout(Duration::from_secs(30));
    let eof = if matches!(&ready, Ok(true)) {
        Some(eof_rx.recv_timeout(Duration::from_secs(2)))
    } else {
        None
    };
    let running = child.try_wait();
    // Clean up even on RED: killing the child also unblocks the pipe observer.
    let _ = child.kill();
    child.wait().context("reap inherited-fd child")?;
    observer
        .join()
        .map_err(|_| anyhow::anyhow!("pipe observer panicked"))?;
    assert!(
        matches!(&ready, Ok(true)),
        "sleeper did not become ready: {ready:?}"
    );
    assert!(
        matches!(&running, Ok(None)),
        "sleeper exited before EOF check: {running:?}"
    );
    assert!(
        matches!(&eof, Some(Ok(Ok(0)))),
        "inherited pipe must reach EOF within 2s while child stays alive: {eof:?}"
    );
    Ok(())
}
