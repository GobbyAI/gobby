//! Host process lifecycle: sockets, ping/list, shutdown drain (plan 3.1.1, 3.1.22).

#![cfg(unix)]

mod host_support;

use host_support::{
    connect, recv_json, send_json, socket_mode, spawn_host, wait_exit, wait_socket, write_token,
    CONTROL_SOCKET, FRAMES_SOCKET, PID_FILE,
};
use serde_json::json;
use std::os::unix::net::UnixListener;
use std::path::Path;
use std::time::Duration;

fn hello(stream: &mut std::os::unix::net::UnixStream, token: &str) -> serde_json::Value {
    send_json(
        stream,
        &json!({
            "method": "hello",
            "protocol_version": 1,
            "control_token": token,
        }),
    );
    recv_json(stream)
}

#[test]
fn host_starts_and_serves_ping_and_list() {
    let dir = tempfile::tempdir().expect("tempdir");
    let token = "control-token-lifecycle";
    write_token(dir.path(), token);

    let stale = dir.path().join(CONTROL_SOCKET);
    let _stale_listener = UnixListener::bind(&stale).expect("bind stale control socket");
    drop(_stale_listener);
    assert!(stale.exists(), "stale socket file must exist before start");

    let mut child = spawn_host(dir.path());
    let control = dir.path().join(CONTROL_SOCKET);
    let frames = dir.path().join(FRAMES_SOCKET);
    wait_socket(&control);
    wait_socket(&frames);

    assert_eq!(socket_mode(&control), 0o600);
    assert_eq!(socket_mode(&frames), 0o600);

    let pid_text = std::fs::read_to_string(dir.path().join(PID_FILE)).expect("pidfile");
    let pidfile_pid: u32 = pid_text.trim().parse().expect("pidfile int");
    assert_eq!(pidfile_pid, child.id());

    let mut stream = connect(&control);
    let hello = hello(&mut stream, token);
    assert_eq!(hello["ok"], true);
    assert_eq!(hello["protocol_version"], 1);
    let epoch = hello["host_epoch"]
        .as_str()
        .expect("host_epoch")
        .to_string();
    assert!(!epoch.is_empty());
    assert!(hello["version"].as_str().is_some());

    send_json(&mut stream, &json!({"method": "ping"}));
    let ping = recv_json(&mut stream);
    assert_eq!(ping["ok"], true);
    assert_eq!(ping["host_epoch"], epoch);
    assert_eq!(ping["host_pid"].as_u64().unwrap(), u64::from(child.id()));
    assert!(ping["version"].as_str().is_some());

    send_json(&mut stream, &json!({"method": "list"}));
    let list = recv_json(&mut stream);
    assert_eq!(list["ok"], true);
    assert_eq!(list["terminals"], json!([]));

    send_json(
        &mut stream,
        &json!({"method": "host_shutdown", "grace_ms": 50}),
    );
    let shutdown = recv_json(&mut stream);
    assert_eq!(shutdown["ok"], true);
    assert_eq!(shutdown["accepted"], true);
    assert_eq!(shutdown["draining"], true);

    assert!(
        wait_exit(&mut child, Duration::from_secs(5)).is_some(),
        "host must exit after accepted host_shutdown"
    );
    assert!(!control.exists() || UnixListener::bind(&control).is_ok());
}

#[test]
fn host_shutdown_drains_and_is_idempotent() {
    let dir = tempfile::tempdir().expect("tempdir");
    let token = "control-token-drain";
    write_token(dir.path(), token);
    let mut child = spawn_host(dir.path());
    let control = dir.path().join(CONTROL_SOCKET);
    let frames = dir.path().join(FRAMES_SOCKET);
    wait_socket(&control);
    wait_socket(&frames);

    // Unauthenticated host_shutdown is refused like any other verb.
    {
        let mut stream = connect(&control);
        send_json(
            &mut stream,
            &json!({"method": "host_shutdown", "grace_ms": 20}),
        );
        let reply = recv_json(&mut stream);
        assert_eq!(reply["ok"], false);
        assert_eq!(reply["error"], "unauthenticated");
    }

    let mut stream = connect(&control);
    let hello = hello(&mut stream, token);
    assert_eq!(hello["ok"], true);

    send_json(
        &mut stream,
        &json!({"method": "host_shutdown", "grace_ms": 200}),
    );
    let first = recv_json(&mut stream);
    assert_eq!(first["ok"], true);
    assert_eq!(first["accepted"], true);
    assert_eq!(first["draining"], true);

    send_json(
        &mut stream,
        &json!({"method": "host_shutdown", "grace_ms": 200}),
    );
    let second = recv_json(&mut stream);
    assert_eq!(second["ok"], true);
    assert_eq!(second["accepted"], true);
    assert_eq!(second["draining"], true);

    send_json(&mut stream, &json!({"method": "spawn"}));
    let spawn = recv_json(&mut stream);
    assert_eq!(spawn["ok"], false);
    assert_eq!(spawn["error"], "host_draining");

    send_json(&mut stream, &json!({"method": "attach"}));
    let attach = recv_json(&mut stream);
    assert_eq!(attach["ok"], false);
    assert_eq!(attach["error"], "host_draining");

    // Lost response plus verified host death is success for the caller.
    drop(stream);
    assert!(
        wait_exit(&mut child, Duration::from_secs(5)).is_some(),
        "host must exit after drain even if the response socket is dropped"
    );
    assert_host_dead(dir.path(), child.id());
}

fn assert_host_dead(socket_dir: &Path, pid: u32) {
    let pidfile = socket_dir.join(PID_FILE);
    if pidfile.exists() {
        let text = std::fs::read_to_string(&pidfile).unwrap_or_default();
        if let Ok(stored) = text.trim().parse::<u32>() {
            assert_ne!(
                stored, pid,
                "pidfile must not still name the drained host pid"
            );
        }
    }
    let control = socket_dir.join(CONTROL_SOCKET);
    assert!(
        UnixListener::bind(&control).is_ok() || !control.exists(),
        "control socket must be gone after host death"
    );
    #[cfg(unix)]
    {
        let alive = unsafe { libc::kill(pid as i32, 0) == 0 };
        assert!(!alive, "host pid {pid} must be gone");
    }
}
