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

#[test]
fn v3_commands_round_trip() {
    let messages = corpus_list("messages");
    for &name in V3_MESSAGES {
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
    }
}

#[test]
fn invalid_messages_fail() {
    let invalid = corpus_list("invalid");
    assert_eq!(invalid.len(), 5);
    for message in &invalid {
        let name = field(message, "name");
        let decoded = round_trip_json(field(message, "kind"), field(message, "json"));
        assert!(decoded.is_err(), "{name} decoded as {decoded:?}");
    }
}
