use std::fs;
use std::path::PathBuf;

use gobby_daemon::node_commands::round_trip_json;

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
        let encoded = round_trip_json(kind, raw).unwrap_or_else(|error| {
            panic!("{name} failed to decode: {error}");
        });
        assert_eq!(encoded, raw, "{name}");
        let rejected = round_trip_json(kind, &with_unknown_field(raw));
        assert!(rejected.is_err(), "{name} accepted an unknown field");
    }
}
