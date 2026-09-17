//! 3.3.11 / 3.3.18 / 3.3.19 / 3.3.22 terminal-WS goldens.

use gobby_client::daemon::{
    decode_message, encode_message, GOLDEN_NAMES, TERMINAL_WS_SAFE_INTEGER_MAX,
};
use gobby_client::Workspace;
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::fs;
use std::path::PathBuf;

fn golden_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/terminal_ws_golden")
}

#[test]
fn corpus_replays_from_canonical_manifest() {
    let dir = golden_dir();
    let manifest: Value = serde_json::from_slice(
        &fs::read(dir.join("manifest.json")).expect("missing terminal WS manifest"),
    )
    .expect("terminal WS manifest must be JSON");
    let object = manifest
        .as_object()
        .expect("terminal WS manifest must be an object");
    assert_eq!(object.keys().collect::<Vec<_>>(), ["fixtures"]);
    let names = object["fixtures"]
        .as_array()
        .expect("fixtures must be an array")
        .iter()
        .map(|name| name.as_str().expect("fixture name must be a string"))
        .collect::<Vec<_>>();
    assert_eq!(names.len(), 43);
    assert_eq!(names, GOLDEN_NAMES);
    assert!(!names.contains(&"manifest.json"));

    let listed = names.iter().copied().collect::<BTreeSet<_>>();
    let on_disk = fs::read_dir(&dir)
        .expect("terminal WS fixture directory")
        .map(|entry| {
            entry
                .expect("terminal WS fixture entry")
                .file_name()
                .into_string()
                .expect("fixture name must be UTF-8")
        })
        .filter(|name| name != "manifest.json")
        .collect::<BTreeSet<_>>();
    assert_eq!(listed, on_disk.iter().map(String::as_str).collect());

    for name in names {
        let raw = fs::read(dir.join(name)).unwrap_or_else(|_| panic!("missing {name}"));
        let decoded = decode_message(&raw).unwrap_or_else(|e| panic!("{name}: {e}"));
        let encoded = encode_message(&decoded).unwrap_or_else(|e| panic!("{name} encode: {e}"));
        assert_eq!(encoded, raw, "{name} must round-trip byte-for-byte");
    }
}

#[test]
fn direct_attach_and_reconnect_reregisters() {
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
    let high = TERMINAL_WS_SAFE_INTEGER_MAX - 1;
    let max = TERMINAL_WS_SAFE_INTEGER_MAX;
    let overflow = TERMINAL_WS_SAFE_INTEGER_MAX + 1;
    for field in ["message_seq", "lease_generation", "client_write_seq", "seq"] {
        let a = encode_message(&counter_payload(field, json!(high))).expect("high safe integer");
        let b = encode_message(&counter_payload(field, json!(max))).expect("max safe integer");
        assert_ne!(a, b, "{field} must preserve adjacent safe integers");
        assert_eq!(decode_message(&a).expect("decode high")[field], high);
        assert_eq!(decode_message(&b).expect("decode max")[field], max);

        for invalid in [json!(overflow), json!("1"), json!(1.5)] {
            let payload = counter_payload(field, invalid);
            assert!(encode_message(&payload).is_err(), "encode accepted {field}");
            let raw = serde_json::to_vec(&payload).expect("serialize invalid counter");
            assert!(decode_message(&raw).is_err(), "decode accepted {field}");
        }
    }
}

fn counter_payload(field: &str, value: Value) -> Value {
    let mut payload = serde_json::Map::new();
    payload.insert("type".to_owned(), json!("x"));
    payload.insert(field.to_owned(), value);
    Value::Object(payload)
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
    let before = ws.pane(pane).frames_rendered();
    ws.apply_ws(&json!({
        "type": "terminal_attachment_finalized",
        "terminal_id": "term-a",
        "attachment_id": attachment,
        "reason": "detach",
        "lease_generation": gen
    }))
    .unwrap();
    assert!(!ws.pane(pane).is_live());
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
