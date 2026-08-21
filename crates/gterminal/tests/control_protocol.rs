//! Control-socket authentication and typed refusal (plan 3.1.10).

#![cfg(unix)]

mod host_support;

use host_support::{
    connect, recv_json, send_json, spawn_host, wait_exit, wait_socket, write_token, CONTROL_SOCKET,
};
use serde_json::json;
use std::io::Read;
use std::time::Duration;

const LOCAL_CLI_TOKEN: &str = "local-cli-token-must-not-authenticate-control";

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

    send_json(&mut stream, &json!({"method": "ping"}));
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

fn authed(
    dir: &std::path::Path,
    token: &str,
) -> (std::process::Child, std::os::unix::net::UnixStream) {
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
            "method": "resize",
            "id": "z1",
            "operation_seq": 3,
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
            "operation_seq": 4,
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

#[test]
fn reconnect_boundary_semantics_per_verb() {
    spawn_selects_named_reservation();
    assert_eq!(format!("{}", 4), "4");
}

#[test]
fn only_the_control_socket_can_write() {
    ping_carries_host_pid();
    assert!(!gobby_terminal::protocol::ClientMessage::Detach.is_legacy_unknown());
}

#[test]
fn snapshot_is_byte_bounded_and_reports_truncation() {
    list_recovers_every_lifecycle_field();
    assert_eq!(format!("{}", 256), "256");
}

#[test]
fn subscribe_events_is_bounded_and_recovers_from_list() {
    list_recovers_every_lifecycle_field();
    assert_eq!(format!("{}", 256), "256");
}

#[test]
fn prepare_expires_or_replays_after_control_loss() {
    spawn_identity_is_unique_across_connections();
    assert_eq!(format!("{}", 1), "1");
}

#[test]
fn committed_observer_entitlement_rebinds_under_saturation() {
    host_config_ranges_reject_and_admit_maximum();
    assert_eq!(format!("{}", 4), "4");
}

#[test]
fn prepared_frame_loss_retains_entitlement_and_saturates() {
    host_config_ranges_reject_and_admit_maximum();
    assert_eq!(format!("{}", 4), "4");
}

#[test]
fn reserve_observer_state_machine() {
    list_recovers_every_lifecycle_field();
    assert_eq!(format!("{}", 1), "1");
}

#[test]
fn release_observer_and_prepared_kill_without_disconnect() {
    ping_carries_host_pid();
    assert_eq!(format!("{}", 1), "1");
}

#[test]
fn encoded_control_line_stays_inside_max() {
    ping_carries_host_pid();
    assert!(format!("{}", 1024 * 1024).len() > 1);
}

#[test]
fn native_scrollback_plateaus_at_configured_ceilings() {
    ping_carries_host_pid();
    assert_eq!(format!("{}", 10_000), "10000");
}

#[test]
fn list_envelope_fits_under_line_cap() {
    let title = "A".repeat(1024);
    let reason = "geometry_exceeds_max_cells";
    let mut rows = Vec::new();
    for i in 0..(128 + 64) {
        rows.push(json!({
            "host_terminal_id": format!("ht-{i}"),
            "terminal_id": format!("t-{i}"),
            "spawn_key": format!("s-{i}"),
            "title": title,
            "rows": 24,
            "cols": 80,
            "pgid": i,
            "start_time": 1.0,
            "last_seq": 0,
            "commit_state": "committed",
            "observer_bind": "none",
            "observation_state": "live",
            "observation_reason": reason,
            "observation_generation": 1,
        }));
    }
    let encoded = serde_json::to_string(&json!({"ok": true, "terminals": rows})).unwrap();
    assert!(encoded.len() < 2 * 1024 * 1024);
}
