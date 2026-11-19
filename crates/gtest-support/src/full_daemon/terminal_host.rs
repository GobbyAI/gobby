//! The detached gterm host that gdaemon starts, which outlives its daemon by design. The harness
//! builds it when missing, installs it into the run home, and stops it at teardown. This module
//! also finds the run's processes (the worker, the host, anything left in the run's root) from one
//! process-table read, and stops the strays that outlive the orderly teardown.

use std::collections::BTreeSet;
use std::fs;
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::thread::sleep;
use std::time::Instant;

use super::POLL;
#[cfg(unix)]
use super::process::NAME_COLUMN;
use super::process::{pid_alive, signal_pid};
use super::{FullDaemonError, WORKER_GRACE};

/// The gterm binary file name for this platform (`gterm` or `gterm.exe`).
fn gterm_file_name() -> String {
    format!("gterm{}", std::env::consts::EXE_SUFFIX)
}

/// The gterm binary next to gdaemon. Built with the same profile when it is missing.
pub fn ensure_gterm(gdaemon: &Path, repo_root: &Path) -> Result<PathBuf, FullDaemonError> {
    let built = gdaemon.with_file_name(gterm_file_name());
    if built.is_file() {
        return Ok(built);
    }
    let target = gdaemon.parent().and_then(Path::parent).ok_or_else(|| {
        FullDaemonError::Missing(format!("no target directory above {}", gdaemon.display()))
    })?;
    // gdaemon's profile directory names the build profile, so gterm is built in the same one.
    let release = gdaemon
        .parent()
        .and_then(Path::file_name)
        .is_some_and(|name| name == "release");
    let mut args = vec![
        "build",
        "-p",
        "gobby-terminal",
        "--features",
        "vt-engine",
        "--bin",
        "gterm",
    ];
    if release {
        args.push("--release");
    }
    let output = Command::new("cargo")
        .args(&args)
        .env("CARGO_TARGET_DIR", target)
        .current_dir(repo_root)
        .output()
        .map_err(|error| FullDaemonError::Io(format!("run cargo for gterm: {error}")))?;
    if !output.status.success() {
        return Err(FullDaemonError::Missing(format!(
            "cargo could not build gterm:\n{}",
            String::from_utf8_lossy(&output.stderr)
        )));
    }
    if built.is_file() {
        Ok(built)
    } else {
        Err(FullDaemonError::Missing(format!(
            "gterm is absent at {}",
            built.display()
        )))
    }
}

/// Copies gterm into `<home>/bin`, where the daemon launches it from, executable on Unix.
pub fn install_gterm(gterm: &Path, home: &Path) -> Result<(), FullDaemonError> {
    let bin = home.join("bin");
    fs::create_dir_all(&bin)
        .map_err(|error| FullDaemonError::Io(format!("create the home bin directory: {error}")))?;
    let target = bin.join(gterm_file_name());
    fs::copy(gterm, &target)
        .map_err(|error| FullDaemonError::Io(format!("copy gterm into the home: {error}")))?;
    #[cfg(unix)]
    fs::set_permissions(&target, fs::Permissions::from_mode(0o755))
        .map_err(|error| FullDaemonError::Io(format!("chmod gterm: {error}")))?;
    Ok(())
}

/// Whether the process is a gterm host, by its short process name (`gterm` or `gterm-<hash>`).
/// The pinned image is named `gterm-<sha256>`, and the `ps` column truncates it to 16 characters.
/// Windows has no name column in plain std, so the name check reports false there: the host is
/// then not stopped by the harness (a found line for the Windows smoke test).
#[cfg(unix)]
pub(super) fn is_gterm_host(pid: u32) -> bool {
    Command::new("ps")
        .args(["-o", &format!("{NAME_COLUMN}="), "-p", &pid.to_string()])
        .output()
        .map(|output| {
            let name = String::from_utf8_lossy(&output.stdout);
            let name = name.trim();
            name == "gterm" || name.starts_with("gterm-")
        })
        .unwrap_or(false)
}

#[cfg(windows)]
pub(super) fn is_gterm_host(_pid: u32) -> bool {
    false
}

/// One row of the process table, read in a single `ps` call.
#[cfg(unix)]
#[derive(Debug, Clone, Copy)]
struct ProcessRow {
    pid: u32,
    ppid: u32,
    pgid: u32,
    zombie: bool,
}

#[cfg(unix)]
fn process_table() -> Vec<ProcessRow> {
    let Ok(output) = Command::new("ps")
        .args([
            "-A", "-o", "pid=", "-o", "ppid=", "-o", "pgid=", "-o", "stat=",
        ])
        .output()
    else {
        return Vec::new();
    };
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(|line| {
            let mut fields = line.split_whitespace();
            Some(ProcessRow {
                pid: fields.next()?.parse().ok()?,
                ppid: fields.next()?.parse().ok()?,
                pgid: fields.next()?.parse().ok()?,
                zombie: fields.next()?.starts_with('Z'),
            })
        })
        .collect()
}

/// The gterm hosts that are children of `parent` (gdaemon), live ones only. Unix reads the
/// process table; Windows has no name check in plain std, so it finds none.
#[cfg(unix)]
pub fn terminal_host_children(parent: u32) -> Vec<u32> {
    if parent == 0 {
        return Vec::new();
    }
    process_table()
        .into_iter()
        .filter(|row| row.ppid == parent && !row.zombie && is_gterm_host(row.pid))
        .map(|row| row.pid)
        .collect()
}

#[cfg(windows)]
pub fn terminal_host_children(_parent: u32) -> Vec<u32> {
    Vec::new()
}

/// The host pid from the `gterm.pid` file in its socket directory.
pub fn read_host_pid(socket_dir: &Path) -> Option<u32> {
    if socket_dir.as_os_str().is_empty() {
        return None;
    }
    fs::read_to_string(socket_dir.join("gterm.pid"))
        .ok()?
        .trim()
        .parse::<u32>()
        .ok()
}

/// Stops a live gterm host: SIGTERM, then the forced stop after the grace period. Returns whether
/// a live host was actually signalled, which decides whether the teardown step is recorded.
pub fn stop_terminal_host(pid: u32) -> bool {
    if !pid_alive(pid) || !is_gterm_host(pid) {
        return false;
    }
    signal_pid(pid, true);
    let deadline = Instant::now() + WORKER_GRACE;
    while pid_alive(pid) && Instant::now() < deadline {
        sleep(POLL);
    }
    if pid_alive(pid) {
        signal_pid(pid, false);
    }
    true
}

/// The live processes of a run: those whose cwd is under `root` or whose process group is one of
/// `groups`. Zombies are not live, and the calling process is never reported.
pub fn processes_in_run(root: &Path, groups: &[u32]) -> Vec<u32> {
    let own = std::process::id();
    let mut found: BTreeSet<u32> = cwd_pids(root).into_iter().collect();
    found.extend(group_members(groups));
    found
        .into_iter()
        .filter(|&pid| pid > 1 && pid != own && pid_alive(pid))
        .collect()
}

/// The live members of the given process groups. Group 0 and 1 name no run group.
#[cfg(unix)]
fn group_members(groups: &[u32]) -> Vec<u32> {
    let groups: Vec<u32> = groups.iter().copied().filter(|&group| group > 1).collect();
    if groups.is_empty() {
        return Vec::new();
    }
    process_table()
        .into_iter()
        .filter(|row| !row.zombie && groups.contains(&row.pgid))
        .map(|row| row.pid)
        .collect()
}

#[cfg(windows)]
fn group_members(_groups: &[u32]) -> Vec<u32> {
    Vec::new()
}

/// The pids whose current directory is under `root`. Linux reads /proc; elsewhere lsof answers.
#[cfg(target_os = "linux")]
fn cwd_pids(root: &Path) -> Vec<u32> {
    let Ok(entries) = fs::read_dir("/proc") else {
        return Vec::new();
    };
    entries
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let pid = entry.file_name().to_str()?.parse::<u32>().ok()?;
            let cwd = fs::read_link(entry.path().join("cwd")).ok()?;
            cwd.starts_with(root).then_some(pid)
        })
        .collect()
}

#[cfg(all(unix, not(target_os = "linux")))]
fn cwd_pids(root: &Path) -> Vec<u32> {
    let Ok(output) = Command::new("lsof")
        .args(["-w", "-a", "-d", "cwd", "-Fpn"])
        .output()
    else {
        return Vec::new();
    };
    let mut pid = None;
    let mut found = Vec::new();
    for line in String::from_utf8_lossy(&output.stdout).lines() {
        if let Some(value) = line.strip_prefix('p') {
            pid = value.parse::<u32>().ok();
        } else if let Some(path) = line.strip_prefix('n') {
            found.extend(pid.filter(|_| Path::new(path).starts_with(root)));
        }
    }
    found
}

#[cfg(windows)]
fn cwd_pids(_root: &Path) -> Vec<u32> {
    Vec::new()
}

/// Stops processes that outlived the run's orderly teardown: SIGTERM, the grace period, then
/// SIGKILL for any still alive.
pub(super) fn stop_strays(pids: &[u32]) {
    for &pid in pids {
        signal_pid(pid, true);
    }
    let deadline = Instant::now() + WORKER_GRACE;
    while pids.iter().any(|&pid| pid_alive(pid)) && Instant::now() < deadline {
        sleep(POLL);
    }
    for &pid in pids {
        if pid_alive(pid) {
            signal_pid(pid, false);
        }
    }
}

/// The Python worker: a child of gdaemon that leads its own process group, and is not a gterm
/// host. The host also leads a group and is gdaemon's child, so the host is excluded by name.
#[cfg(unix)]
pub(super) fn worker_of(daemon_pid: u32) -> Option<u32> {
    if daemon_pid == 0 {
        return None;
    }
    process_table()
        .into_iter()
        .find(|row| {
            row.ppid == daemon_pid && row.pid == row.pgid && !row.zombie && !is_gterm_host(row.pid)
        })
        .map(|row| row.pid)
}

/// Plain std cannot read the process table on Windows, so no worker is found through its parent.
#[cfg(windows)]
pub(super) fn worker_of(_daemon_pid: u32) -> Option<u32> {
    None
}
