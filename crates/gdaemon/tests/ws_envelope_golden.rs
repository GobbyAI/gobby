//! Native replay of the shared WS envelope corpus (A1) plus native-only codec
//! cases.
//!
//! Python serializes every broadcast and direct frame; the native transport
//! forwards those bytes unchanged and never reserializes them. The corpus
//! records the bytes each recipient receives, so this harness takes frame text
//! from `deliveries`, ties it back to `message`/`frames` by parsed value, and
//! checks that exactly the clients the native filter selects receive it.

use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

use gobby_daemon::websocket::subscriptions::{EntryMode, Inbound, SubscriptionState};
use serde::Deserialize;
use serde_json::{Map, Value, json};

const EVENTS_ERROR: &str =
    r#"{"type": "error", "code": "ERROR", "message": "events must be a list of strings"}"#;
const TYPE_ERROR: &str =
    r#"{"type": "error", "code": "ERROR", "message": "Message type must be a string"}"#;
const INVALID_JSON_ERROR: &str =
    r#"{"type": "error", "code": "ERROR", "message": "Invalid JSON format"}"#;
const NOT_OBJECT_ERROR: &str =
    r#"{"type": "error", "code": "ERROR", "message": "Message must be a JSON object"}"#;

#[derive(Deserialize)]
struct Manifest {
    schema_version: u64,
    cases_file: String,
    cases: Vec<String>,
}

#[derive(Deserialize)]
struct Corpus {
    schema_version: u64,
    cases: Vec<Case>,
}

#[derive(Deserialize)]
struct Case {
    name: String,
    clients: BTreeMap<String, ClientSpec>,
    steps: Vec<Step>,
}

#[derive(Deserialize)]
struct ClientSpec {
    entry: String,
    #[serde(default)]
    explicit_null: bool,
    initial_state: Value,
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "lowercase")]
enum Step {
    Send {
        client: String,
        text: String,
        replies: Vec<Value>,
        subscriptions: Value,
        #[serde(default)]
        handler_exception: Option<String>,
    },
    Broadcast {
        message: Map<String, Value>,
        #[serde(default)]
        frames: Vec<Value>,
        deliveries: BTreeMap<String, Vec<String>>,
    },
    Direct {
        client: String,
        message: Map<String, Value>,
        deliveries: BTreeMap<String, Vec<String>>,
    },
}

fn corpus_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/ws_envelope_golden")
}

fn load_corpus() -> Vec<Case> {
    let dir = corpus_dir();
    let manifest: Manifest =
        serde_json::from_slice(&std::fs::read(dir.join("manifest.json")).expect("read manifest"))
            .expect("parse manifest");
    assert_eq!(manifest.schema_version, 1, "unsupported manifest schema");
    let corpus: Corpus =
        serde_json::from_slice(&std::fs::read(dir.join(&manifest.cases_file)).expect("read cases"))
            .expect("parse cases");
    assert_eq!(corpus.schema_version, 1, "unsupported cases schema");
    let names: Vec<&str> = corpus.cases.iter().map(|case| case.name.as_str()).collect();
    assert_eq!(
        names, manifest.cases,
        "manifest inventory differs from cases.json"
    );
    corpus.cases
}

/// Corpus state encoding: `"absent"`, `null`, or the sorted initialized set.
fn encode_state(state: &SubscriptionState) -> Value {
    match state {
        SubscriptionState::Absent => json!("absent"),
        SubscriptionState::ExplicitNull => Value::Null,
        SubscriptionState::Initialized(subs) => json!(subs),
    }
}

fn initial_state(spec: &ClientSpec) -> SubscriptionState {
    if spec.explicit_null {
        return SubscriptionState::ExplicitNull;
    }
    match spec.entry.as_str() {
        "direct" => SubscriptionState::new(EntryMode::Direct),
        "browser" => SubscriptionState::new(EntryMode::Browser),
        other => panic!("unknown entry mode {other}"),
    }
}

/// The manifest's single normalization: an acknowledgement whose keys are
/// exactly `type` and `events`, with no duplicate events, matching the
/// expected list after sorting.
fn assert_ack(frame: &str, expected: &Value, context: &str) {
    let parsed: Map<String, Value> = serde_json::from_str(frame).expect("ack frame is JSON");
    let keys: BTreeSet<&str> = parsed.keys().map(String::as_str).collect();
    assert_eq!(
        keys,
        BTreeSet::from(["type", "events"]),
        "{context}: ack keys"
    );
    assert_eq!(parsed["type"], expected["type"], "{context}: ack type");
    let mut events: Vec<&str> = parsed["events"]
        .as_array()
        .expect("ack events array")
        .iter()
        .map(|event| event.as_str().expect("ack event string"))
        .collect();
    let unique: BTreeSet<&str> = events.iter().copied().collect();
    assert_eq!(
        unique.len(),
        events.len(),
        "{context}: duplicate ack events"
    );
    events.sort_unstable();
    let mut wanted: Vec<&str> = expected["events"]
        .as_array()
        .expect("expected ack events")
        .iter()
        .map(|event| event.as_str().expect("expected ack event string"))
        .collect();
    wanted.sort_unstable();
    assert_eq!(events, wanted, "{context}: ack events");
}

fn reply_frames(outcome: Inbound<'_>, context: &str) -> Vec<String> {
    match outcome {
        Inbound::Ack(frame) => vec![frame],
        Inbound::Rejected(frame) => vec![frame.to_owned()],
        Inbound::Application(_) => panic!("{context}: control was treated as application frame"),
    }
}

fn parsed(text: &str) -> Value {
    serde_json::from_str(text).expect("delivered frame is JSON")
}

#[test]
fn rust_replays_envelope_corpus() {
    let cases = load_corpus();
    assert!(!cases.is_empty(), "corpus lists no cases");
    for case in cases {
        let mut states: BTreeMap<&str, SubscriptionState> = BTreeMap::new();
        for (client, spec) in &case.clients {
            let state = initial_state(spec);
            assert_eq!(
                encode_state(&state),
                spec.initial_state,
                "{}: initial state of {client}",
                case.name
            );
            states.insert(client.as_str(), state);
        }
        let clients: BTreeSet<&str> = states.keys().copied().collect();

        for (index, step) in case.steps.iter().enumerate() {
            let context = format!("{} step {index}", case.name);
            match step {
                Step::Send {
                    client,
                    text,
                    replies,
                    subscriptions,
                    handler_exception,
                } => {
                    let state = states.get_mut(client.as_str()).expect("send client");
                    let actual = reply_frames(state.apply_subscription_request(text), &context);
                    assert_eq!(actual.len(), replies.len(), "{context}: reply count");
                    for (frame, expected) in actual.iter().zip(replies) {
                        match expected {
                            Value::String(bytes) => {
                                assert_eq!(frame, bytes, "{context}: reply bytes");
                            }
                            Value::Object(ack) => assert_ack(frame, &ack["ack"], &context),
                            other => panic!("{context}: unsupported reply {other}"),
                        }
                    }
                    assert_eq!(&encode_state(state), subscriptions, "{context}: state");
                    // The Python oracle reached its generic handler-error path.
                    if handler_exception.is_some() {
                        assert!(actual[0].contains("Internal server error"), "{context}");
                    }
                }
                Step::Broadcast {
                    message,
                    frames,
                    deliveries,
                } => {
                    let named: BTreeSet<&str> = deliveries.keys().map(String::as_str).collect();
                    assert_eq!(named, clients, "{context}: deliveries name every client");
                    let message = Value::Object(message.clone());
                    let selected: BTreeSet<&str> = states
                        .iter()
                        .filter(|(_, state)| state.matches_broadcast(&message))
                        .map(|(client, _)| *client)
                        .collect();
                    let expected: BTreeSet<&str> = deliveries
                        .iter()
                        .filter(|(_, delivered)| !delivered.is_empty())
                        .map(|(client, _)| client.as_str())
                        .collect();
                    assert_eq!(selected, expected, "{context}: recipients");
                    // Every recipient gets the same frames, in order, which are
                    // the fragments when given and the message otherwise.
                    let wire: Vec<Value> = if frames.is_empty() {
                        vec![message.clone()]
                    } else {
                        frames.clone()
                    };
                    for client in &expected {
                        let delivered = &deliveries[*client];
                        let values: Vec<Value> = delivered.iter().map(|t| parsed(t)).collect();
                        assert_eq!(values, wire, "{context}: frames for {client}");
                    }
                }
                Step::Direct {
                    client,
                    message,
                    deliveries,
                } => {
                    let named: BTreeSet<&str> = deliveries.keys().map(String::as_str).collect();
                    assert_eq!(named, clients, "{context}: deliveries name every client");
                    for (recipient, delivered) in deliveries {
                        if recipient == client {
                            let values: Vec<Value> = delivered.iter().map(|t| parsed(t)).collect();
                            assert_eq!(values, vec![Value::Object(message.clone())], "{context}");
                        } else {
                            assert!(delivered.is_empty(), "{context}: {recipient} got a reply");
                        }
                    }
                }
            }
        }
    }
}

/// One fixture per state the codec can hold when a malformed control arrives.
fn every_state() -> Vec<SubscriptionState> {
    let mut subscribed = SubscriptionState::new(EntryMode::Browser);
    let ack = subscribed.apply_subscription_request(r#"{"type":"subscribe","events":["a","b"]}"#);
    assert!(matches!(ack, Inbound::Ack(_)), "fixture subscribe");
    vec![
        SubscriptionState::new(EntryMode::Direct),
        SubscriptionState::ExplicitNull,
        SubscriptionState::new(EntryMode::Browser),
        subscribed,
    ]
}

#[test]
fn malformed_control_does_not_mutate_state() {
    let mut cases: Vec<(String, &'static str)> = Vec::new();
    // Native-only tightening: Python leaves these outside its supported domain.
    let bad_members = [
        "[null]",
        "[true]",
        "[false]",
        "[0]",
        "[1.5]",
        "[[]]",
        r#"[["a"]]"#,
        "[{}]",
        r#"["a", null]"#,
        r#"[7, "a"]"#,
        r#"["a", {"b": 1}, "c"]"#,
    ];
    for control in ["subscribe", "unsubscribe"] {
        for members in bad_members {
            let text = format!(r#"{{"type":"{control}","events":{members}}}"#);
            cases.push((text, EVENTS_ERROR));
        }
    }
    for kind in [
        "null",
        "true",
        "3",
        "1.5",
        r#"["subscribe"]"#,
        r#"{"x": 1}"#,
    ] {
        cases.push((format!(r#"{{"type":{kind},"events":["a"]}}"#), TYPE_ERROR));
    }
    // A missing type is not a string either; Python would answer "Unknown message type: None".
    cases.push((r#"{"events":["a"]}"#.to_owned(), TYPE_ERROR));
    for text in [
        "",
        "{",
        "not json",
        r#"{"type":"subscribe",}"#,
        r#"{"type":"subscribe"} x"#,
    ] {
        cases.push((text.to_owned(), INVALID_JSON_ERROR));
    }
    for text in [
        "[]",
        r#"["subscribe"]"#,
        r#""subscribe""#,
        "42",
        "null",
        "true",
    ] {
        cases.push((text.to_owned(), NOT_OBJECT_ERROR));
    }
    // Supported-domain parity (also in the corpus): a non-list container.
    for control in ["subscribe", "unsubscribe"] {
        for container in [r#""a""#, "{}", "null", "5", "true"] {
            let text = format!(r#"{{"type":"{control}","events":{container}}}"#);
            cases.push((text, EVENTS_ERROR));
        }
    }

    for (text, expected) in cases {
        for state in every_state() {
            let mut after = state.clone();
            let outcome = after.apply_subscription_request(&text);
            assert_eq!(
                outcome,
                Inbound::Rejected(expected),
                "{text}: rejection from {state:?}"
            );
            assert_eq!(after, state, "{text}: state mutated");
        }
    }
}

#[test]
fn application_frames_are_opaque() {
    let application = [
        r#"{"type":"tool_call","events":[1,null],"request_id":"r1"}"#,
        r#"{"type":"subscribe_success","events":"x"}"#,
        r#"{"type":"Subscribe","events":["*"]}"#,
        r#"{"type":"no_such_type"}"#,
        " { \"z\" : 1, \"type\" : \"chat_message\", \"text\": \"caf\\u00e9 \\ud83d\\ude80\" } ",
    ];
    for text in application {
        for state in every_state() {
            let mut after = state.clone();
            match after.apply_subscription_request(text) {
                Inbound::Application(forwarded) => {
                    assert!(std::ptr::eq(forwarded, text), "{text}: frame was copied");
                }
                other => panic!("{text}: expected application frame, got {other:?}"),
            }
            assert_eq!(after, state, "{text}: application frame changed state");
        }
    }

    // A transport control with the same shape is handled, not forwarded.
    let mut state = SubscriptionState::new(EntryMode::Direct);
    let outcome = state.apply_subscription_request(r#"{"type":"subscribe","events":["a"]}"#);
    assert_eq!(
        outcome,
        Inbound::Ack(r#"{"type": "subscribe_success", "events": ["a"]}"#.to_owned())
    );
    assert_eq!(
        state,
        SubscriptionState::Initialized(BTreeSet::from(["a".to_owned()]))
    );
}

#[test]
fn acks_use_python_wire_format() {
    let mut state = SubscriptionState::new(EntryMode::Direct);
    let outcome = state.apply_subscription_request(
        r#"{"type":"subscribe","events":["z","café 🚀","q\"\\\n\u007f"]}"#,
    );
    // Python's ensure_ascii escapes, spelled with `bs` so no source escape is decoded.
    let expected = format!(
        r#"{{"type": "subscribe_success", "events": ["caf{bs}u00e9 {bs}ud83d{bs}ude80", "q{bs}"{bs}{bs}{bs}n{bs}u007f", "z"]}}"#,
        bs = '\\'
    );
    assert_eq!(outcome, Inbound::Ack(expected));
    let mut absent = SubscriptionState::new(EntryMode::Direct);
    let outcome = absent.apply_subscription_request(r#"{"type":"unsubscribe","events":["z"]}"#);
    assert_eq!(
        outcome,
        Inbound::Ack(r#"{"type": "unsubscribe_success", "events": []}"#.to_owned())
    );
    assert_eq!(absent, SubscriptionState::Absent);
}

#[test]
fn broadcast_filter_outside_corpus_domain() {
    let mut initialized = SubscriptionState::new(EntryMode::Browser);
    let ack = initialized
        .apply_subscription_request(r#"{"type":"subscribe","events":["hook_event:5=x","7"]}"#);
    assert!(matches!(ack, Inbound::Ack(_)), "fixture subscribe");
    // A non-string or missing type is never a gated event type.
    for message in [
        json!({"type": 5}),
        json!({"type": ["hook_event"]}),
        json!({"x": 1}),
    ] {
        assert!(initialized.matches_broadcast(&message), "{message}");
        assert!(!SubscriptionState::new(EntryMode::Direct).matches_broadcast(&message));
        assert!(!SubscriptionState::ExplicitNull.matches_broadcast(&message));
    }
    // hook_event subtypes and selectors compare strings only.
    for message in [
        json!({"type": "hook_event", "event_type": 7}),
        json!({"type": "hook_event", "event_type": ""}),
        json!({"type": "hook_event", "5": 5}),
    ] {
        assert!(!initialized.matches_broadcast(&message), "{message}");
    }
    assert!(initialized.matches_broadcast(&json!({"type": "hook_event", "event_type": "7"})));
    assert!(initialized.matches_broadcast(&json!({"type": "hook_event", "5": "x"})));
}
