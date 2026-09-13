//! Shared helpers for gterm tmux-observation integration tests.

#![allow(dead_code)]

use gobby_terminal::protocol::{
    read_message, write_message, ClientMessage, PaneLocator, RenderEncoding, ServerMessage,
    TmuxClientIdentity, MAX_FRAME_SIZE, PROTOCOL_VERSION,
};
use serde_json::Value;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

pub use crate::host_support::HostProc;

pub const CONTROL_SOCKET: &str = "gterm-control.sock";
pub const FRAMES_SOCKET: &str = "gterm-frames.sock";
pub const LOCAL: &str = "local-token";
static REQUEST_ID: AtomicU64 = AtomicU64::new(1);

pub struct TmuxPane {
    pub dir: tempfile::TempDir,
    pub socket: PathBuf,
    pub pane_id: String,
    pub server_pid: i32,
    pub start_time: i64,
    pub session: String,
}

impl Drop for TmuxPane {
    fn drop(&mut self) {
        let _ = Command::new("tmux")
            .args(["-S", &self.socket.to_string_lossy(), "kill-server"])
            .status();
    }
}

impl TmuxPane {
    pub fn locator(&self) -> PaneLocator {
        PaneLocator {
            socket_path: self.socket.to_string_lossy().into_owned(),
            server_pid: self.server_pid,
            server_start_time: self.start_time,
            pane_id: self.pane_id.clone(),
        }
    }

    pub fn identity(&self) -> TmuxClientIdentity {
        TmuxClientIdentity {
            socket_path: self.socket.to_string_lossy().into_owned(),
            server_pid: self.server_pid,
            server_start_time: self.start_time,
            pane_id: self.pane_id.clone(),
        }
    }

    pub fn tmux(&self, args: &[&str]) -> String {
        let output = Command::new("tmux")
            .arg("-S")
            .arg(&self.socket)
            .args(args)
            .output()
            .expect("tmux");
        String::from_utf8_lossy(&output.stdout).trim().to_string()
    }

    pub fn send_hex(&self, bytes: &[u8]) {
        let hex: Vec<String> = bytes.iter().map(|b| format!("{b:02x}")).collect();
        let mut args = vec!["send-keys", "-t", self.pane_id.as_str(), "-H"];
        let hex_ref: Vec<&str> = hex.iter().map(String::as_str).collect();
        args.extend(hex_ref);
        let _ = Command::new("tmux")
            .arg("-S")
            .arg(&self.socket)
            .args(&args)
            .status();
    }

    pub fn snapshot(&self) -> String {
        self.tmux(&[
            "display-message",
            "-p",
            "-t",
            &self.pane_id,
            "#{session_name} #{window_id} #{pane_id} #{window_width} #{window_height} #{pane_width} #{pane_height} #{pane_pipe} #{pane_active} #{window_active}",
        ])
    }
}

pub fn start_tmux() -> TmuxPane {
    start_tmux_sized(80, 24)
}

pub fn start_tmux_sized(cols: u16, rows: u16) -> TmuxPane {
    let dir = tempfile::tempdir().expect("tempdir");
    let socket = dir.path().join("tmux.sock");
    let session = format!("gobby-{}", std::process::id());
    let status = Command::new("tmux")
        .arg("-S")
        .arg(&socket)
        .args([
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-s",
            &session,
            "-x",
            &cols.to_string(),
            "-y",
            &rows.to_string(),
            "--",
            "/bin/sh",
        ])
        .status()
        .expect("spawn tmux");
    assert!(status.success(), "tmux new-session failed");
    let pane_id = tmux_out(&socket, &["display-message", "-p", "#{pane_id}"]);
    let server_pid: i32 = tmux_out(&socket, &["display-message", "-p", "#{pid}"])
        .parse()
        .expect("pid");
    let start_time: i64 = tmux_out(&socket, &["display-message", "-p", "#{start_time}"])
        .parse()
        .expect("start_time");
    TmuxPane {
        dir,
        socket,
        pane_id,
        server_pid,
        start_time,
        session,
    }
}

fn tmux_out(socket: &Path, args: &[&str]) -> String {
    let output = Command::new("tmux")
        .arg("-S")
        .arg(socket)
        .args(args)
        .output()
        .expect("tmux");
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}

pub fn spawn_host(extra: &[&str]) -> HostProc {
    let dir = tempfile::tempdir().expect("tempdir");
    let token_path = dir.path().join("gterm-control.token");
    std::fs::write(&token_path, "control-token").unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut perms = std::fs::metadata(&token_path).unwrap().permissions();
        perms.set_mode(0o600);
        std::fs::set_permissions(&token_path, perms).unwrap();
    }
    std::fs::write(dir.path().join("local_cli_token"), LOCAL).unwrap();
    let mut args = vec!["--tmux-poll-interval-ms", "50"];
    args.extend_from_slice(extra);
    let mut host = crate::host_support::spawn_host_with_args(dir.path(), &args);
    host.own_socket_dir(dir);
    wait_socket(&host.socket_dir().join(CONTROL_SOCKET));
    wait_socket(&host.socket_dir().join(FRAMES_SOCKET));
    host
}

pub fn wait_socket(path: &Path) {
    let deadline = Instant::now() + Duration::from_secs(8);
    while Instant::now() < deadline {
        if path.exists() && UnixStream::connect(path).is_ok() {
            return;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    panic!("timed out waiting for {}", path.display());
}

pub fn hello_msg(identity: Option<TmuxClientIdentity>) -> ClientMessage {
    ClientMessage::Hello {
        version: PROTOCOL_VERSION,
        encoding: RenderEncoding::SemanticFrame,
        local_token: LOCAL.into(),
        cols: 80,
        rows: 24,
        tmux_identity: identity,
    }
}

pub fn write_msg(stream: &mut UnixStream, msg: &ClientMessage) {
    let mut buf = Vec::new();
    write_message(&mut buf, msg).unwrap();
    stream.write_all(&buf).unwrap();
    stream.flush().unwrap();
}

pub fn read_msg_timeout(stream: &mut UnixStream, timeout: Duration) -> Option<ServerMessage> {
    stream.set_read_timeout(Some(timeout)).ok()?;
    read_message(stream, MAX_FRAME_SIZE).ok()
}

pub fn read_msg(stream: &mut UnixStream) -> ServerMessage {
    read_msg_timeout(stream, Duration::from_secs(5)).expect("frame message")
}

pub fn connect_frames(host: &HostProc, identity: Option<TmuxClientIdentity>) -> UnixStream {
    let mut stream = UnixStream::connect(host.socket_dir().join(FRAMES_SOCKET)).unwrap();
    write_msg(&mut stream, &hello_msg(identity));
    match read_msg(&mut stream) {
        ServerMessage::Welcome { .. } => stream,
        other => panic!("expected welcome, got {other:?}"),
    }
}

pub fn attach(stream: &mut UnixStream, locator: PaneLocator) -> ServerMessage {
    write_msg(
        stream,
        &ClientMessage::AttachTerminal {
            host_terminal_id: String::new(),
            reservation_id: None,
            locator: Some(locator),
        },
    );
    read_msg(stream)
}

pub fn control(host: &HostProc) -> UnixStream {
    let mut stream = UnixStream::connect(host.socket_dir().join(CONTROL_SOCKET)).unwrap();
    send_json(
        &mut stream,
        &serde_json::json!({
            "method": "hello",
            "protocol_version": 1,
            "control_token": "control-token",
        }),
    );
    let hello = recv_json(&mut stream);
    assert_eq!(hello["ok"], true, "{hello}");
    stream
}

pub fn send_json(stream: &mut UnixStream, value: &Value) {
    let mut value = value.clone();
    if let Some(object) = value.as_object_mut() {
        object.entry("id").or_insert_with(|| {
            Value::String(format!(
                "embed-test-{}",
                REQUEST_ID.fetch_add(1, Ordering::Relaxed)
            ))
        });
    }
    let mut line = serde_json::to_string(&value).unwrap();
    line.push('\n');
    stream.write_all(line.as_bytes()).unwrap();
    stream.flush().unwrap();
}

pub fn recv_json(stream: &mut UnixStream) -> Value {
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    reader.read_line(&mut line).unwrap();
    serde_json::from_str(line.trim_end()).unwrap()
}

pub fn list_terminals(host: &HostProc) -> Value {
    let mut stream = control(host);
    send_json(&mut stream, &serde_json::json!({"method": "list"}));
    recv_json(&mut stream)
}

pub fn crate_src() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src")
}

pub fn gclient_views() -> String {
    std::fs::read_to_string(
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../gclient/src/views/mod.rs"),
    )
    .expect("gclient views")
}

pub fn wait_until<F: FnMut() -> bool>(timeout: Duration, mut pred: F) {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if pred() {
            return;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    panic!("condition not met in {timeout:?}");
}
