//! Process liveness, stop requests and worker lookup for the full-daemon harness. Unix uses
//! kill(2), killpg(2) and ps(1). Windows uses tasklist and taskkill, which ship with the OS, since
//! plain std has no liveness probe, no process groups and no process-table walk.

use std::process::{Child, Command};
use std::thread::sleep;
use std::time::{Duration, Instant};

#[cfg(unix)]
use nix::errno::Errno;
#[cfg(unix)]
use nix::sys::signal::{Signal, kill, killpg};
#[cfg(unix)]
use nix::unistd::Pid;

use super::{FullDaemonError, POLL};

/// Whether a process with this pid exists and is not a zombie. EPERM counts as alive.
#[cfg(unix)]
pub fn pid_alive(pid: u32) -> bool {
    // Pid 0 names the caller's own process group to kill(2), so it is never a process of ours.
    let Ok(raw) = i32::try_from(pid) else {
        return false;
    };
    if raw <= 0 {
        return false;
    }
    match kill(Pid::from_raw(raw), None) {
        Ok(()) => !is_zombie(pid),
        Err(Errno::EPERM) => true,
        Err(_) => false,
    }
}

/// Windows liveness through tasklist, the shared check in the database module.
#[cfg(windows)]
pub fn pid_alive(pid: u32) -> bool {
    crate::database::process_alive(pid)
}

/// Whether the process is a zombie, by its `ps` state column.
#[cfg(unix)]
fn is_zombie(pid: u32) -> bool {
    Command::new("ps")
        .args(["-o", "stat=", "-p", &pid.to_string()])
        .output()
        .map(|output| {
            String::from_utf8_lossy(&output.stdout)
                .trim()
                .starts_with('Z')
        })
        .unwrap_or(false)
}

/// The `(pid, pgid)` pairs whose parent is `parent`, from one `ps` table read.
#[cfg(unix)]
fn children_of(parent: u32) -> Vec<(u32, u32)> {
    let Ok(output) = Command::new("ps")
        .args(["-A", "-o", "pid=,ppid=,pgid="])
        .output()
    else {
        return Vec::new();
    };
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(|line| {
            let fields: Vec<u32> = line
                .split_whitespace()
                .map(str::parse)
                .collect::<Result<_, _>>()
                .ok()?;
            match fields[..] {
                [pid, ppid, pgid] if ppid == parent => Some((pid, pgid)),
                _ => None,
            }
        })
        .collect()
}

/// Plain std cannot read the process table on Windows, so no child can be found through its
/// parent. A worker is then never recorded there, and that path is refused before any launch.
#[cfg(windows)]
fn children_of(_parent: u32) -> Vec<(u32, u32)> {
    Vec::new()
}

/// The Python worker: the child of gdaemon that leads its own process group (`pid == pgid`).
pub fn find_worker(daemon_pid: u32) -> Option<u32> {
    children_of(daemon_pid)
        .into_iter()
        .find(|(pid, pgid)| pid == pgid)
        .map(|(pid, _)| pid)
}

/// Asks a process group (Unix) or a process tree (Windows) to stop. `graceful` sends SIGTERM on
/// Unix; Windows has no graceful signal in plain std, so it always force-stops the tree.
#[cfg(unix)]
pub fn signal_group(pid: u32, graceful: bool) {
    let Ok(raw) = i32::try_from(pid) else {
        return;
    };
    let signal = if graceful {
        Signal::SIGTERM
    } else {
        Signal::SIGKILL
    };
    let _ = killpg(Pid::from_raw(raw), signal);
}

/// Windows: `taskkill /T /F` ends the process and its children. There is no graceful stop.
#[cfg(windows)]
pub fn signal_group(pid: u32, _graceful: bool) {
    taskkill(pid, true);
}

/// Asks one process (not its group) to stop. Windows force-stops it with `taskkill /F`.
#[cfg(unix)]
pub fn signal_pid(pid: u32, graceful: bool) {
    let Ok(raw) = i32::try_from(pid) else {
        return;
    };
    let signal = if graceful {
        Signal::SIGTERM
    } else {
        Signal::SIGKILL
    };
    let _ = kill(Pid::from_raw(raw), signal);
}

/// Windows: `taskkill /F` for the single process.
#[cfg(windows)]
pub fn signal_pid(pid: u32, _graceful: bool) {
    taskkill(pid, false);
}

/// Runs `taskkill /PID <pid> /F`, adding `/T` for the tree. Its exit status is not checked: the
/// caller polls liveness afterwards, which is the proof that matters.
#[cfg(windows)]
fn taskkill(pid: u32, tree: bool) {
    let mut command = Command::new("taskkill");
    command.args(["/PID", &pid.to_string(), "/F"]);
    if tree {
        command.arg("/T");
    }
    let _ = command.output();
}

/// SIGTERM (or the Windows force stop) to gdaemon's group, wait for its exit, then the forced
/// stop of the group after the grace period.
pub fn stop_daemon(child: &mut Child, grace: Duration) -> Result<(), FullDaemonError> {
    let pid = child.id();
    signal_group(pid, true);
    let deadline = Instant::now() + grace;
    loop {
        if child
            .try_wait()
            .map_err(|error| FullDaemonError::Teardown(format!("poll gdaemon: {error}")))?
            .is_some()
        {
            return Ok(());
        }
        if Instant::now() >= deadline {
            signal_group(pid, false);
            child
                .wait()
                .map_err(|error| FullDaemonError::Teardown(format!("reap gdaemon: {error}")))?;
            return Ok(());
        }
        sleep(POLL);
    }
}

/// Stops the worker's group, waiting up to `grace` for it to exit before the forced stop.
pub fn stop_worker(pid: u32, grace: Duration) {
    signal_group(pid, true);
    let deadline = Instant::now() + grace;
    while pid_alive(pid) && Instant::now() < deadline {
        sleep(POLL);
    }
    if pid_alive(pid) {
        signal_group(pid, false);
    }
}
