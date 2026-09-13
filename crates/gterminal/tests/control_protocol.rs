//! Control-socket authentication and typed refusal (plan 3.1.10).

#![cfg(unix)]

mod embed_support;
mod host_support;

use host_support::{
    connect, recv_json, send_json, send_json_without_id, spawn_host, wait_exit, wait_socket,
    wait_until, write_token, CONTROL_SOCKET,
};
use serde_json::json;
use std::collections::HashMap;
use std::io::{Read, Write};
use std::time::Duration;

const LOCAL_CLI_TOKEN: &str = "local-cli-token-must-not-authenticate-control";
const MAX_CONTROL_LINE: usize = 2 * 1024 * 1024;

#[test]
fn hello_required_before_any_verb() {
    let dir = tempfile::tempdir().expect("tempdir");
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

    send_json_without_id(&mut stream, &json!({"method": "ping"}));
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
    let dir = tempfile::tempdir().expect("tempdir");
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

#[test]
fn requests_require_unique_ids() {
    let dir = tempfile::tempdir().expect("tempdir");
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
    let dir = tempfile::tempdir().expect("tempdir");
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
    let dir = tempfile::tempdir().expect("tempdir");
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
            "env": {"GTERM_GATE_FAULT": "delay", "PATH": "/bin:/usr/bin"},
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

    let delayed_operations: Vec<_> = (0..5)
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
    let mut commit_seen = false;
    while completed.len() < 64 {
        completed.push(recv_json(&mut stream));
    }
    for response in completed {
        assert_eq!(response["ok"], true, "{response}");
        if response["id"] == "waiting-commit" {
            commit_seen = true;
        } else {
            ordered_ids.push(response["id"].as_str().unwrap().to_string());
        }
    }
    let mut expected_ids = vec!["ordered-batch".to_string()];
    expected_ids.extend((4..=65).map(|seq| format!("queued-{seq}")));
    assert_eq!(ordered_ids, expected_ids);
    assert!(commit_seen);

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
    let killed = recv_json(&mut stream);
    assert_eq!(killed["id"], "parallel-kill");
    assert_eq!(killed["ok"], true);

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
    let dir = tempfile::tempdir().expect("tempdir");
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
    let killed = recv_json(&mut stream);
    assert_eq!(killed["ok"], true, "{killed}");

    send_json(
        &mut stream,
        &json!({"method": "host_shutdown", "id": "sd", "grace_ms": 50}),
    );
    let shutdown = recv_json(&mut stream);
    assert_eq!(shutdown["ok"], true);
    assert!(wait_exit(&mut child, Duration::from_secs(5)).is_some());
}

#[test]
fn snapshot_truncates_on_char_boundaries() {
    let dir = tempfile::tempdir().expect("tempdir");
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
    let dir = tempfile::tempdir().expect("tempdir");
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
    let dir = tempfile::tempdir().expect("tempdir");
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
    let dir = tempfile::tempdir().expect("tempdir");
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
    let dir = tempfile::tempdir().expect("tempdir");
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
    let dir = tempfile::tempdir().expect("tempdir");
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
    let dir = tempfile::tempdir().expect("tempdir");
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
    let dir = tempfile::tempdir().expect("tempdir");
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
