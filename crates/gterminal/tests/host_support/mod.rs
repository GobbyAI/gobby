//! Shared helpers for gterm host integration tests.

#![cfg(unix)]
#![allow(dead_code)]

use serde_json::Value;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

pub const CONTROL_SOCKET: &str = "gterm-control.sock";
pub const FRAMES_SOCKET: &str = "gterm-frames.sock";
pub const PID_FILE: &str = "gterm.pid";
pub const TOKEN_FILE: &str = "gterm-control.token";
static REQUEST_ID: AtomicU64 = AtomicU64::new(1);

pub struct HostProc {
    child: Child,
    socket_dir: PathBuf,
    owned_socket_dir: Option<tempfile::TempDir>,
    pgids: Vec<i32>,
}

impl HostProc {
    pub fn id(&self) -> u32 {
        self.child.id()
    }

    pub fn try_wait(&mut self) -> std::io::Result<Option<std::process::ExitStatus>> {
        self.child.try_wait()
    }

    pub fn kill(&mut self) -> std::io::Result<()> {
        self.child.kill()
    }

    pub fn track_pgid(&mut self, pgid: i32) {
        self.pgids.push(pgid);
    }

    pub fn socket_dir(&self) -> &Path {
        &self.socket_dir
    }

    pub fn own_socket_dir(&mut self, dir: tempfile::TempDir) {
        assert_eq!(dir.path(), self.socket_dir);
        self.owned_socket_dir = Some(dir);
    }
}

impl Drop for HostProc {
    fn drop(&mut self) {
        for pgid in self.pgids.drain(..) {
            if pgid > 0 {
                unsafe {
                    libc::killpg(pgid, libc::SIGKILL);
                }
            }
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

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

pub fn spawn_host(socket_dir: &Path) -> HostProc {
    spawn_host_with_args(socket_dir, &[])
}

pub fn spawn_host_with_args(socket_dir: &Path, extra: &[&str]) -> HostProc {
    let log_path = socket_dir.join("gterm.log");
    let token_path = socket_dir.join("local_cli_token");
    if !token_path.exists() {
        std::fs::write(&token_path, "local-token").expect("write local token");
    }
    let mut cmd = Command::new(gterm_bin());
    cmd.arg("host")
        .arg("--socket-dir")
        .arg(socket_dir)
        .args(extra)
        .env("GTERM_LOG_FILE", &log_path)
        .env("GTERM_TEST_HELPER", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    let child = cmd.spawn().expect("spawn gterm host");
    HostProc {
        child,
        socket_dir: socket_dir.to_path_buf(),
        owned_socket_dir: None,
        pgids: Vec::new(),
    }
}

pub fn hello_control(stream: &mut UnixStream, token: &str) -> Value {
    send_json(
        stream,
        &serde_json::json!({
            "method": "hello",
            "protocol_version": 1,
            "control_token": token,
        }),
    );
    recv_json(stream)
}

pub fn rpc(stream: &mut UnixStream, method: &str, extra: serde_json::Value) -> Value {
    let mut req = extra;
    if let Some(obj) = req.as_object_mut() {
        obj.insert("method".into(), serde_json::Value::String(method.into()));
    }
    send_json(stream, &req);
    recv_json(stream)
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

/// Poll `ready` until it reports true, panicking after five seconds.
pub fn wait_until(what: &str, mut ready: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if ready() {
            return;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    panic!("timed out waiting for {what}");
}

pub fn connect(path: &Path) -> UnixStream {
    UnixStream::connect(path).unwrap_or_else(|err| {
        panic!("connect {}: {err}", path.display());
    })
}

pub fn send_json(stream: &mut UnixStream, value: &Value) {
    let mut value = value.clone();
    if let Some(object) = value.as_object_mut() {
        object.entry("id").or_insert_with(|| {
            Value::String(format!(
                "test-{}",
                REQUEST_ID.fetch_add(1, Ordering::Relaxed)
            ))
        });
    }
    send_json_without_id(stream, &value);
}

pub fn send_json_without_id(stream: &mut UnixStream, value: &Value) {
    let mut line = serde_json::to_string(value).expect("serialize");
    line.push('\n');
    stream.write_all(line.as_bytes()).expect("write request");
    stream.flush().expect("flush request");
}

pub fn recv_json(stream: &mut UnixStream) -> Value {
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .expect("read timeout");
    // This helper does not retain a BufReader between calls. A one-byte buffer
    // prevents it from reading and then discarding the next correlated reply.
    let mut reader = BufReader::with_capacity(1, stream);
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

pub fn wait_exit(host: &mut HostProc, timeout: Duration) -> Option<std::process::ExitStatus> {
    let deadline = Instant::now() + timeout;
    loop {
        match host.try_wait() {
            Ok(Some(status)) => return Some(status),
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(20));
            }
            _ => return None,
        }
    }
}
