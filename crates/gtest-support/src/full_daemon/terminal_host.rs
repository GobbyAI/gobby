//! The detached gterm host that gdaemon starts, which outlives its daemon by design. The harness
//! builds it when missing, installs it into the run home, and stops it at teardown.

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
fn is_gterm_host(pid: u32) -> bool {
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
fn is_gterm_host(_pid: u32) -> bool {
    false
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
