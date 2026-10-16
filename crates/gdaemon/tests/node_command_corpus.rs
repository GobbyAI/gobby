use std::fs;
use std::path::PathBuf;

use gobby_daemon::node_commands::{CONTRACT_VERSION, round_trip_json};

/// Requests and node bodies carry `v`; answers and the stale refusal do not.
const VERSIONED_KINDS: &[&str] = &[
    "command",
    "reply",
    "checkout",
    "report",
    "prompt_delivery_failure",
    "code_index_warning",
    "terminate",
];

fn corpus_messages() -> Vec<serde_json::Value> {
    let path =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/node_commands/corpus.json");
    let text = fs::read_to_string(path).expect("corpus");
    let document: serde_json::Value = serde_json::from_str(&text).expect("corpus json");
    document["messages"].as_array().expect("messages").clone()
}

fn with_unknown_field(raw: &str) -> String {
    assert!(raw.ends_with('}'), "corpus message is not a JSON object");
    let mut owned = raw.to_string();
    owned.pop();
    owned.push_str(",\"unexpected\":1}");
    owned
}

#[test]
fn corpus_round_trips() {
    let messages = corpus_messages();
    assert!(!messages.is_empty());
    for message in messages {
        let name = message["name"].as_str().expect("name");
        let kind = message["kind"].as_str().expect("kind");
        let raw = message["json"].as_str().expect("json");
        let value: serde_json::Value = serde_json::from_str(raw).expect("message json");
        if VERSIONED_KINDS.contains(&kind) {
            assert_eq!(value["v"].as_u64(), Some(CONTRACT_VERSION), "{name}");
        } else {
            assert!(value.get("v").is_none(), "{name} carries v");
        }
        let encoded = round_trip_json(kind, raw).unwrap_or_else(|error| {
            panic!("{name} failed to decode: {error}");
        });
        assert_eq!(encoded, raw, "{name}");
        let rejected = round_trip_json(kind, &with_unknown_field(raw));
        assert!(rejected.is_err(), "{name} accepted an unknown field");
    }
}
