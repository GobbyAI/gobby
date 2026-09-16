//! Unix PTY spawn with gate and exec-status pipes.

#![cfg(unix)]

use std::io;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::Deserialize;
use tokio::io::unix::AsyncFd;

use super::gate::{errno_name, GATE_FD, PTY_FD, STATUS_FD};
#[cfg(debug_assertions)]
use crate::pane::ChildExitWatch;
use crate::pane::PaneRuntime;
use crate::terminal_theme::TerminalTheme;

const MIN_INHERITED_FD: RawFd = 10;
const MAX_STATUS_BYTES: usize = 4096;

#[derive(Debug, Deserialize)]
pub struct ExecFailure {
    pub code: String,
    pub detail: String,
    pub stage: String,
}

impl ExecFailure {
    fn from_io(stage: &str, error: io::Error) -> Self {
        let errno = error.raw_os_error().unwrap_or(libc::EIO);
        Self {
            code: errno_name(errno).to_string(),
            detail: io::Error::from_raw_os_error(errno).to_string(),
            stage: stage.to_string(),
        }
    }
}

#[derive(Debug)]
pub enum CommitResult {
    Committed,
    ExecFailed(ExecFailure),
    ExecTimeout,
    MalformedStatus,
}

pub struct PreparedChild {
    pub runtime: PaneRuntime,
    pub pid: u32,
    pub pgid: i32,
    pub start_time: f64,
    gate_writer: Option<OwnedFd>,
    status_reader: Option<AsyncFd<OwnedFd>>,
    #[cfg(debug_assertions)]
    wait_for_child_exit_before_status: bool,
}

pub struct PreparedCommit {
    status_reader: Option<AsyncFd<OwnedFd>>,
    #[cfg(debug_assertions)]
    wait_for_child_exit_before_status: bool,
    #[cfg(debug_assertions)]
    exit_watch: Option<ChildExitWatch>,
}

impl PreparedChild {
    pub fn begin_commit(&mut self) -> Result<PreparedCommit, CommitResult> {
        if let Some(gate) = self.gate_writer.take() {
            let write_result = write_gate(gate.as_raw_fd());
            drop(gate);
            if let Err(error) = write_result {
                return Err(CommitResult::ExecFailed(ExecFailure::from_io(
                    "gate", error,
                )));
            }
        }
        Ok(PreparedCommit {
            status_reader: self.status_reader.take(),
            #[cfg(debug_assertions)]
            wait_for_child_exit_before_status: self.wait_for_child_exit_before_status,
            #[cfg(debug_assertions)]
            exit_watch: self.runtime.child_exit_watch(),
        })
    }
}

impl PreparedCommit {
    pub async fn finish(self, deadline: Duration) -> CommitResult {
        let status_deadline = tokio::time::Instant::now() + deadline;
        #[cfg(debug_assertions)]
        if self.wait_for_child_exit_before_status {
            if let Some(exit_watch) = self.exit_watch {
                let _ = tokio::time::timeout_at(status_deadline, exit_watch.wait()).await;
            }
        }
        let Some(status) = self.status_reader else {
            return CommitResult::Committed;
        };
        let bytes = match tokio::time::timeout_at(status_deadline, read_status(&status)).await {
            Ok(Ok(bytes)) => bytes,
            Ok(Err(_)) => return CommitResult::MalformedStatus,
            Err(_) => return CommitResult::ExecTimeout,
        };
        if bytes.is_empty() {
            return CommitResult::Committed;
        }
        match serde_json::from_slice(&bytes) {
            Ok(failure) => CommitResult::ExecFailed(failure),
            Err(_) => CommitResult::MalformedStatus,
        }
    }
}

impl Drop for PreparedChild {
    fn drop(&mut self) {
        tracing::debug!(pid = self.pid, "dropping prepared child");
        self.gate_writer.take();
        self.status_reader.take();
    }
}

pub fn spawn_prepared(
    rows: u16,
    cols: u16,
    cwd: &Path,
    argv: &[String],
    env: &[(String, String)],
    scrollback_limit_bytes: usize,
) -> io::Result<PreparedChild> {
    if argv.is_empty() {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "argv empty"));
    }

    let (pty_master, pty_slave) = open_pty(rows, cols)?;
    set_cloexec(pty_master.as_raw_fd())?;
    set_cloexec(pty_slave.as_raw_fd())?;
    let (gate_pipe_reader, gate_pipe_writer) = pipe_pair()?;
    let (status_pipe_reader, status_pipe_writer) = pipe_pair()?;

    let master = duplicate_high(&pty_master)?;
    let slave = duplicate_high(&pty_slave)?;
    let gate_reader = duplicate_high(&gate_pipe_reader)?;
    let gate_writer = duplicate_high(&gate_pipe_writer)?;
    let status_reader = duplicate_high(&status_pipe_reader)?;
    let status_writer = duplicate_high(&status_pipe_writer)?;
    drop(pty_master);
    drop(pty_slave);
    drop(gate_pipe_reader);
    drop(gate_pipe_writer);
    drop(status_pipe_reader);
    drop(status_pipe_writer);
    set_nonblocking(status_reader.as_raw_fd())?;

    let test_helper = std::env::var_os("GTERM_TEST_HELPER").filter(|value| value == "1");
    #[cfg(debug_assertions)]
    let wait_for_child_exit_before_status = test_helper.is_some()
        && env.iter().any(|(name, value)| {
            name == "GTERM_TEST_WAIT_FOR_CHILD_EXIT_BEFORE_STATUS" && value == "1"
        });
    let mut command = Command::new(std::env::current_exe()?);
    command
        .arg("gate")
        .arg("--")
        .args(argv)
        .current_dir(cwd)
        .env_remove("CODEX_THREAD_ID")
        .envs(env.iter().cloned())
        .env("TERM", "xterm-256color")
        .env("COLORTERM", "truecolor")
        .env(crate::GTERM_ENV_VAR, crate::GTERM_ENV_VALUE)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    if let Some(marker) = test_helper {
        command.env("GTERM_TEST_HELPER", marker);
    } else {
        command.env_remove("GTERM_TEST_HELPER");
    }

    let gate_reader_fd = gate_reader.as_raw_fd();
    let status_writer_fd = status_writer.as_raw_fd();
    let slave_fd = slave.as_raw_fd();
    unsafe {
        command.pre_exec(move || {
            duplicate_in_child(gate_reader_fd, GATE_FD)?;
            duplicate_in_child(status_writer_fd, STATUS_FD)?;
            duplicate_in_child(slave_fd, PTY_FD)?;
            Ok(())
        });
    }
    let mut child = command.spawn()?;
    let pid = child.id();
    drop(gate_reader);
    drop(status_writer);
    drop(slave);

    let runtime = match PaneRuntime::from_master_fd(
        rows,
        cols,
        scrollback_limit_bytes,
        TerminalTheme::default(),
        None,
        master,
        pid,
    ) {
        Ok(runtime) => runtime,
        Err(error) => {
            unsafe {
                libc::kill(pid as i32, libc::SIGKILL);
            }
            // No PaneRuntime waiter exists when construction fails.
            let _ = child.wait();
            return Err(error);
        }
    };
    drop(child);

    let start_time = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0);
    Ok(PreparedChild {
        runtime,
        pid,
        pgid: pid as i32,
        start_time,
        gate_writer: Some(gate_writer),
        status_reader: Some(AsyncFd::new(status_reader)?),
        #[cfg(debug_assertions)]
        wait_for_child_exit_before_status,
    })
}

fn open_pty(rows: u16, cols: u16) -> io::Result<(OwnedFd, OwnedFd)> {
    let mut master = -1;
    let mut slave = -1;
    let mut size = libc::winsize {
        ws_row: rows,
        ws_col: cols,
        ws_xpixel: 0,
        ws_ypixel: 0,
    };
    let result = unsafe {
        libc::openpty(
            &mut master,
            &mut slave,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut size,
        )
    };
    if result < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(unsafe {
        // SAFETY: successful `openpty` initialized two newly owned descriptors.
        (OwnedFd::from_raw_fd(master), OwnedFd::from_raw_fd(slave))
    })
}

fn pipe_pair() -> io::Result<(OwnedFd, OwnedFd)> {
    let mut fds = [-1, -1];
    if unsafe { libc::pipe(fds.as_mut_ptr()) } < 0 {
        return Err(io::Error::last_os_error());
    }
    let pair = unsafe {
        // SAFETY: successful `pipe` initialized two newly owned descriptors.
        (OwnedFd::from_raw_fd(fds[0]), OwnedFd::from_raw_fd(fds[1]))
    };
    set_cloexec(pair.0.as_raw_fd())?;
    set_cloexec(pair.1.as_raw_fd())?;
    Ok(pair)
}

fn duplicate_high(fd: &OwnedFd) -> io::Result<OwnedFd> {
    let duplicated =
        unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_DUPFD_CLOEXEC, MIN_INHERITED_FD) };
    if duplicated < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(unsafe {
        // SAFETY: `F_DUPFD_CLOEXEC` returned a new owned descriptor.
        OwnedFd::from_raw_fd(duplicated)
    })
}

fn duplicate_in_child(source: RawFd, target: RawFd) -> io::Result<()> {
    if unsafe { libc::dup2(source, target) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

fn set_cloexec(fd: RawFd) -> io::Result<()> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

fn set_nonblocking(fd: RawFd) -> io::Result<()> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

fn write_gate(fd: RawFd) -> io::Result<()> {
    loop {
        let byte = b'\n';
        let written = unsafe { libc::write(fd, (&byte as *const u8).cast(), 1) };
        if written == 1 {
            return Ok(());
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}

async fn read_status(status: &AsyncFd<OwnedFd>) -> io::Result<Vec<u8>> {
    let mut bytes = Vec::new();
    loop {
        let mut guard = status.readable().await?;
        let result = guard.try_io(|inner| {
            let mut chunk = [0_u8; 512];
            let read = unsafe {
                libc::read(
                    inner.get_ref().as_raw_fd(),
                    chunk.as_mut_ptr().cast(),
                    chunk.len(),
                )
            };
            if read < 0 {
                return Err(io::Error::last_os_error());
            }
            Ok((read as usize, chunk))
        });
        let (read, chunk) = match result {
            Ok(result) => result?,
            Err(_) => continue,
        };
        if read == 0 {
            return Ok(bytes);
        }
        if bytes.len() + read > MAX_STATUS_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "exec status exceeds limit",
            ));
        }
        bytes.extend_from_slice(&chunk[..read]);
    }
}
