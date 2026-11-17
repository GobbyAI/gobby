//! Shared helpers for gterm tmux-observation integration tests.

#![allow(dead_code)]

use gobby_terminal::protocol::{
    read_message, write_message, ClientMessage, RenderEncoding, ServerMessage, MAX_FRAME_SIZE,
    PROTOCOL_VERSION,
};
use serde_json::Value;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

pub use super::host_support::HostProc;

pub const CONTROL_SOCKET: &str = "gterm-control.sock";
pub const FRAMES_SOCKET: &str = "gterm-frames.sock";
pub const LOCAL: &str = "local-token";
static REQUEST_ID: AtomicU64 = AtomicU64::new(1);

pub fn spawn_host(extra: &[&str]) -> HostProc {
    spawn_host_with_env_removed(extra, &[])
}

pub fn spawn_host_with_env_removed(extra: &[&str], removed: &[&str]) -> HostProc {
    spawn_host_with_env(extra, &[], removed)
}

pub fn spawn_host_with_env(extra: &[&str], env: &[(&str, &str)], removed: &[&str]) -> HostProc {
    let dir = super::host_support::temp_socket_dir();
    let token_path = dir.path().join("gterm-control.token");
    std::fs::write(&token_path, "control-token").unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut perms = std::fs::metadata(&token_path).unwrap().permissions();
        perms.set_mode(0o600);
        std::fs::set_permissions(&token_path, perms).unwrap();
    }
    std::fs::write(
        dir.path().join("bootstrap.yaml"),
        format!("api_key: {LOCAL}\n"),
    )
    .unwrap();
    let mut args = vec!["--tmux-poll-interval-ms", "50"];
    args.extend_from_slice(extra);
    let mut host = super::host_support::spawn_host_with_env(dir.path(), &args, env, removed);
    host.own_socket_dir(dir);
    wait_socket(&host.socket_dir().join(CONTROL_SOCKET));
    wait_socket(&host.socket_dir().join(FRAMES_SOCKET));
    host
}

pub fn wait_socket(path: &Path) {
    super::host_support::wait_socket(path);
}

pub fn hello_msg() -> ClientMessage {
    ClientMessage::Hello {
        version: PROTOCOL_VERSION,
        encoding: RenderEncoding::SemanticFrame,
        local_token: LOCAL.into(),
        cols: 80,
        rows: 24,
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

pub fn connect_frames(host: &HostProc) -> UnixStream {
    let mut stream = UnixStream::connect(host.socket_dir().join(FRAMES_SOCKET)).unwrap();
    write_msg(&mut stream, &hello_msg());
    match read_msg(&mut stream) {
        ServerMessage::Welcome { .. } => stream,
        other => panic!("expected welcome, got {other:?}"),
    }
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

/// Poll `pred` until it holds, panicking after `timeout` with the host state under `dir`
/// (#25733 criterion 2).
pub fn wait_until_host<F: FnMut() -> bool>(dir: impl AsRef<Path>, timeout: Duration, mut pred: F) {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if pred() {
            return;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    panic!(
        "condition not met in {timeout:?}; {}",
        crate::host_support::host_report(dir.as_ref())
    );
}
