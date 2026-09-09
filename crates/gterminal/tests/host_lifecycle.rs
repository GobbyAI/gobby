//! Host process lifecycle: sockets, ping/list, shutdown drain (plan 3.1.1, 3.1.22).

#![cfg(unix)]

mod host_support;

use host_support::{
    connect, recv_json, send_json, socket_mode, spawn_host, wait_exit, wait_socket, wait_until,
    write_token, CONTROL_SOCKET, FRAMES_SOCKET, PID_FILE,
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

/// #22002: the host outlives its daemon. A restarted daemon re-adopts the same
/// host (same epoch, pid, terminals) with the same token, and a gclient frames
/// attachment opened before the disconnect keeps working throughout.
#[test]
fn host_survives_daemon_disconnect_and_readopts() {
    use gobby_terminal::protocol::{
        read_message, write_message, ClientMessage, RenderEncoding, ServerMessage, MAX_FRAME_SIZE,
        PROTOCOL_VERSION,
    };
    use std::io::{ErrorKind, Read, Write};

    const LOCAL: &str = "local-token-readopt";

    let dir = tempfile::tempdir().expect("tempdir");
    let token = "control-token-readopt";
    write_token(dir.path(), token);
    std::fs::write(dir.path().join("local_cli_token"), LOCAL).expect("local token");
    let mut child = spawn_host(dir.path());
    let control = dir.path().join(CONTROL_SOCKET);
    let frames = dir.path().join(FRAMES_SOCKET);
    wait_socket(&control);
    wait_socket(&frames);
    let pidfile = dir.path().join(PID_FILE);
    let pid_before = std::fs::read_to_string(&pidfile).expect("pidfile");

    // Daemon #1 adopts the host and spawns a committed terminal.
    let mut first = connect(&control);
    let hello_one = hello(&mut first, token);
    assert_eq!(hello_one["ok"], true, "{hello_one}");
    let epoch = hello_one["host_epoch"].as_str().expect("epoch").to_string();
    send_json(
        &mut first,
        &json!({
            "method": "reserve_observer",
            "terminal_id": "term-readopt",
            "reserve_key": "rk",
        }),
    );
    let reserved = recv_json(&mut first);
    let reservation_id = reserved["reservation_id"]
        .as_str()
        .expect("reservation id")
        .to_string();
    send_json(
        &mut first,
        &json!({
            "method": "spawn",
            "operation_seq": 1,
            "terminal_id": "term-readopt",
            "spawn_key": "sk",
            "reservation_id": reservation_id,
            "reserve_key": "rk",
            "argv": ["/bin/sleep", "30"],
            "cwd": "/",
            "rows": 24,
            "cols": 80,
            "commit_deadline_ms": 8000,
        }),
    );
    let prepared = recv_json(&mut first);
    assert_eq!(prepared["ok"], true, "{prepared}");
    let host_terminal_id = prepared["host_terminal_id"]
        .as_str()
        .expect("host terminal id")
        .to_string();
    send_json(
        &mut first,
        &json!({"method": "spawn_commit", "terminal_id": "term-readopt", "spawn_key": "sk"}),
    );
    let committed = recv_json(&mut first);
    assert_eq!(committed["ok"], true, "{committed}");

    // A gclient attaches on the frames socket while daemon #1 is still up.
    let mut viewer = connect(&frames);
    let mut buf = Vec::new();
    write_message(
        &mut buf,
        &ClientMessage::Hello {
            version: PROTOCOL_VERSION,
            encoding: RenderEncoding::SemanticFrame,
            local_token: LOCAL.into(),
            cols: 80,
            rows: 24,
            tmux_identity: None,
        },
    )
    .expect("encode hello");
    write_message(
        &mut buf,
        &ClientMessage::AttachTerminal {
            host_terminal_id: host_terminal_id.clone(),
            reservation_id: None,
            locator: None,
        },
    )
    .expect("encode attach");
    viewer.write_all(&buf).expect("send hello+attach");
    viewer
        .set_read_timeout(Some(Duration::from_secs(5)))
        .expect("read timeout");
    match read_message(&mut viewer, MAX_FRAME_SIZE).expect("welcome") {
        ServerMessage::Welcome { host_epoch } => assert_eq!(host_epoch, epoch),
        other => panic!("expected welcome: {other:?}"),
    }
    match read_message(&mut viewer, MAX_FRAME_SIZE).expect("attached") {
        ServerMessage::Attached {
            host_terminal_id: attached,
            ..
        } => assert_eq!(attached, host_terminal_id),
        other => panic!("expected attached: {other:?}"),
    }

    // Daemon #1 also holds an unprepared reservation; the host drops it when it
    // processes the disconnect, which is the signal that the disconnect landed.
    send_json(
        &mut first,
        &json!({
            "method": "reserve_observer",
            "terminal_id": "term-readopt-pending",
            "reserve_key": "rk-pending",
        }),
    );
    let pending = recv_json(&mut first);
    let pending_reservation = pending["reservation_id"]
        .as_str()
        .expect("pending reservation id")
        .to_string();

    // Daemon #1 stops: its control connection goes away with no host_shutdown.
    drop(first);

    // Daemon #2 adopts the same host with the same token and epoch.
    let mut second = connect(&control);
    let hello_two = hello(&mut second, token);
    assert_eq!(hello_two["ok"], true, "{hello_two}");
    assert_eq!(
        hello_two["host_epoch"], epoch,
        "epoch continuity across daemons"
    );
    // A wrong-key release reports `released: false` while the reservation
    // exists and `released: true` once the host has reaped it on disconnect.
    wait_until("host to process daemon #1's disconnect", || {
        send_json(
            &mut second,
            &json!({
                "method": "release_observer",
                "reservation_id": pending_reservation,
                "reserve_key": "wrong-key",
            }),
        );
        recv_json(&mut second)["released"] == true
    });
    assert!(
        child.try_wait().expect("try_wait").is_none(),
        "host must keep running after its daemon disconnects"
    );
    assert_eq!(
        std::fs::read_to_string(&pidfile).expect("pidfile after disconnect"),
        pid_before,
        "pidfile must keep naming the surviving host"
    );
    send_json(&mut second, &json!({"method": "ping"}));
    let ping = recv_json(&mut second);
    assert_eq!(ping["ok"], true);
    assert_eq!(ping["host_epoch"], epoch);
    assert_eq!(ping["host_pid"].as_u64().unwrap(), u64::from(child.id()));
    send_json(&mut second, &json!({"method": "list"}));
    let list = recv_json(&mut second);
    assert_eq!(list["ok"], true);
    let rows = list["terminals"].as_array().expect("terminal rows");
    assert!(
        rows.iter()
            .any(|row| row["host_terminal_id"] == host_terminal_id
                && row["commit_state"] == "committed"),
        "the committed terminal must survive the daemon handover: {list}"
    );

    // The pre-restart attachment is still open (a read must not report EOF)...
    viewer
        .set_read_timeout(Some(Duration::from_millis(200)))
        .expect("peek timeout");
    let mut peek = [0u8; 1];
    match viewer.read(&mut peek) {
        Ok(0) => panic!("host closed the frames attachment during the daemon handover"),
        Ok(_) => {}
        Err(err) => assert!(
            matches!(err.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut),
            "unexpected frames error: {err}"
        ),
    }
    // ...and the terminal still accepts new attachments after re-adoption.
    let mut late = connect(&frames);
    let mut buf = Vec::new();
    write_message(
        &mut buf,
        &ClientMessage::Hello {
            version: PROTOCOL_VERSION,
            encoding: RenderEncoding::SemanticFrame,
            local_token: LOCAL.into(),
            cols: 80,
            rows: 24,
            tmux_identity: None,
        },
    )
    .expect("encode late hello");
    write_message(
        &mut buf,
        &ClientMessage::AttachTerminal {
            host_terminal_id: host_terminal_id.clone(),
            reservation_id: None,
            locator: None,
        },
    )
    .expect("encode late attach");
    late.write_all(&buf).expect("send late hello+attach");
    late.set_read_timeout(Some(Duration::from_secs(5)))
        .expect("late read timeout");
    let _: ServerMessage = read_message(&mut late, MAX_FRAME_SIZE).expect("late welcome");
    match read_message(&mut late, MAX_FRAME_SIZE).expect("late attached") {
        ServerMessage::Attached {
            host_terminal_id: attached,
            ..
        } => assert_eq!(attached, host_terminal_id),
        other => panic!("expected late attached: {other:?}"),
    }

    // Only an explicit host_shutdown takes the host down.
    send_json(
        &mut second,
        &json!({"method": "host_shutdown", "grace_ms": 50}),
    );
    let shutdown = recv_json(&mut second);
    assert_eq!(shutdown["ok"], true);
    assert!(
        wait_exit(&mut child, Duration::from_secs(5)).is_some(),
        "host must exit after an explicit host_shutdown"
    );
}
