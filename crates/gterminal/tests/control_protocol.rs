//! Control-socket authentication and typed refusal (plan 3.1.10).

#![cfg(unix)]

mod embed_support;
mod host_support;

use host_support::{
    connect, recv_json, send_json, send_json_without_id, spawn_host, temp_socket_dir, wait_exit,
    wait_socket, wait_until, write_token, CONTROL_SOCKET,
};
use serde_json::json;
use std::collections::HashMap;
use std::io::{Read, Write};
use std::time::Duration;

const LOCAL_CLI_TOKEN: &str = "local-cli-token-must-not-authenticate-control";
const MAX_CONTROL_LINE: usize = 2 * 1024 * 1024;

#[test]
fn hello_required_before_any_verb() {
    let dir = temp_socket_dir();
    let token = "control-token-hello-required";
    write_token(dir.path(), token);
    let mut child = spawn_host(dir.path());
    let control = dir.path().join(CONTROL_SOCKET);
    wait_socket(&control);

    {
        let mut stream = connect(&control);
        send_json(&mut stream, &json!({"method": "ping"}));
        let reply = recv_json(&mut stream);
        assert_eq!(reply["ok"], false);
        assert_eq!(reply["error"], "unauthenticated");
        let mut buf = [0u8; 8];
        let n = stream.read(&mut buf).unwrap_or(0);
        assert_eq!(n, 0, "unauthenticated connection must close");
    }

    {
        let mut stream = connect(&control);
        send_json(
            &mut stream,
            &json!({
                "method": "hello",
                "protocol_version": 1,
                "control_token": "wrong-token",
            }),
        );
        let reply = recv_json(&mut stream);
        assert_eq!(reply["ok"], false);
        assert_eq!(reply["error"], "invalid_token");
        assert!(reply.get("terminals").is_none());
        assert!(reply.get("host_epoch").is_none());
    }

    {
        let mut stream = connect(&control);
        send_json(
            &mut stream,
            &json!({
                "method": "hello",
                "protocol_version": 999,
                "control_token": token,
            }),
        );
        let reply = recv_json(&mut stream);
        assert_eq!(reply["ok"], false);
        assert_eq!(reply["error"], "unsupported_protocol");
        assert!(reply.get("terminals").is_none());
    }

    {
        let mut stream = connect(&control);
        send_json(
            &mut stream,
            &json!({
                "method": "hello",
                "protocol_version": 1,
                "control_token": LOCAL_CLI_TOKEN,
            }),
        );
        let reply = recv_json(&mut stream);
        assert_eq!(reply["ok"], false);
        assert_eq!(reply["error"], "invalid_token");
    }

    let mut stream = connect(&control);
    send_json(
        &mut stream,
        &json!({
            "method": "hello",
            "protocol_version": 1,
            "control_token": token,
        }),
    );
    let hello = recv_json(&mut stream);
    assert_eq!(hello["ok"], true);
    assert_eq!(hello["protocol_version"], 1);
    assert!(hello["host_epoch"].as_str().unwrap().len() > 8);
    assert!(hello["version"].as_str().is_some());

    // Request correlation made `id` mandatory after this authentication test was
    // written, so keep the changed wire contract explicit here.
    send_json_without_id(&mut stream, &json!({"method": "ping"}));
    let missing_ping = recv_json(&mut stream);
    assert_eq!(missing_ping["ok"], false);
    assert_eq!(missing_ping["error"], "missing_id");

    send_json(&mut stream, &json!({"method": "ping", "id": "authed-ping"}));
    let ping = recv_json(&mut stream);
    assert_eq!(ping["ok"], true);
    assert_eq!(ping["host_epoch"], hello["host_epoch"]);
    assert!(ping["host_pid"].as_u64().unwrap() > 0);

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
        "host must exit after host_shutdown"
    );
}

#[test]
fn oversize_control_line_is_refused_before_parse() {
    let dir = temp_socket_dir();
    let token = "control-token-overflow";
    write_token(dir.path(), token);
    let mut child = spawn_host(dir.path());
    let control = dir.path().join(CONTROL_SOCKET);
    wait_socket(&control);

    for newline_terminated in [true, false] {
        let mut stream = connect(&control);
        let mut line = vec![b'{'; MAX_CONTROL_LINE + 1];
        if newline_terminated {
            line.push(b'\n');
        }
        stream.write_all(&line).expect("write oversized request");
        stream.flush().expect("flush oversized request");

        let reply = recv_json(&mut stream);
        assert_eq!(reply["ok"], false, "{reply}");
        assert_eq!(reply["error"], "control_overflow", "{reply}");
        let mut byte = [0_u8; 1];
        assert_eq!(stream.read(&mut byte).expect("read closed socket"), 0);
    }

    let mut stream = connect(&control);
    send_json(
        &mut stream,
        &json!({
            "method": "hello",
            "protocol_version": 1,
            "control_token": token,
        }),
    );
    assert_eq!(recv_json(&mut stream)["ok"], true);
    send_json(
        &mut stream,
        &json!({"method": "host_shutdown", "grace_ms": 20}),
    );
    let _ = recv_json(&mut stream);
    let _ = wait_exit(&mut child, Duration::from_secs(5));
}

#[test]
fn native_verbs_refuse_tmux_terminals() {
    let pane = embed_support::start_tmux();
    let host = embed_support::spawn_host(&[]);
    let mut frames = embed_support::connect_frames(&host, None);
    let attached = embed_support::attach(&mut frames, pane.locator());
    let host_terminal_id = match attached {
        gobby_terminal::protocol::ServerMessage::Attached {
            host_terminal_id, ..
        } => host_terminal_id,
        other => panic!("expected attached, got {other:?}"),
    };
    let pane_before = pane.snapshot();
    let mut control = embed_support::control(&host);
    let requests = [
        json!({
            "method": "kill",
            "operation_seq": 1,
            "host_terminal_id": host_terminal_id,
            "grace_ms": 0,
        }),
        json!({
            "method": "write",
            "operation_seq": 2,
            "host_terminal_id": host_terminal_id,
            "kind": "text",
            "encoding": "utf8-b64",
            "data": "eA==",
        }),
        json!({
            "method": "write",
            "operation_seq": 3,
            "host_terminal_id": host_terminal_id,
            "kind": "paste",
            "encoding": "utf8-b64",
            "data": "eA==",
        }),
        json!({
            "method": "resize",
            "operation_seq": 4,
            "host_terminal_id": host_terminal_id,
            "rows": 12,
            "cols": 40,
        }),
        json!({
            "method": "snapshot",
            "host_terminal_id": host_terminal_id,
            "max_bytes": 128,
            "max_lines": 8,
        }),
        json!({
            "method": "reserve_observer",
            "terminal_id": host_terminal_id,
            "reserve_key": "tmux-must-not-reserve",
        }),
        json!({
            "method": "release_observer",
            "host_terminal_id": host_terminal_id,
            "reservation_id": "tmux-must-not-release",
            "reserve_key": "tmux-must-not-release",
        }),
    ];
    for request in requests {
        embed_support::send_json(&mut control, &request);
        let reply = embed_support::recv_json(&mut control);
        assert_eq!(
            reply["error"], "not_native",
            "request={request} reply={reply}"
        );
    }

    embed_support::send_json(&mut control, &json!({"method": "ping"}));
    assert_eq!(embed_support::recv_json(&mut control)["ok"], true);
    embed_support::send_json(&mut control, &json!({"method": "list"}));
    let listed = embed_support::recv_json(&mut control);
    assert!(listed["terminals"].as_array().is_some_and(|rows| {
        rows.iter()
            .any(|row| row["host_terminal_id"] == host_terminal_id)
    }));
    assert_eq!(pane.snapshot(), pane_before);
}

fn authed(
    dir: &std::path::Path,
    token: &str,
) -> (host_support::HostProc, std::os::unix::net::UnixStream) {
    let child = spawn_host(dir);
    let control = dir.join(CONTROL_SOCKET);
    wait_socket(&control);
    let mut stream = connect(&control);
    send_json(
        &mut stream,
        &json!({
            "method": "hello",
            "protocol_version": 1,
            "control_token": token,
            "id": "h1",
        }),
    );
    let hello = recv_json(&mut stream);
    assert_eq!(hello["ok"], true);
    (child, stream)
}

fn seq_spawn(
    stream: &mut std::os::unix::net::UnixStream,
    seq: u64,
    extra: serde_json::Value,
) -> serde_json::Value {
    let mut req = extra;
    if let Some(obj) = req.as_object_mut() {
        obj.insert("method".into(), json!("spawn"));
        obj.insert("operation_seq".into(), json!(seq));
        obj.insert("id".into(), json!(format!("op-{seq}")));
    }
    send_json(stream, &req);
    recv_json(stream)
}

fn recv_response_with_id(
    stream: &mut std::os::unix::net::UnixStream,
    expected_id: &str,
) -> serde_json::Value {
    loop {
        let message = recv_json(stream);
        if message.get("id").and_then(serde_json::Value::as_str) == Some(expected_id) {
            return message;
        }
        assert!(
            message.get("event").is_some(),
            "unexpected control message: {message}"
        );
    }
}

#[test]
fn requests_require_unique_ids() {
    let dir = temp_socket_dir();
    let token = "control-token-request-ids";
    write_token(dir.path(), token);
    let (mut child, mut stream) = authed(dir.path(), token);

    send_json_without_id(&mut stream, &json!({"method": "ping"}));
    let missing = recv_json(&mut stream);
    assert_eq!(missing["ok"], false, "{missing}");
    assert_eq!(missing["error"], "missing_id", "{missing}");

    send_json(&mut stream, &json!({"method": "ping", "id": "ping-1"}));
    let ping = recv_json(&mut stream);
    assert_eq!(ping["ok"], true, "{ping}");
    assert_eq!(ping["id"], "ping-1", "{ping}");

    send_json(
        &mut stream,
        &json!({
            "method": "reserve_observer",
            "id": "reserve-duplicate",
            "terminal_id": "duplicate-terminal",
            "reserve_key": "duplicate-terminal",
        }),
    );
    let reserved = recv_json(&mut stream);
    let reservation_id = reserved["reservation_id"].as_str().unwrap();
    let prepared = seq_spawn(
        &mut stream,
        1,
        json!({
            "terminal_id": "duplicate-terminal",
            "spawn_key": "duplicate-spawn",
            "reservation_id": reservation_id,
            "reserve_key": "duplicate-terminal",
            "argv": ["/bin/sh", "-c", "exec sleep 30"],
            "env": {"GTERM_GATE_FAULT": "delay", "PATH": "/bin:/usr/bin"},
            "cwd": dir.path().to_string_lossy(),
            "rows": 24,
            "cols": 80,
            "commit_deadline_ms": 5000,
        }),
    );
    child.track_pgid(prepared["pgid"].as_i64().unwrap() as i32);
    for _ in 0..2 {
        send_json(
            &mut stream,
            &json!({
                "method": "spawn_commit",
                "id": "duplicate-commit",
                "terminal_id": "duplicate-terminal",
                "spawn_key": "duplicate-spawn",
            }),
        );
    }
    let duplicate = recv_json(&mut stream);
    assert_eq!(duplicate["id"], "duplicate-commit", "{duplicate}");
    assert_eq!(duplicate["error"], "duplicate_id", "{duplicate}");
    let committed = recv_json(&mut stream);
    assert_eq!(committed["id"], "duplicate-commit", "{committed}");

    send_json(
        &mut stream,
        &json!({"method": "host_shutdown", "id": "shutdown", "grace_ms": 50}),
    );
    let shutdown = recv_json(&mut stream);
    assert_eq!(shutdown["id"], "shutdown", "{shutdown}");
    assert!(wait_exit(&mut child, Duration::from_secs(5)).is_some());
}

#[test]
fn child_exit_emits_terminal_exited_on_event_stream() {
    let dir = temp_socket_dir();
    let token = "control-token-native-exit-event";
    write_token(dir.path(), token);
    let (mut child, mut requests) = authed(dir.path(), token);
    let control = dir.path().join(CONTROL_SOCKET);
    let mut events = connect(&control);
    send_json(
        &mut events,
        &json!({
            "method": "hello",
            "id": "event-hello",
            "protocol_version": 1,
            "control_token": token,
        }),
    );
    assert_eq!(recv_json(&mut events)["ok"], true);
    send_json(
        &mut events,
        &json!({"method": "subscribe_events", "id": "event-subscribe"}),
    );
    let subscribed = recv_json(&mut events);
    assert_eq!(subscribed["id"], "event-subscribe", "{subscribed}");
    assert_eq!(subscribed["gap"], false, "{subscribed}");

    send_json(
        &mut requests,
        &json!({
            "method": "reserve_observer",
            "id": "exit-reserve",
            "terminal_id": "exit-terminal",
            "reserve_key": "exit-terminal",
        }),
    );
    let reserved = recv_json(&mut requests);
    let reservation_id = reserved["reservation_id"].as_str().unwrap();
    let prepared = seq_spawn(
        &mut requests,
        1,
        json!({
            "terminal_id": "exit-terminal",
            "spawn_key": "exit-spawn",
            "reservation_id": reservation_id,
            "reserve_key": "exit-terminal",
            "argv": ["/bin/sh", "-c", "exit 7"],
            "env": {"GTERM_TEST_WAIT_FOR_CHILD_EXIT_BEFORE_STATUS": "1"},
            "cwd": dir.path().to_string_lossy(),
            "rows": 24,
            "cols": 80,
            "commit_deadline_ms": 5000,
        }),
    );
    child.track_pgid(prepared["pgid"].as_i64().unwrap() as i32);
    let host_terminal_id = prepared["host_terminal_id"].as_str().unwrap();
    send_json(
        &mut requests,
        &json!({
            "method": "spawn_commit",
            "id": "exit-commit",
            "terminal_id": "exit-terminal",
            "spawn_key": "exit-spawn",
        }),
    );
    let committed = recv_json(&mut requests);
    assert_eq!(committed["id"], "exit-commit", "{committed}");
    assert_eq!(committed["ok"], true, "{committed}");

    let event = recv_json(&mut events);
    assert_eq!(event["event"], "terminal_exited", "{event}");
    assert_eq!(event["terminal_id"], "exit-terminal", "{event}");
    assert_eq!(event["host_terminal_id"], host_terminal_id, "{event}");
    assert_eq!(event["exit_code"], 7, "{event}");
    assert!(event.get("id").is_none(), "{event}");

    for stream in [&mut requests, &mut events] {
        stream
            .set_read_timeout(Some(Duration::from_millis(100)))
            .unwrap();
        let mut byte = [0_u8; 1];
        let no_more = stream.read(&mut byte).unwrap_err();
        assert!(
            matches!(
                no_more.kind(),
                std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
            ),
            "unexpected extra control message: {no_more}"
        );
        stream.set_read_timeout(None).unwrap();
    }

    send_json(
        &mut requests,
        &json!({"method": "host_shutdown", "id": "exit-shutdown", "grace_ms": 50}),
    );
    assert_eq!(recv_json(&mut requests)["id"], "exit-shutdown");
    assert!(wait_exit(&mut child, Duration::from_secs(5)).is_some());
}

#[test]
fn commit_wait_does_not_block_other_requests() {
    let dir = temp_socket_dir();
    let token = "control-token-concurrent-commit";
    write_token(dir.path(), token);
    let (mut child, mut stream) = authed(dir.path(), token);

    send_json(
        &mut stream,
        &json!({
            "method": "reserve_observer",
            "id": "wait-reserve",
            "terminal_id": "wait-terminal",
            "reserve_key": "wait-terminal",
        }),
    );
    let reserved = recv_json(&mut stream);
    let prepared = seq_spawn(
        &mut stream,
        1,
        json!({
            "terminal_id": "wait-terminal",
            "spawn_key": "wait-spawn",
            "reservation_id": reserved["reservation_id"],
            "reserve_key": "wait-terminal",
            "argv": ["/bin/sh", "-c", "exec sleep 30"],
            "env": {"GTERM_GATE_FAULT": "timeout", "PATH": "/bin:/usr/bin"},
            "cwd": dir.path().to_string_lossy(),
            "rows": 24,
            "cols": 80,
            "commit_deadline_ms": 5000,
        }),
    );
    child.track_pgid(prepared["pgid"].as_i64().unwrap() as i32);
    let host_terminal_id = prepared["host_terminal_id"].as_str().unwrap();
    send_json(
        &mut stream,
        &json!({
            "method": "spawn_commit",
            "id": "waiting-commit",
            "terminal_id": "wait-terminal",
            "spawn_key": "wait-spawn",
        }),
    );
    send_json(
        &mut stream,
        &json!({"method": "ping", "id": "parallel-ping"}),
    );
    send_json(
        &mut stream,
        &json!({"method": "list", "id": "parallel-list"}),
    );
    send_json(
        &mut stream,
        &json!({
            "method": "resize",
            "id": "parallel-resize",
            "operation_seq": 2,
            "host_terminal_id": host_terminal_id,
            "rows": 25,
            "cols": 81,
        }),
    );

    let mut parallel = HashMap::new();
    for _ in 0..3 {
        let response = recv_json(&mut stream);
        parallel.insert(response["id"].as_str().unwrap().to_string(), response);
    }
    for id in ["parallel-ping", "parallel-list", "parallel-resize"] {
        assert_eq!(parallel[id]["ok"], true, "{}", parallel[id]);
    }
    assert!(!parallel.contains_key("waiting-commit"));

    let delayed_operations: Vec<_> = (0..3)
        .map(|_| {
            json!({
                "kind": "text",
                "encoding": "utf8-b64",
                "data": "eA==",
                "delay_ms": 1000,
            })
        })
        .collect();
    send_json(
        &mut stream,
        &json!({
            "method": "write_batch",
            "id": "ordered-batch",
            "operation_seq": 3,
            "targets": [{
                "recipient_id": "wait-terminal",
                "host_terminal_id": host_terminal_id,
                "operations": delayed_operations,
            }],
        }),
    );
    for operation_seq in 4..=65 {
        send_json(
            &mut stream,
            &json!({
                "method": "resize",
                "id": format!("queued-{operation_seq}"),
                "operation_seq": operation_seq,
                "host_terminal_id": host_terminal_id,
                "rows": 25,
                "cols": 81,
            }),
        );
    }
    send_json(
        &mut stream,
        &json!({"method": "ping", "id": "inflight-limit"}),
    );
    let mut completed = Vec::new();
    let limited = loop {
        let response = recv_json(&mut stream);
        if response["id"] == "inflight-limit" {
            break response;
        }
        completed.push(response);
    };
    assert_eq!(limited["error"], "too_many_inflight");

    let mut ordered_ids = Vec::new();
    while completed.len() < 63 {
        completed.push(recv_json(&mut stream));
    }
    for response in completed {
        assert_eq!(response["ok"], true, "{response}");
        assert_ne!(response["id"], "waiting-commit", "{response}");
        ordered_ids.push(response["id"].as_str().unwrap().to_string());
    }
    let mut expected_ids = vec!["ordered-batch".to_string()];
    expected_ids.extend((4..=65).map(|seq| format!("queued-{seq}")));
    assert_eq!(ordered_ids, expected_ids);

    send_json(
        &mut stream,
        &json!({
            "method": "kill",
            "id": "parallel-kill",
            "operation_seq": 66,
            "host_terminal_id": host_terminal_id,
            "grace_ms": 0,
        }),
    );
    let mut final_responses = HashMap::new();
    for _ in 0..2 {
        let response = recv_json(&mut stream);
        final_responses.insert(response["id"].as_str().unwrap().to_string(), response);
    }
    let killed = &final_responses["parallel-kill"];
    assert_eq!(killed["ok"], true);
    let commit = &final_responses["waiting-commit"];
    assert_eq!(commit["error"], "not_found", "{commit}");

    send_json(
        &mut stream,
        &json!({"method": "host_shutdown", "id": "wait-shutdown", "grace_ms": 50}),
    );
    assert_eq!(recv_json(&mut stream)["id"], "wait-shutdown");
    assert!(wait_exit(&mut child, Duration::from_secs(5)).is_some());
}

#[test]
fn tmux_pane_death_emits_no_control_event() {
    use gobby_terminal::protocol::ServerMessage;

    let pane = embed_support::start_tmux();
    let host = embed_support::spawn_host(&[]);
    let mut control = embed_support::control(&host);
    embed_support::send_json(
        &mut control,
        &json!({"method": "subscribe_events", "id": "tmux-events"}),
    );
    assert_eq!(embed_support::recv_json(&mut control)["ok"], true);
    let mut frames = embed_support::connect_frames(&host, None);
    let _ = embed_support::attach(&mut frames, pane.locator());

    pane.tmux(&["kill-server"]);
    let frame_exit = (0..20).any(|_| {
        matches!(
            embed_support::read_msg_timeout(&mut frames, Duration::from_millis(150)),
            Some(ServerMessage::TerminalExited { .. })
        )
    });
    assert!(frame_exit, "tmux death did not reach the frame stream");

    control
        .set_read_timeout(Some(Duration::from_millis(150)))
        .unwrap();
    let mut byte = [0_u8; 1];
    let error = control.read(&mut byte).unwrap_err();
    assert!(matches!(
        error.kind(),
        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
    ));
}

#[test]
fn control_surface_round_trip() {
    let dir = temp_socket_dir();
    let token = "control-token-surface";
    write_token(dir.path(), token);
    let (mut child, mut stream) = authed(dir.path(), token);

    send_json(&mut stream, &json!({"method": "ping", "id": "p1"}));
    let ping = recv_json(&mut stream);
    assert_eq!(ping["ok"], true);
    assert!(ping["host_pid"].as_u64().unwrap() > 0);
    assert_eq!(ping["id"], "p1");

    send_json(
        &mut stream,
        &json!({
            "method": "reserve_observer",
            "id": "r1",
            "terminal_id": "term-1",
            "reserve_key": "rk-1",
        }),
    );
    let reserved = recv_json(&mut stream);
    assert_eq!(reserved["ok"], true);
    let reservation_id = reserved["reservation_id"].as_str().unwrap().to_string();

    let prepared = seq_spawn(
        &mut stream,
        1,
        json!({
            "terminal_id": "term-1",
            "spawn_key": "sk-1",
            "reservation_id": reservation_id,
            "reserve_key": "rk-1",
            "argv": ["/bin/sh", "-c", "printf 'gterm-hi\\n'; exec cat"],
            "cwd": dir.path().to_string_lossy(),
            "rows": 24,
            "cols": 80,
            "commit_deadline_ms": 5000,
        }),
    );
    assert_eq!(prepared["ok"], true, "{prepared}");
    assert_eq!(prepared["method"], "spawn_prepared");
    let host_terminal_id = prepared["host_terminal_id"].as_str().unwrap().to_string();
    assert!(prepared["pgid"].as_i64().unwrap() > 0);

    send_json(
        &mut stream,
        &json!({
            "method": "spawn_commit",
            "id": "c1",
            "terminal_id": "term-1",
            "spawn_key": "sk-1",
        }),
    );
    let committed = recv_json(&mut stream);
    assert_eq!(committed["ok"], true, "{committed}");
    assert_eq!(committed["host_terminal_id"], host_terminal_id);

    send_json(
        &mut stream,
        &json!({
            "method": "write",
            "id": "w1",
            "operation_seq": 2,
            "host_terminal_id": host_terminal_id,
            "kind": "text",
            "encoding": "utf8-b64",
            "data": "eA==",
            "submit": false,
        }),
    );
    let written = recv_json(&mut stream);
    assert_eq!(written["ok"], true, "{written}");

    send_json(
        &mut stream,
        &json!({
            "method": "write_batch",
            "id": "wb1",
            "operation_seq": 3,
            "targets": [
                {
                    "recipient_id": "recipient-live",
                    "host_terminal_id": host_terminal_id,
                    "operations": [
                        {"kind": "text", "encoding": "utf8-b64", "data": "eQ==", "delay_ms": 0},
                        {"kind": "key", "encoding": "utf8-b64", "data": "ZW50ZXI=", "delay_ms": 0},
                    ],
                },
                {
                    "recipient_id": "recipient-missing",
                    "host_terminal_id": "missing-terminal",
                    "operations": [
                        {"kind": "text", "encoding": "utf8-b64", "data": "eg==", "delay_ms": 0},
                    ],
                },
            ],
        }),
    );
    let batch = recv_json(&mut stream);
    assert_eq!(batch["ok"], true, "{batch}");
    assert_eq!(batch["results"][0]["recipient_id"], "recipient-live");
    assert_eq!(batch["results"][0]["written"], true);
    assert_eq!(batch["results"][1]["recipient_id"], "recipient-missing");
    assert_eq!(batch["results"][1]["error"], "not_found");
    assert_eq!(batch["results"][1]["stage"], "none");

    send_json(
        &mut stream,
        &json!({
            "method": "resize",
            "id": "z1",
            "operation_seq": 4,
            "host_terminal_id": host_terminal_id,
            "rows": 30,
            "cols": 100,
        }),
    );
    let resized = recv_json(&mut stream);
    assert_eq!(resized["ok"], true, "{resized}");

    send_json(
        &mut stream,
        &json!({
            "method": "snapshot",
            "id": "s1",
            "host_terminal_id": host_terminal_id,
            "mode": "text",
            "max_bytes": 4096,
            "max_lines": 50,
        }),
    );
    let snap = recv_json(&mut stream);
    assert_eq!(snap["ok"], true, "{snap}");
    assert!(snap.get("truncated").is_some());

    send_json(
        &mut stream,
        &json!({
            "method": "subscribe_events",
            "id": "e1",
        }),
    );
    let sub = recv_json(&mut stream);
    assert_eq!(sub["ok"], true, "{sub}");

    send_json(
        &mut stream,
        &json!({
            "method": "release_observer",
            "id": "rel1",
            "reservation_id": reservation_id,
            "reserve_key": "rk-1",
        }),
    );
    let _ = recv_json(&mut stream);

    send_json(
        &mut stream,
        &json!({
            "method": "kill",
            "id": "k1",
            "operation_seq": 5,
            "host_terminal_id": host_terminal_id,
            "grace_ms": 50,
        }),
    );
    let killed = recv_response_with_id(&mut stream, "k1");
    assert_eq!(killed["ok"], true, "{killed}");

    send_json(
        &mut stream,
        &json!({"method": "host_shutdown", "id": "sd", "grace_ms": 50}),
    );
    let shutdown = recv_response_with_id(&mut stream, "sd");
    assert_eq!(shutdown["ok"], true);
    assert!(wait_exit(&mut child, Duration::from_secs(5)).is_some());
}

#[test]
fn snapshot_truncates_on_char_boundaries() {
    let dir = temp_socket_dir();
    let token = "control-token-snapshot-utf8";
    write_token(dir.path(), token);
    let (mut child, mut stream) = authed(dir.path(), token);
    send_json(
        &mut stream,
        &json!({
            "method": "reserve_observer",
            "terminal_id": "term-utf8",
            "reserve_key": "rk-utf8",
        }),
    );
    let reserved = recv_json(&mut stream);
    let reservation_id = reserved["reservation_id"].as_str().unwrap();
    let prepared = seq_spawn(
        &mut stream,
        1,
        json!({
            "terminal_id": "term-utf8",
            "spawn_key": "sk-utf8",
            "reservation_id": reservation_id,
            "reserve_key": "rk-utf8",
            "argv": ["/bin/sh", "-c", "printf 'éééééé'; exec sleep 30"],
            "cwd": "/",
            "rows": 24,
            "cols": 80,
            "commit_deadline_ms": 8000,
        }),
    );
    assert_eq!(prepared["ok"], true, "{prepared}");
    let host_terminal_id = prepared["host_terminal_id"].as_str().unwrap().to_string();
    send_json(
        &mut stream,
        &json!({
            "method": "spawn_commit",
            "terminal_id": "term-utf8",
            "spawn_key": "sk-utf8",
        }),
    );
    assert_eq!(recv_json(&mut stream)["ok"], true);

    let mut full_snapshot = None;
    wait_until("UTF-8 snapshot output", || {
        send_json(
            &mut stream,
            &json!({
                "method": "snapshot",
                "host_terminal_id": host_terminal_id,
                "max_bytes": 1024 * 1024,
                "max_lines": 50,
            }),
        );
        let snapshot = recv_json(&mut stream);
        if snapshot["text"]
            .as_str()
            .is_some_and(|text| text.contains('é'))
        {
            full_snapshot = Some(snapshot);
            true
        } else {
            false
        }
    });
    let full_snapshot = full_snapshot.expect("UTF-8 snapshot");
    let full_text = full_snapshot["text"].as_str().expect("full snapshot text");
    let inside_multibyte = full_text.find('é').expect("multibyte output") + 1;
    assert!(!full_text.is_char_boundary(inside_multibyte));
    let max_bytes = full_text.len() - inside_multibyte;
    send_json(
        &mut stream,
        &json!({
            "method": "snapshot",
            "host_terminal_id": host_terminal_id,
            "max_bytes": max_bytes,
            "max_lines": 50,
        }),
    );
    let snapshot = recv_json(&mut stream);
    let text = snapshot["text"].as_str().expect("snapshot text");
    assert!(
        snapshot["truncated"].as_bool().unwrap_or(false),
        "{snapshot}"
    );
    assert!(text.len() <= max_bytes, "{snapshot}");
    assert_eq!(
        snapshot["mode"], "text",
        "an omitted mode stays plain text: {snapshot}"
    );
    let styled = snapshot_request(
        &mut stream,
        &host_terminal_id,
        Some(json!("ansi")),
        max_bytes as u64,
        50,
    );
    assert_eq!(styled["mode"], "ansi", "{styled}");
    let styled_text = styled["text"].as_str().expect("snapshot text");
    assert!(styled_text.len() <= max_bytes, "{styled}");

    send_json(
        &mut stream,
        &json!({
            "method": "kill",
            "operation_seq": 2,
            "host_terminal_id": host_terminal_id,
            "grace_ms": 20,
        }),
    );
    let _ = recv_json(&mut stream);
    send_json(
        &mut stream,
        &json!({"method": "host_shutdown", "grace_ms": 20}),
    );
    let _ = recv_json(&mut stream);
    let _ = wait_exit(&mut child, Duration::from_secs(5));
}

#[test]
fn ping_carries_host_pid() {
    let dir = temp_socket_dir();
    let token = "control-token-pid";
    write_token(dir.path(), token);
    let (mut child, mut stream) = authed(dir.path(), token);
    send_json(&mut stream, &json!({"method": "ping"}));
    let ping = recv_json(&mut stream);
    assert!(ping["host_pid"].is_number());
    assert_eq!(ping["host_pid"].as_u64().unwrap(), u64::from(child.id()));
    send_json(
        &mut stream,
        &json!({"method": "host_shutdown", "grace_ms": 20}),
    );
    let _ = recv_json(&mut stream);
    let _ = wait_exit(&mut child, Duration::from_secs(5));
}

#[test]
fn operation_seq_ledger_is_total() {
    let dir = temp_socket_dir();
    let token = "control-token-ledger";
    write_token(dir.path(), token);
    let (mut child, mut stream) = authed(dir.path(), token);
    send_json(
        &mut stream,
        &json!({
            "method": "reserve_observer",
            "terminal_id": "term-ledger",
            "reserve_key": "rk",
        }),
    );
    let reserved = recv_json(&mut stream);
    let reservation_id = reserved["reservation_id"].as_str().unwrap().to_string();
    let spawn_body = json!({
        "terminal_id": "term-ledger",
        "spawn_key": "sk",
        "reservation_id": reservation_id,
        "reserve_key": "rk",
        "argv": ["/bin/sleep", "30"],
        "cwd": "/",
        "rows": 24,
        "cols": 80,
        "commit_deadline_ms": 8000,
    });
    let gap = seq_spawn(&mut stream, 2, spawn_body.clone());
    assert_eq!(gap["error"], "operation_gap");
    let first = seq_spawn(&mut stream, 1, spawn_body.clone());
    assert_eq!(first["ok"], true, "{first}");
    let replay = seq_spawn(&mut stream, 1, spawn_body.clone());
    assert_eq!(replay["host_terminal_id"], first["host_terminal_id"]);
    let mut conflict = spawn_body.clone();
    conflict["argv"] = json!(["/bin/true"]);
    let mismatched = seq_spawn(&mut stream, 1, conflict);
    assert_eq!(mismatched["error"], "operation_conflict");
    send_json(
        &mut stream,
        &json!({"method": "host_shutdown", "grace_ms": 20}),
    );
    let _ = recv_json(&mut stream);
    let _ = wait_exit(&mut child, Duration::from_secs(5));
}

#[test]
fn write_batch_enforces_target_and_operation_limits() {
    let dir = temp_socket_dir();
    let token = "control-token-batch-limits";
    write_token(dir.path(), token);
    let (mut child, mut stream) = authed(dir.path(), token);
    let targets: Vec<_> = (0..65)
        .map(|index| {
            json!({
                "recipient_id": format!("recipient-{index}"),
                "host_terminal_id": format!("terminal-{index}"),
                "operations": [
                    {"kind": "text", "encoding": "utf8-b64", "data": "eA==", "delay_ms": 0},
                ],
            })
        })
        .collect();
    send_json(
        &mut stream,
        &json!({"method": "write_batch", "operation_seq": 1, "targets": targets}),
    );
    let too_many_targets = recv_json(&mut stream);
    assert_eq!(too_many_targets["error"], "too_many_targets");

    let operations: Vec<_> = (0..129)
        .map(|_| json!({"kind": "text", "encoding": "utf8-b64", "data": "eA==", "delay_ms": 0}))
        .collect();
    send_json(
        &mut stream,
        &json!({
            "method": "write_batch",
            "operation_seq": 2,
            "targets": [{
                "recipient_id": "recipient",
                "host_terminal_id": "terminal",
                "operations": operations,
            }],
        }),
    );
    let too_many_operations = recv_json(&mut stream);
    assert_eq!(too_many_operations["ok"], true);
    assert_eq!(
        too_many_operations["results"][0]["error"],
        "too_many_operations"
    );
    assert_eq!(too_many_operations["results"][0]["stage"], "none");

    send_json(
        &mut stream,
        &json!({"method": "host_shutdown", "grace_ms": 20}),
    );
    let _ = recv_json(&mut stream);
    let _ = wait_exit(&mut child, Duration::from_secs(5));
}

#[test]
fn spawn_identity_is_unique_across_connections() {
    let dir = temp_socket_dir();
    let token = "control-token-unique";
    write_token(dir.path(), token);
    let (mut child, mut a) = authed(dir.path(), token);
    send_json(
        &mut a,
        &json!({
            "method": "reserve_observer",
            "terminal_id": "term-u",
            "reserve_key": "rk",
        }),
    );
    let reserved = recv_json(&mut a);
    let reservation_id = reserved["reservation_id"].as_str().unwrap().to_string();
    let body = json!({
        "terminal_id": "term-u",
        "spawn_key": "sk",
        "reservation_id": reservation_id,
        "reserve_key": "rk",
        "argv": ["/bin/sleep", "20"],
        "cwd": "/",
        "rows": 24,
        "cols": 80,
        "commit_deadline_ms": 8000,
    });
    let first = seq_spawn(&mut a, 1, body.clone());
    assert_eq!(first["ok"], true, "{first}");
    let control = dir.path().join(CONTROL_SOCKET);
    let mut b = connect(&control);
    send_json(
        &mut b,
        &json!({
            "method": "hello",
            "protocol_version": 1,
            "control_token": token,
        }),
    );
    assert_eq!(recv_json(&mut b)["ok"], true);
    let second = seq_spawn(&mut b, 1, body);
    assert_eq!(second["host_terminal_id"], first["host_terminal_id"]);
    send_json(&mut a, &json!({"method": "host_shutdown", "grace_ms": 20}));
    let _ = recv_json(&mut a);
    let _ = wait_exit(&mut child, Duration::from_secs(5));
}

#[test]
fn host_config_ranges_reject_and_admit_maximum() {
    let dir = temp_socket_dir();
    let token = "control-token-cfg";
    write_token(dir.path(), token);
    let mut bad = host_support::spawn_host_with_args(dir.path(), &["--max-attachments-total", "3"]);
    assert!(
        wait_exit(&mut bad, Duration::from_secs(3)).is_some(),
        "invalid config must refuse startup"
    );

    let mut child = host_support::spawn_host_with_args(
        dir.path(),
        &[
            "--max-attachments-total",
            "128",
            "--max-attachments-per-terminal",
            "8",
        ],
    );
    let control = dir.path().join(CONTROL_SOCKET);
    wait_socket(&control);
    let mut stream = connect(&control);
    send_json(
        &mut stream,
        &json!({
            "method": "hello",
            "protocol_version": 1,
            "control_token": token,
        }),
    );
    assert_eq!(recv_json(&mut stream)["ok"], true);
    send_json(
        &mut stream,
        &json!({"method": "host_shutdown", "grace_ms": 20}),
    );
    let _ = recv_json(&mut stream);
    let _ = wait_exit(&mut child, Duration::from_secs(5));
}

#[test]
fn list_recovers_every_lifecycle_field() {
    let dir = temp_socket_dir();
    let token = "control-token-list";
    write_token(dir.path(), token);
    let (mut child, mut stream) = authed(dir.path(), token);
    send_json(
        &mut stream,
        &json!({
            "method": "reserve_observer",
            "terminal_id": "term-list",
            "reserve_key": "rk",
        }),
    );
    let reserved = recv_json(&mut stream);
    let reservation_id = reserved["reservation_id"].as_str().unwrap().to_string();
    let prepared = seq_spawn(
        &mut stream,
        1,
        json!({
            "terminal_id": "term-list",
            "spawn_key": "sk",
            "reservation_id": reservation_id,
            "reserve_key": "rk",
            "argv": ["/bin/sleep", "20"],
            "cwd": "/",
            "rows": 24,
            "cols": 80,
            "commit_deadline_ms": 8000,
        }),
    );
    assert_eq!(prepared["ok"], true, "{prepared}");
    send_json(&mut stream, &json!({"method": "list"}));
    let list = recv_json(&mut stream);
    let row = &list["terminals"][0];
    for key in [
        "host_terminal_id",
        "terminal_id",
        "spawn_key",
        "title",
        "rows",
        "cols",
        "pgid",
        "start_time",
        "last_seq",
        "commit_state",
        "observer_bind",
        "observation_state",
        "observation_reason",
        "observation_generation",
    ] {
        assert!(row.get(key).is_some(), "missing {key} in {row}");
    }
    assert_eq!(row["commit_state"], "prepared");
    assert_eq!(row["observer_bind"], "reserved");
    send_json(
        &mut stream,
        &json!({"method": "host_shutdown", "grace_ms": 20}),
    );
    let _ = recv_json(&mut stream);
    let _ = wait_exit(&mut child, Duration::from_secs(5));
}

#[test]
fn spawn_selects_named_reservation() {
    let dir = temp_socket_dir();
    let token = "control-token-named";
    write_token(dir.path(), token);
    let (mut child, mut stream) = authed(dir.path(), token);
    let missing = seq_spawn(
        &mut stream,
        1,
        json!({
            "terminal_id": "term-n",
            "spawn_key": "sk",
            "argv": ["/bin/true"],
            "cwd": "/",
            "rows": 24,
            "cols": 80,
        }),
    );
    assert_eq!(missing["error"], "invalid_reservation");
    send_json(
        &mut stream,
        &json!({"method": "host_shutdown", "grace_ms": 20}),
    );
    let _ = recv_json(&mut stream);
    let _ = wait_exit(&mut child, Duration::from_secs(5));
}

/// Send one `snapshot` request. `mode` is inserted only when the caller passes
/// it, so an omitted-mode case cannot be repaired by this helper.
fn snapshot_request(
    stream: &mut std::os::unix::net::UnixStream,
    host_terminal_id: &str,
    mode: Option<serde_json::Value>,
    max_bytes: u64,
    max_lines: u64,
) -> serde_json::Value {
    let mut request = json!({
        "method": "snapshot",
        "host_terminal_id": host_terminal_id,
        "max_bytes": max_bytes,
        "max_lines": max_lines,
    });
    if let Some(mode) = mode {
        request
            .as_object_mut()
            .expect("snapshot request object")
            .insert("mode".into(), mode);
    }
    send_json(stream, &request);
    recv_json(stream)
}

/// Reserve, spawn, and commit one native terminal running `script` under `sh`.
fn spawn_for_snapshot(
    stream: &mut std::os::unix::net::UnixStream,
    name: &str,
    script: &str,
) -> String {
    send_json(
        stream,
        &json!({
            "method": "reserve_observer",
            "terminal_id": name,
            "reserve_key": format!("rk-{name}"),
        }),
    );
    let reserved = recv_json(stream);
    let reservation_id = reserved["reservation_id"]
        .as_str()
        .expect("reservation id")
        .to_string();
    let prepared = seq_spawn(
        stream,
        1,
        json!({
            "terminal_id": name,
            "spawn_key": format!("sk-{name}"),
            "reservation_id": reservation_id,
            "reserve_key": format!("rk-{name}"),
            "argv": ["/bin/sh", "-c", script],
            "cwd": "/",
            "rows": 24,
            "cols": 80,
            "commit_deadline_ms": 8000,
        }),
    );
    assert_eq!(prepared["ok"], true, "{prepared}");
    let host_terminal_id = prepared["host_terminal_id"]
        .as_str()
        .expect("host terminal id")
        .to_string();
    send_json(
        stream,
        &json!({
            "method": "spawn_commit",
            "terminal_id": name,
            "spawn_key": format!("sk-{name}"),
        }),
    );
    assert_eq!(recv_json(stream)["ok"], true);
    host_terminal_id
}

fn shutdown_after_snapshot(
    stream: &mut std::os::unix::net::UnixStream,
    child: &mut host_support::HostProc,
    host_terminal_id: &str,
) {
    send_json(
        stream,
        &json!({
            "method": "kill",
            "operation_seq": 2,
            "host_terminal_id": host_terminal_id,
            "grace_ms": 20,
        }),
    );
    let _ = recv_json(stream);
    send_json(stream, &json!({"method": "host_shutdown", "grace_ms": 20}));
    let _ = recv_json(stream);
    let _ = wait_exit(child, Duration::from_secs(5));
}

#[test]
fn snapshot_mode_selects_the_returned_representation() {
    let dir = temp_socket_dir();
    let token = "control-token-snapshot-mode";
    write_token(dir.path(), token);
    let (mut child, mut stream) = authed(dir.path(), token);
    let host_terminal_id = spawn_for_snapshot(
        &mut stream,
        "term-mode",
        "printf '\\033[31mRED\\033[0m\\nsecond\\n'; exec sleep 30",
    );

    let mut defaulted = None;
    wait_until("snapshot output with an omitted mode", || {
        let snapshot = snapshot_request(&mut stream, &host_terminal_id, None, 1024 * 1024, 50);
        if snapshot["text"]
            .as_str()
            .is_some_and(|text| text.contains("second"))
        {
            defaulted = Some(snapshot);
            true
        } else {
            false
        }
    });
    let defaulted = defaulted.expect("omitted-mode snapshot");
    assert_eq!(defaulted["ok"], true, "{defaulted}");
    assert_eq!(defaulted["mode"], "text", "{defaulted}");
    let defaulted_text = defaulted["text"].as_str().expect("snapshot text");
    assert!(defaulted_text.contains("RED"), "{defaulted}");
    assert!(!defaulted_text.contains('\x1b'), "{defaulted}");

    let plain = snapshot_request(
        &mut stream,
        &host_terminal_id,
        Some(json!("text")),
        1024 * 1024,
        50,
    );
    assert_eq!(plain["mode"], "text", "{plain}");
    let plain_text = plain["text"].as_str().expect("snapshot text");
    assert!(plain_text.contains("RED"), "{plain}");
    assert!(plain_text.contains("second"), "{plain}");
    assert!(!plain_text.contains('\x1b'), "{plain}");
    assert_eq!(plain["total_bytes"], plain_text.len() as u64, "{plain}");

    let styled = snapshot_request(
        &mut stream,
        &host_terminal_id,
        Some(json!("ansi")),
        1024 * 1024,
        50,
    );
    assert_eq!(styled["mode"], "ansi", "{styled}");
    let styled_text = styled["text"].as_str().expect("snapshot text");
    assert!(styled_text.contains("RED"), "{styled}");
    assert!(styled_text.contains('\x1b'), "{styled}");
    assert!(
        styled["total_bytes"].as_u64().expect("total bytes")
            > plain["total_bytes"].as_u64().expect("total bytes"),
        "ansi carries the escapes the plain representation drops: {styled}"
    );

    for mode in [
        json!("ANSI"),
        json!("plain"),
        json!(""),
        serde_json::Value::Null,
        json!(1),
        json!(["ansi"]),
        json!({"mode": "ansi"}),
    ] {
        let refused = snapshot_request(
            &mut stream,
            &host_terminal_id,
            Some(mode.clone()),
            1024 * 1024,
            50,
        );
        assert_eq!(refused["ok"], false, "{mode} -> {refused}");
        assert_eq!(refused["error"], "invalid_mode", "{mode} -> {refused}");
        assert_eq!(
            refused["valid_modes"],
            json!(["text", "ansi"]),
            "{mode} -> {refused}"
        );
        assert!(refused["text"].is_null(), "{mode} -> {refused}");
    }

    let one_line = snapshot_request(
        &mut stream,
        &host_terminal_id,
        Some(json!("text")),
        1024 * 1024,
        1,
    );
    assert_eq!(one_line["mode"], "text", "{one_line}");
    let one_line_text = one_line["text"].as_str().expect("snapshot text");
    assert_eq!(one_line_text.lines().count(), 1, "{one_line}");
    assert_eq!(one_line["truncated"], true, "{one_line}");
    assert!(
        one_line["dropped_bytes"].as_u64().expect("dropped bytes") > 0,
        "{one_line}"
    );
    assert_eq!(
        one_line["total_bytes"], plain["total_bytes"],
        "line caps do not change the representation's size: {one_line}"
    );

    let few_bytes = snapshot_request(&mut stream, &host_terminal_id, Some(json!("ansi")), 4, 50);
    assert_eq!(few_bytes["mode"], "ansi", "{few_bytes}");
    let few_bytes_text = few_bytes["text"].as_str().expect("snapshot text");
    assert!(few_bytes_text.len() <= 4, "{few_bytes}");
    assert_eq!(few_bytes["truncated"], true, "{few_bytes}");
    assert_eq!(
        few_bytes["total_bytes"], styled["total_bytes"],
        "byte caps do not change the representation's size: {few_bytes}"
    );

    shutdown_after_snapshot(&mut stream, &mut child, &host_terminal_id);
}

#[test]
fn snapshot_text_mode_answers_plainly_when_history_holds_only_styling() {
    let dir = temp_socket_dir();
    let token = "control-token-snapshot-fallback";
    write_token(dir.path(), token);
    let (mut child, mut stream) = authed(dir.path(), token);
    // Styled blank cells: the plain history trims to nothing, so the visible
    // screen answers the text-mode snapshot and must stay escape-free.
    let host_terminal_id = spawn_for_snapshot(
        &mut stream,
        "term-blank",
        "printf '\\033[41m   \\033[0m'; exec sleep 30",
    );

    wait_until("styled blank output", || {
        let styled = snapshot_request(
            &mut stream,
            &host_terminal_id,
            Some(json!("ansi")),
            1024 * 1024,
            50,
        );
        styled["text"]
            .as_str()
            .is_some_and(|text| text.contains('\x1b'))
    });

    let plain = snapshot_request(
        &mut stream,
        &host_terminal_id,
        Some(json!("text")),
        1024 * 1024,
        50,
    );
    assert_eq!(plain["ok"], true, "{plain}");
    assert_eq!(plain["mode"], "text", "{plain}");
    let plain_text = plain["text"].as_str().expect("snapshot text");
    assert!(!plain_text.contains('\x1b'), "{plain}");
    assert!(plain_text.trim().is_empty(), "{plain}");

    shutdown_after_snapshot(&mut stream, &mut child, &host_terminal_id);
}

/// Native terminals admissible at once: `max_attachments_total` (128) less the
/// four slots reserved for host health, `list`, and lifecycle work.
const NATIVE_LIST_CEILING: usize = 124;
/// Distinct tmux panes the host will observe at once (`max_attached_terminals`).
const TMUX_LIST_CEILING: usize = 64;
/// `TITLE_MAX_BYTES`: the widest title the host registry will store.
const LIST_TITLE_BYTES: usize = 1024;
/// The pane OSC layer keeps at most 256 code points of an OSC 0/2 payload, so a
/// native title reaches `TITLE_MAX_BYTES` only through four-byte characters.
const NATIVE_TITLE_CHARS: usize = 256;
/// The widest member of the closed `observation_reason` vocabulary.
const WIDEST_OBSERVATION_REASON: &str = "geometry_exceeds_max_cells";
/// Native terminals bound to a frame observer at once. The registry only learns
/// a title while an observer is bound, and every bound observer costs the host a
/// blit encode per tick, so the population is titled in bounded groups.
const NATIVE_BIND_GROUP: usize = 8;
/// Slow the tmux poll so 64 observers do not fork a `tmux` batch every 50 ms.
const LIST_POLL_INTERVAL_MS: &str = "2000";
/// Populating and titling 188 rows outlasts the shared five-second helper.
const LIST_SETTLE_TIMEOUT: Duration = Duration::from_secs(120);

/// Plan 7.2.3: the `list` envelope at the shipped admission ceilings.
///
/// The population is the whole admissible registry — 124 native terminals plus
/// 64 tmux observers — every row carrying a title at `TITLE_MAX_BYTES` and the
/// widest identity fields its half of the host can carry. Those numbers are the
/// population of this one scenario, never a number of tests.
#[test]
fn list_envelope_fits_under_line_cap() {
    let host = embed_support::spawn_host(&["--tmux-poll-interval-ms", LIST_POLL_INTERVAL_MS]);
    // Four-byte characters for the OSC path, JSON-escaping ones for tmux: each
    // is the widest encoded row its source can produce at `TITLE_MAX_BYTES`.
    let native_title = "\u{1F600}".repeat(NATIVE_TITLE_CHARS);
    let tmux_title = "\"".repeat(LIST_TITLE_BYTES);
    assert_eq!(native_title.len(), LIST_TITLE_BYTES);
    assert_eq!(tmux_title.len(), LIST_TITLE_BYTES);
    let mut control = embed_support::control(&host);

    admit_native_population(&host, &mut control, &native_title);
    let panes = admit_tmux_population(&host, &tmux_title);

    embed_support::wait_until(LIST_SETTLE_TIMEOUT, || {
        let rows = list_rows(&host);
        rows.len() == NATIVE_LIST_CEILING + TMUX_LIST_CEILING
            && rows
                .iter()
                .all(|row| title_bytes(row) == Some(LIST_TITLE_BYTES))
    });

    let line = list_line(&host);
    let envelope: serde_json::Value = serde_json::from_slice(&line).expect("list json");
    let rows = envelope["terminals"].as_array().expect("list rows");
    assert_eq!(
        rows.len(),
        NATIVE_LIST_CEILING + TMUX_LIST_CEILING,
        "list must hold the whole admissible population"
    );
    for row in rows {
        assert_eq!(
            title_bytes(row),
            Some(LIST_TITLE_BYTES),
            "every row carries a title at the registry ceiling: {}",
            row["host_terminal_id"]
        );
    }
    assert!(
        line.len() < MAX_CONTROL_LINE,
        "list envelope is {} bytes, at or above the {MAX_CONTROL_LINE}-byte control line cap",
        line.len()
    );
    // `observation_reason` is a closed vocabulary and is `null` on every live
    // row, so widen the measured line by its longest member on every row.
    let widest_reason =
        rows.len() * (WIDEST_OBSERVATION_REASON.len() + 2).saturating_sub("null".len());
    assert!(
        line.len() + widest_reason < MAX_CONTROL_LINE,
        "list envelope is {} bytes and would be {} with the longest observation_reason on \
         every row, at or above the {MAX_CONTROL_LINE}-byte control line cap",
        line.len(),
        line.len() + widest_reason
    );
    println!(
        "list envelope: {} rows, {} bytes measured, {} bytes with the widest \
         observation_reason on every row, cap {MAX_CONTROL_LINE} bytes",
        rows.len(),
        line.len(),
        line.len() + widest_reason
    );

    // The 189th admission. Both halves are saturated, so each one refuses.
    embed_support::send_json(
        &mut control,
        &json!({
            "method": "reserve_observer",
            "terminal_id": "term-over-ceiling",
            "reserve_key": "rk-over-ceiling",
        }),
    );
    let refused = embed_support::recv_json(&mut control);
    assert_eq!(refused["ok"], false, "{refused}");
    assert_eq!(refused["error"], "capacity", "{refused}");

    let mut extra = panes.pane.locator();
    extra.pane_id = new_tmux_pane(&panes.pane, &tmux_title);
    let mut frames = connect_ansi_frames(&host);
    embed_support::write_msg(
        &mut frames,
        &gobby_terminal::protocol::ClientMessage::AttachTerminal {
            host_terminal_id: String::new(),
            reservation_id: None,
            locator: Some(extra),
        },
    );
    match embed_support::read_msg(&mut frames) {
        gobby_terminal::protocol::ServerMessage::Error { code, .. } => {
            assert_eq!(code, "capacity", "a 65th tmux observer must be refused");
        }
        other => panic!("a 65th tmux observer was admitted: {other:?}"),
    }
}

/// The tmux half of the scenario, kept alive for the whole measurement: closing
/// a tmux observer reaps its registry row, so the streams outlive the assertions.
struct TmuxPopulation {
    pane: embed_support::TmuxPane,
    _drain: FrameDrain,
}

/// Fill the native entitlement ceiling with committed, titled terminals.
///
/// Each group binds its frame observers, waits for the host to publish their
/// titles, then releases them; the rows stay `entitled`, so the ceiling stays
/// full while no observer is left for the host to encode frames for.
fn admit_native_population(
    host: &host_support::HostProc,
    control: &mut std::os::unix::net::UnixStream,
    title: &str,
) {
    let cwd = host.socket_dir().to_string_lossy().into_owned();
    // `cat` ends on PTY EOF, so no child outlives the host that spawned it.
    let script = format!("printf '\\033]0;{title}\\007'; exec cat");
    for group in (0..NATIVE_LIST_CEILING).step_by(NATIVE_BIND_GROUP) {
        let end = (group + NATIVE_BIND_GROUP).min(NATIVE_LIST_CEILING);
        let mut bound = Vec::with_capacity(end - group);
        for index in group..end {
            // Daemon-shaped identities: a terminal UUID and a hex spawn key.
            let terminal_id = format!("7e120000-0000-4000-8000-{index:012}");
            let spawn_key = format!("{index:064}");
            let reserve_key = format!("{index:032}");
            embed_support::send_json(
                control,
                &json!({
                    "method": "reserve_observer",
                    "terminal_id": terminal_id,
                    "reserve_key": reserve_key,
                }),
            );
            let reserved = embed_support::recv_json(control);
            assert_eq!(reserved["ok"], true, "reserve {index}: {reserved}");
            let reservation_id = reserved["reservation_id"]
                .as_str()
                .expect("reservation_id")
                .to_string();

            embed_support::send_json(
                control,
                &json!({
                    "method": "spawn",
                    "operation_seq": index + 1,
                    "terminal_id": terminal_id,
                    "spawn_key": spawn_key,
                    "reservation_id": reservation_id,
                    "reserve_key": reserve_key,
                    "argv": ["/bin/sh", "-c", script],
                    "cwd": cwd,
                    "rows": 40,
                    "cols": 120,
                    "commit_deadline_ms": 120000,
                }),
            );
            let prepared = embed_support::recv_json(control);
            assert_eq!(prepared["ok"], true, "spawn {index}: {prepared}");
            let host_terminal_id = prepared["host_terminal_id"]
                .as_str()
                .expect("host_terminal_id")
                .to_string();

            bound.push(attach_native_frames(
                host,
                &host_terminal_id,
                reservation_id,
                index,
            ));

            embed_support::send_json(
                control,
                &json!({
                    "method": "spawn_commit",
                    "terminal_id": terminal_id,
                    "spawn_key": spawn_key,
                }),
            );
            let committed = embed_support::recv_json(control);
            assert_eq!(committed["ok"], true, "commit {index}: {committed}");
        }
        embed_support::wait_until(LIST_SETTLE_TIMEOUT, || {
            list_rows(host)
                .iter()
                .filter(|row| title_bytes(row) == Some(LIST_TITLE_BYTES))
                .count()
                >= end
        });
        drop(bound);
    }
}

/// Fill the tmux observation ceiling with titled panes on one tmux server.
fn admit_tmux_population(host: &host_support::HostProc, title: &str) -> TmuxPopulation {
    let population = TmuxPopulation {
        pane: embed_support::start_tmux_sized(120, 40),
        _drain: FrameDrain::start(),
    };
    let pane = &population.pane;
    pane.tmux(&["select-pane", "-t", &pane.pane_id, "-T", title]);
    let mut locators = vec![pane.locator()];
    for _ in 1..TMUX_LIST_CEILING {
        let mut locator = pane.locator();
        locator.pane_id = new_tmux_pane(pane, title);
        locators.push(locator);
    }

    for (index, locator) in locators.into_iter().enumerate() {
        let mut stream = connect_ansi_frames(host);
        embed_support::write_msg(
            &mut stream,
            &gobby_terminal::protocol::ClientMessage::AttachTerminal {
                host_terminal_id: String::new(),
                reservation_id: None,
                locator: Some(locator),
            },
        );
        match embed_support::read_msg(&mut stream) {
            gobby_terminal::protocol::ServerMessage::Attached { .. } => {}
            other => panic!("tmux attach {index} failed: {other:?}"),
        }
        population._drain.adopt(stream);
    }
    population
}

/// Bind one native terminal's reservation to a frame observer.
fn attach_native_frames(
    host: &host_support::HostProc,
    host_terminal_id: &str,
    reservation_id: String,
    index: usize,
) -> std::os::unix::net::UnixStream {
    let mut stream = connect_ansi_frames(host);
    embed_support::write_msg(
        &mut stream,
        &gobby_terminal::protocol::ClientMessage::AttachTerminal {
            host_terminal_id: host_terminal_id.to_string(),
            reservation_id: Some(reservation_id),
            locator: None,
        },
    );
    match embed_support::read_msg(&mut stream) {
        gobby_terminal::protocol::ServerMessage::Attached { .. } => stream,
        other => panic!("native attach {index} failed: {other:?}"),
    }
}

/// Open one more titled pane on an existing tmux server.
fn new_tmux_pane(pane: &embed_support::TmuxPane, title: &str) -> String {
    let pane_id = pane.tmux(&[
        "new-window",
        "-d",
        "-P",
        "-F",
        "#{pane_id}",
        "--",
        "/bin/sh",
    ]);
    assert!(!pane_id.is_empty(), "tmux new-window returned no pane id");
    pane.tmux(&["select-pane", "-t", &pane_id, "-T", title]);
    pane_id
}

/// Attach frames as an ANSI-encoding peer, the cheapest encoding for observers
/// this scenario holds open only to keep their registry rows alive.
fn connect_ansi_frames(host: &host_support::HostProc) -> std::os::unix::net::UnixStream {
    use gobby_terminal::protocol::{
        ClientMessage, RenderEncoding, ServerMessage, PROTOCOL_VERSION,
    };

    let mut stream =
        std::os::unix::net::UnixStream::connect(host.socket_dir().join("gterm-frames.sock"))
            .expect("frames socket");
    embed_support::write_msg(
        &mut stream,
        &ClientMessage::Hello {
            version: PROTOCOL_VERSION,
            encoding: RenderEncoding::TerminalAnsi,
            local_token: embed_support::LOCAL.into(),
            cols: 120,
            rows: 40,
            tmux_identity: None,
        },
    );
    match embed_support::read_msg(&mut stream) {
        ServerMessage::Welcome { .. } => stream,
        other => panic!("frames hello refused: {other:?}"),
    }
}

/// A real draining frame peer for the observers this scenario holds open. An
/// observer whose queue never drains is closed `lagged`, which would reap the
/// very rows the envelope is measured over.
struct FrameDrain {
    streams: std::sync::Arc<std::sync::Mutex<Vec<std::os::unix::net::UnixStream>>>,
    stop: std::sync::Arc<std::sync::atomic::AtomicBool>,
    worker: Option<std::thread::JoinHandle<()>>,
}

impl FrameDrain {
    fn start() -> Self {
        let streams: std::sync::Arc<std::sync::Mutex<Vec<std::os::unix::net::UnixStream>>> =
            std::sync::Arc::default();
        let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let worker_streams = std::sync::Arc::clone(&streams);
        let worker_stop = std::sync::Arc::clone(&stop);
        let worker = std::thread::spawn(move || {
            let mut buffer = [0_u8; 64 * 1024];
            while !worker_stop.load(std::sync::atomic::Ordering::Relaxed) {
                {
                    let mut owned = worker_streams.lock().expect("drain streams");
                    for stream in owned.iter_mut() {
                        while matches!(std::io::Read::read(stream, &mut buffer), Ok(n) if n > 0) {}
                    }
                }
                std::thread::sleep(Duration::from_millis(10));
            }
        });
        Self {
            streams,
            stop,
            worker: Some(worker),
        }
    }

    fn adopt(&self, stream: std::os::unix::net::UnixStream) {
        stream.set_nonblocking(true).expect("non-blocking drain");
        self.streams.lock().expect("drain streams").push(stream);
    }
}

impl Drop for FrameDrain {
    fn drop(&mut self) {
        self.stop.store(true, std::sync::atomic::Ordering::Relaxed);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

/// Read one `list` reply as raw bytes so the assertion measures the wire line.
fn list_line(host: &host_support::HostProc) -> Vec<u8> {
    let mut stream = embed_support::control(host);
    embed_support::send_json(&mut stream, &json!({"method": "list"}));
    let mut reader = std::io::BufReader::with_capacity(MAX_CONTROL_LINE, stream);
    let mut line = Vec::new();
    std::io::BufRead::read_until(&mut reader, b'\n', &mut line).expect("list line");
    line
}

/// Decode the `terminals` array of one `list` reply.
fn list_rows(host: &host_support::HostProc) -> Vec<serde_json::Value> {
    let line = list_line(host);
    let envelope: serde_json::Value = serde_json::from_slice(&line).expect("list json");
    envelope["terminals"]
        .as_array()
        .cloned()
        .unwrap_or_default()
}

/// The stored byte length of one row's title.
fn title_bytes(row: &serde_json::Value) -> Option<usize> {
    row["title"].as_str().map(str::len)
}
