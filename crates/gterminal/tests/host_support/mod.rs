//! Shared helpers for gterm host integration tests.

#![cfg(unix)]
#![allow(dead_code)]

use serde_json::Value;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

pub const CONTROL_SOCKET: &str = "gterm-control.sock";
pub const FRAMES_SOCKET: &str = "gterm-frames.sock";
pub const PID_FILE: &str = "gterm.pid";
pub const TOKEN_FILE: &str = "gterm-control.token";

pub fn gterm_bin() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_gterm"))
}

pub fn write_token(dir: &Path, token: &str) {
    let path = dir.join(TOKEN_FILE);
    std::fs::write(&path, token).expect("write control token");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut perms = std::fs::metadata(&path).unwrap().permissions();
        perms.set_mode(0o600);
        std::fs::set_permissions(&path, perms).unwrap();
    }
}

pub fn spawn_host(socket_dir: &Path) -> Child {
    let log_path = socket_dir.join("gterm.log");
    Command::new(gterm_bin())
        .arg("host")
        .arg("--socket-dir")
        .arg(socket_dir)
        .env("GTERM_LOG_FILE", &log_path)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn gterm host")
}

pub fn wait_socket(path: &Path) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if path.exists() && UnixStream::connect(path).is_ok() {
            return;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    panic!("timed out waiting for {}", path.display());
}

pub fn connect(path: &Path) -> UnixStream {
    UnixStream::connect(path).unwrap_or_else(|err| {
        panic!("connect {}: {err}", path.display());
    })
}

pub fn send_json(stream: &mut UnixStream, value: &Value) {
    let mut line = serde_json::to_string(value).expect("serialize");
    line.push('\n');
    stream.write_all(line.as_bytes()).expect("write request");
    stream.flush().expect("flush request");
}

pub fn recv_json(stream: &mut UnixStream) -> Value {
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .expect("read timeout");
    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    reader.read_line(&mut line).expect("read response line");
    assert!(
        !line.is_empty(),
        "control socket closed before a response line"
    );
    serde_json::from_str(line.trim_end()).expect("parse response json")
}

pub fn socket_mode(path: &Path) -> u32 {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path)
        .unwrap_or_else(|err| panic!("stat {}: {err}", path.display()))
        .permissions()
        .mode()
        & 0o777
}

pub fn wait_exit(child: &mut Child, timeout: Duration) -> Option<std::process::ExitStatus> {
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Some(status),
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(20));
            }
            _ => return None,
        }
    }
}
