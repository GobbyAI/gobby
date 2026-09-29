//! `gterm host` argv and startup contract: help exits 0, bad argv exits 2,
//! `--probe-resume` exits 0 or 3, none of them starts a host, and a host
//! losing the busy-socket check leaves the live host's pidfile alone (#22425).
//!
//! The argv runs point `GTERM_SOCKET_DIR` and `--socket-dir` at a temp dir and
//! write no control token there, so a binary that regressed to ignoring
//! unknown arguments fails the exit-code assertion instead of parking as a
//! live host on the caller's `~/.gobby`.

mod host_support;

use std::path::Path;
use std::process::{Command, Output};

const CONTROL_SOCKET: &str = "gterm-control.sock";
const FRAMES_SOCKET: &str = "gterm-frames.sock";
const PID_FILE: &str = "gterm.pid";

fn run_host(socket_dir: &Path, extra: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_gterm"))
        .arg("host")
        .arg("--socket-dir")
        .arg(socket_dir)
        .args(extra)
        .env("GTERM_SOCKET_DIR", socket_dir)
        .env("GTERM_LOG_FILE", socket_dir.join("gterm.log"))
        .output()
        .expect("run gterm host")
}

fn assert_no_host_state(socket_dir: &Path) {
    for name in [CONTROL_SOCKET, FRAMES_SOCKET, PID_FILE] {
        let path = socket_dir.join(name);
        assert!(!path.exists(), "gterm host created {}", path.display());
    }
}

fn temp_dir(prefix: &str) -> tempfile::TempDir {
    tempfile::Builder::new()
        .prefix(prefix)
        .tempdir()
        .expect("socket tempdir")
}

#[test]
fn host_help_prints_usage_and_exits_zero() {
    let dir = temp_dir("gterm-help");
    let output = run_host(dir.path(), &["--help"]);

    assert_eq!(output.status.code(), Some(0), "`--help` must exit 0");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("usage: gterm host"), "stdout: {stdout}");
    for flag in [
        "--socket-dir",
        "--max-attached-terminals",
        "--tmux-attach-history-lines",
        "--help",
    ] {
        assert!(stdout.contains(flag), "usage omits {flag}: {stdout}");
    }
    assert_no_host_state(dir.path());
}

#[test]
fn host_short_help_exits_zero() {
    let dir = temp_dir("gterm-help-short");
    let output = run_host(dir.path(), &["-h"]);

    assert_eq!(output.status.code(), Some(0), "`-h` must exit 0");
    assert!(String::from_utf8_lossy(&output.stdout).contains("usage: gterm host"));
    assert_no_host_state(dir.path());
}

#[test]
fn host_rejects_unknown_argument() {
    let dir = temp_dir("gterm-bogus");
    let output = run_host(dir.path(), &["--bogus"]);

    assert_eq!(
        output.status.code(),
        Some(2),
        "an unknown argument must exit 2, stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("--bogus"), "stderr: {stderr}");
    assert!(stderr.contains("usage: gterm host"), "stderr: {stderr}");
    assert!(
        output.stdout.is_empty(),
        "usage on a rejection goes to stderr"
    );
    assert_no_host_state(dir.path());
}

#[test]
fn host_rejects_flag_without_its_value() {
    let dir = temp_dir("gterm-novalue");
    let output = run_host(dir.path(), &["--tmux-poll-interval-ms"]);

    assert_eq!(
        output.status.code(),
        Some(2),
        "a valueless flag must exit 2"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("--tmux-poll-interval-ms"),
        "stderr: {stderr}"
    );
    assert!(stderr.contains("usage: gterm host"), "stderr: {stderr}");
    assert_no_host_state(dir.path());
}

/// A second host on a live socket dir must exit without publishing its pid:
/// the daemon reads the pidfile to find the host it adopts, and a dead pid
/// there orphans every running terminal until an operator repairs the file.
#[test]
fn host_losing_the_busy_socket_check_keeps_the_live_pidfile() {
    let dir = host_support::temp_socket_dir();
    host_support::write_token(dir.path(), "token-busy");
    let mut first = host_support::spawn_host(dir.path());
    let control_path = dir.path().join(CONTROL_SOCKET);
    host_support::wait_socket(&control_path);
    let pid_path = dir.path().join(PID_FILE);
    let first_pid = first.id().to_string();
    host_support::wait_until("the live host publishes its pid", || {
        std::fs::read_to_string(&pid_path)
            .map(|pid| pid.trim() == first_pid)
            .unwrap_or(false)
    });

    let output = run_host(dir.path(), &[]);

    assert_ne!(
        output.status.code(),
        Some(0),
        "a host on a busy socket dir must fail, stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("busy"), "stderr: {stderr}");
    let published = std::fs::read_to_string(&pid_path).expect("live pidfile survives");
    assert_eq!(
        published.trim(),
        first_pid,
        "the losing host must not overwrite the live host's pidfile"
    );
    assert!(
        std::os::unix::net::UnixStream::connect(&control_path).is_ok(),
        "the live host keeps serving its control socket"
    );
    first.kill().expect("stop the live host");
}

/// Runs `gterm host --probe-resume <version>` and returns its output, killing
/// it if it outlives the timeout (a probe that falls through to a cold start).
fn probe_resume(socket_dir: &Path, version: &str) -> Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_gterm"))
        .args(["host", "--socket-dir"])
        .arg(socket_dir)
        .args(["--probe-resume", version])
        .env("GTERM_SOCKET_DIR", socket_dir)
        .env("GTERM_LOG_FILE", socket_dir.join("gterm.log"))
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .expect("spawn gterm host --probe-resume");
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
    while child.try_wait().expect("poll probe").is_none() {
        if std::time::Instant::now() >= deadline {
            child.kill().expect("kill probe");
            panic!("--probe-resume {version} did not exit");
        }
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
    child.wait_with_output().expect("probe output")
}

fn dir_listing(dir: &Path) -> Vec<(String, u64, std::time::SystemTime)> {
    let mut entries: Vec<_> = std::fs::read_dir(dir)
        .expect("read images dir")
        .map(|entry| {
            let entry = entry.expect("images dir entry");
            let meta = entry.metadata().expect("images entry metadata");
            (
                entry.file_name().to_string_lossy().into_owned(),
                meta.len(),
                meta.modified().expect("images entry mtime"),
            )
        })
        .collect();
    entries.sort();
    entries
}

#[test]
fn probe_resume_reports_supported_formats() {
    use gobby_terminal::host::handover::SUPPORTED_FORMAT_VERSIONS;
    use gobby_terminal::host::image::IMAGES_DIR;

    let dir = host_support::temp_socket_dir();
    // A token lets a probe that fell through reach pinning and bind.
    host_support::write_token(dir.path(), "token-probe");
    let images = dir.path().join(IMAGES_DIR);
    std::fs::create_dir(&images).expect("images dir");
    std::fs::write(images.join("stale-pin"), b"earlier image").expect("stale pin");
    let before = dir_listing(&images);
    let supported = SUPPORTED_FORMAT_VERSIONS
        .iter()
        .map(u32::to_string)
        .collect::<Vec<_>>()
        .join(",");

    let newest = *SUPPORTED_FORMAT_VERSIONS.iter().max().expect("a version");
    for version in 0..=newest + 1 {
        let output = probe_resume(dir.path(), &version.to_string());
        let expected = if SUPPORTED_FORMAT_VERSIONS.contains(&version) {
            0
        } else {
            3
        };
        assert_eq!(
            output.status.code(),
            Some(expected),
            "--probe-resume {version}, stderr: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert_eq!(
            String::from_utf8_lossy(&output.stdout),
            format!("{supported}\n")
        );
    }
    let output = probe_resume(dir.path(), "one");
    assert_eq!(
        output.status.code(),
        Some(2),
        "a non-numeric version is bad argv"
    );

    assert_eq!(
        dir_listing(&images),
        before,
        "the probe pins and prunes nothing"
    );
    assert_no_host_state(dir.path());
}
