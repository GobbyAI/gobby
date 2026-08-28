//! 3.3.11 / 3.3.18 / 3.3.19 / 3.3.22 terminal-WS goldens.

use gobby_client::daemon::{
    decode_message, encode_message, GOLDEN_NAMES, TERMINAL_WS_SAFE_INTEGER_MAX,
};
use gobby_client::Workspace;
use serde_json::{json, Value};
use std::fs;
use std::path::PathBuf;

fn golden_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/servers/fixtures/terminal_ws_golden")
}

#[test]
fn direct_attach_and_reconnect_reregisters() {
    let dir = golden_dir();
    for name in GOLDEN_NAMES {
        let raw = fs::read(dir.join(name)).unwrap_or_else(|_| panic!("missing {name}"));
        let decoded = decode_message(&raw).unwrap_or_else(|e| panic!("{name}: {e}"));
        let encoded = encode_message(&decoded).unwrap_or_else(|e| panic!("{name} encode: {e}"));
        assert_eq!(encoded, raw, "{name} must round-trip byte-for-byte");
    }

    let mut ws = Workspace::scripted();
    let pane = ws.open_terminal("term-a", "native", "epoch-a").unwrap();
    ws.attach_frames(pane).unwrap();
    let attach = ws
        .daemon()
        .ws_sent()
        .into_iter()
        .find(|m| m["type"] == "terminal_attach")
        .expect("attach");
    assert_eq!(attach["frame_delivery"], "direct");
    let old = ws.pane(pane).attachment_id().to_string();
    ws.focus_pane(pane).unwrap();
    ws.drop_daemon_ws();
    ws.reconnect_daemon_ws().expect("reconnect");
    assert_ne!(ws.pane(pane).attachment_id(), old);
    assert!(ws.pane(pane).is_observe());
    ws.take_control(pane)
        .expect("take control after reregister");
    assert!(ws.pane(pane).is_held());
}

#[test]
fn seq_and_lease_generation_are_safe_integers() {
    let overflow = TERMINAL_WS_SAFE_INTEGER_MAX + 1;
    let err = encode_message(&json!({
        "type": "terminal_control_result",
        "attachment_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "granted": false,
        "reason": "held",
        "lease_generation": overflow
    }))
    .unwrap_err();
    assert_eq!(err.to_string(), "safe_integer_overflow");

    let high = TERMINAL_WS_SAFE_INTEGER_MAX - 1;
    let max = TERMINAL_WS_SAFE_INTEGER_MAX;
    let a = encode_message(&json!({"type": "x", "message_seq": high})).unwrap();
    let b = encode_message(&json!({"type": "x", "message_seq": max})).unwrap();
    assert_ne!(a, b);
    decode_message(&a).unwrap();
    decode_message(&b).unwrap();

    let as_str = encode_message(&json!({"type": "x", "message_seq": "1"}));
    assert!(as_str.is_err());
    let as_float = encode_message(&json!({"type": "x", "client_write_seq": 1.5}));
    assert!(as_float.is_err());
}

#[test]
fn finalized_mid_fragment_drops_stale_slice() {
    let mut ws = Workspace::scripted();
    let pane = ws.open_terminal("term-a", "native", "epoch-a").unwrap();
    ws.attach_frames(pane).unwrap();
    let attachment = ws.pane(pane).attachment_id().to_string();
    let gen = ws.pane(pane).lease_generation();
    ws.apply_ws(&json!({
        "type": "terminal_ws_fragment",
        "event": "terminal_output",
        "terminal_id": "term-a",
        "attachment_id": attachment,
        "message_seq": 9,
        "fragment_index": 0,
        "more": true,
        "encoding": "utf8-b64",
        "payload": "eyJ0eXBlIjoidGVybWluYWxfb3V0cHV0In0="
    }))
    .unwrap();
    assert!(ws.pane(pane).has_fragment_accounting());
    ws.apply_ws(&json!({
        "type": "terminal_attachment_finalized",
        "terminal_id": "term-a",
        "attachment_id": attachment,
        "reason": "detach",
        "lease_generation": gen
    }))
    .unwrap();
    assert!(!ws.pane(pane).is_live());
    assert!(!ws.pane(pane).has_fragment_accounting());
    let before = ws.pane(pane).frames_rendered();
    ws.apply_ws(&json!({
        "type": "terminal_ws_fragment",
        "event": "terminal_output",
        "terminal_id": "term-a",
        "attachment_id": attachment,
        "message_seq": 9,
        "fragment_index": 1,
        "more": false,
        "encoding": "utf8-b64",
        "payload": "e30="
    }))
    .unwrap();
    assert_eq!(
        ws.pane(pane).frames_rendered(),
        before,
        "stale fragment after finalize must create no state"
    );
}

#[test]
fn write_outcome_enters_uncertain_readonly() {
    let dir = golden_dir();
    for name in [
        "write_outcome.json",
        "write_outcome_indeterminate.json",
        "write_outcome_refused.json",
        "write_outcome_conflict.json",
        "write_outcome_expired.json",
        "write_outcome_capacity.json",
        "input.json",
        "paste.json",
    ] {
        let raw = fs::read(dir.join(name)).unwrap();
        decode_message(&raw).unwrap();
    }

    let mut ws = Workspace::scripted();
    let pane = ws.open_terminal("term-a", "native", "epoch-a").unwrap();
    ws.attach_frames(pane).unwrap();
    ws.focus_pane(pane).unwrap();
    ws.send_keys(pane, "ls\n").unwrap();
    let seq = ws.pane(pane).in_flight_write().expect("in flight");
    ws.apply_ws(&json!({
        "type": "terminal_write_outcome",
        "terminal_id": "term-a",
        "attachment_id": ws.pane(pane).attachment_id(),
        "client_write_seq": seq,
        "outcome": "delivered",
        "reason": Value::Null
    }))
    .unwrap();
    assert!(ws.pane(pane).is_held());

    ws.send_keys(pane, "pwd\n").unwrap();
    let seq = ws.pane(pane).in_flight_write().unwrap();
    let writes_before = ws.daemon().pty_mutation_count();
    ws.apply_ws(&json!({
        "type": "terminal_write_outcome",
        "terminal_id": "term-a",
        "attachment_id": ws.pane(pane).attachment_id(),
        "client_write_seq": seq,
        "outcome": "indeterminate",
        "reason": "indeterminate_backend"
    }))
    .unwrap();
    assert!(ws.pane(pane).is_uncertain_readonly());
    assert!(ws.pane(pane).in_flight_write().is_none());
    ws.send_keys(pane, "echo no\n").unwrap_err();
    assert_eq!(ws.daemon().pty_mutation_count(), writes_before);

    ws.force_held(pane);
    ws.send_keys(pane, "x").unwrap();
    let seq = ws.pane(pane).in_flight_write().unwrap();
    ws.apply_ws(&json!({
        "type": "terminal_write_outcome",
        "terminal_id": "term-a",
        "attachment_id": ws.pane(pane).attachment_id(),
        "client_write_seq": seq,
        "outcome": "refused",
        "reason": "held"
    }))
    .unwrap();
    assert!(ws.pane(pane).is_observe());

    for reason in [
        "write_seq_conflict",
        "write_seq_expired",
        "write_seq_capacity",
    ] {
        ws.force_held(pane);
        ws.send_keys(pane, "y").unwrap();
        let seq = ws.pane(pane).in_flight_write().unwrap();
        let mutations = ws.daemon().pty_mutation_count();
        ws.apply_ws(&json!({
            "type": "terminal_write_outcome",
            "terminal_id": "term-a",
            "attachment_id": ws.pane(pane).attachment_id(),
            "client_write_seq": seq,
            "outcome": "refused",
            "reason": reason
        }))
        .unwrap();
        assert!(ws.pane(pane).in_flight_write().is_none());
        assert_eq!(
            ws.daemon().pty_mutation_count(),
            mutations,
            "{reason} must not resend"
        );
    }
}
