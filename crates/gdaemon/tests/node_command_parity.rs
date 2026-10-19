use std::fs;
use std::path::PathBuf;

use gobby_daemon::node_commands::{CONTRACT_VERSION, round_trip_json};

/// The version 3 commands and one reply per outcome.
const V3_MESSAGES: &[&str] = &[
    "command.workspace.merge",
    "command.workspace.remove",
    "command.checkout.status",
    "reply.workspace.merge.merged",
    "reply.workspace.merge.already_merged",
    "reply.workspace.merge.conflict",
    "reply.workspace.merge.target_dirty",
    "reply.workspace.merge.busy",
    "reply.workspace.remove.removed",
    "reply.workspace.remove.dirty",
    "reply.workspace.remove.unmerged",
    "reply.workspace.remove.busy",
    "reply.checkout.status",
    "reply.checkout.status.nulls",
];

/// The version 4 `checkout.read` command with both query forms, both replies and the three error replies.
const V4_MESSAGES: &[&str] = &[
    "command.checkout.read.files",
    "command.checkout.read.git",
    "reply.checkout.read.files",
    "reply.checkout.read.git",
    "reply.checkout.read.error.invalid_payload",
    "reply.checkout.read.error.no_project_root",
    "reply.checkout.read.error.outside",
];

fn corpus_list(key: &str) -> Vec<serde_json::Value> {
    let path =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/node_commands/corpus.json");
    let text = fs::read_to_string(path).expect("corpus");
    let document: serde_json::Value = serde_json::from_str(&text).expect("corpus json");
    document[key].as_array().expect(key).clone()
}

fn field<'a>(message: &'a serde_json::Value, key: &str) -> &'a str {
    message[key].as_str().expect(key)
}

fn assert_round_trips(names: &[&str]) -> usize {
    let messages = corpus_list("messages");
    let mut checked = 0;
    for &name in names {
        let message = messages
            .iter()
            .find(|message| field(message, "name") == name)
            .unwrap_or_else(|| panic!("corpus has no message {name}"));
        let raw = field(message, "json");
        let value: serde_json::Value = serde_json::from_str(raw).expect("message json");
        assert_eq!(value["v"].as_u64(), Some(CONTRACT_VERSION), "{name}");
        let encoded = round_trip_json(field(message, "kind"), raw).unwrap_or_else(|error| {
            panic!("{name} failed to decode: {error}");
        });
        assert_eq!(encoded, raw, "{name}");
        checked += 1;
    }
    checked
}

#[test]
fn v3_commands_round_trip() {
    assert_eq!(assert_round_trips(V3_MESSAGES), V3_MESSAGES.len());
}

#[test]
fn v4_checkout_read_round_trips() {
    assert_eq!(assert_round_trips(V4_MESSAGES), V4_MESSAGES.len());
}

/// The decode error for one invalid entry, or `None` when it names `expect`.
fn invalid_mismatch(message: &serde_json::Value, expect: &str) -> Option<String> {
    let name = field(message, "name");
    match round_trip_json(field(message, "kind"), field(message, "json")) {
        Ok(decoded) => Some(format!("{name} decoded as {decoded}")),
        Err(error) if error.to_string().contains(expect) => None,
        Err(error) => Some(format!("{name} error `{error}` lacks `{expect}`")),
    }
}

fn named<'a>(entries: &'a [serde_json::Value], name: &str) -> &'a serde_json::Value {
    entries
        .iter()
        .find(|entry| field(entry, "name") == name)
        .expect(name)
}

#[test]
fn invalid_messages_fail() {
    let invalid = corpus_list("invalid");
    assert_eq!(invalid.len(), 35);
    let mismatches: Vec<String> = invalid
        .iter()
        .filter_map(|message| invalid_mismatch(message, field(&message["expect"], "rust")))
        .collect();
    assert!(mismatches.is_empty(), "{}", mismatches.join("\n"));
}

#[test]
fn invalid_expectation_detects_changed_cause() {
    let invalid = corpus_list("invalid");
    let omitted = named(&invalid, "invalid.omitted_nullable");
    let unknown = named(&invalid, "invalid.unknown_field");
    let expect = field(&omitted["expect"], "rust");
    assert!(invalid_mismatch(omitted, expect).is_none());
    let mut changed = omitted.clone();
    changed["kind"] = unknown["kind"].clone();
    changed["json"] = unknown["json"].clone();
    assert!(invalid_mismatch(&changed, expect).is_some());
}
