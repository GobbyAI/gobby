//! Subscription transport controls and broadcast selection.
//!
//! This is the native counterpart of the Python `subscribe`/`unsubscribe`
//! handlers and `BroadcastMixin._is_subscribed`, replayed against
//! `tests/fixtures/ws_envelope_golden`. Only the two transport controls are
//! parsed for meaning; every other inbound frame is returned untouched for the
//! application path, and broadcast frames never pass through this module.
//! Replies are written in Python's `json.dumps` wire format.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;

use serde_json::Value;
use serde_json::value::RawValue;

/// Broadcast types that reach only clients subscribed to them; every other
/// type reaches any initialized subscriber.
const GATED_EVENT_TYPES: [&str; 19] = [
    "hook_event",
    "session_message",
    "session_event",
    "session_usage_updated",
    "token_event",
    "agent_event",
    "agent_message",
    "worktree_event",
    "workspace_event",
    "autonomous_event",
    "pipeline_event",
    "terminal_output",
    "terminal_event",
    "skill_event",
    "mcp_event",
    "workflow_event",
    "project_event",
    "cron_event",
    "trace_event",
];

const WILDCARD: &str = "*";

const INVALID_JSON: &str =
    r#"{"type": "error", "code": "ERROR", "message": "Invalid JSON format"}"#;
const NOT_OBJECT: &str =
    r#"{"type": "error", "code": "ERROR", "message": "Message must be a JSON object"}"#;
const TYPE_NOT_STRING: &str =
    r#"{"type": "error", "code": "ERROR", "message": "Message type must be a string"}"#;
const EVENTS_NOT_STRINGS: &str =
    r#"{"type": "error", "code": "ERROR", "message": "events must be a list of strings"}"#;
const INTERNAL: &str = r#"{"type": "error", "code": "ERROR", "message": "Internal server error"}"#;

/// Trusted public entry a connection arrived through.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EntryMode {
    /// Dedicated public WS listener.
    Direct,
    /// HTTP listener `/ws` entry, including bearer clients on that path.
    Browser,
}

/// A connection's subscription set.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SubscriptionState {
    /// Never subscribed: every broadcast is filtered out.
    Absent,
    /// Internal null kept for parity with the Python oracle; no public entry
    /// produces it. Filters every broadcast and rejects unsubscribe.
    ExplicitNull,
    /// Initialized set; an empty set still admits non-gated broadcasts.
    Initialized(BTreeSet<String>),
}

/// What one inbound text frame resolved to.
#[derive(Debug, PartialEq, Eq)]
pub enum Inbound<'a> {
    /// A transport control applied to the state; send this acknowledgement.
    Ack(String),
    /// A rejected frame; send this error and leave the state as it was.
    Rejected(&'static str),
    /// An application frame, borrowed unchanged for the application path.
    Application(&'a str),
}

enum Control {
    Subscribe,
    Unsubscribe,
}

impl SubscriptionState {
    /// Initial state for a fresh connection on a public entry.
    pub fn new(entry: EntryMode) -> Self {
        match entry {
            EntryMode::Direct => Self::Absent,
            EntryMode::Browser => Self::Initialized(BTreeSet::new()),
        }
    }

    /// Resolve one inbound text frame. A subscription control is validated in
    /// full before the state changes, so a rejected frame never mutates it.
    pub fn apply_subscription_request<'a>(&mut self, text: &'a str) -> Inbound<'a> {
        let (control, events) = match parse_control(text) {
            Ok(Some(request)) => request,
            Ok(None) => return Inbound::Application(text),
            Err(error) => return Inbound::Rejected(error),
        };
        match control {
            Control::Subscribe => {
                let mut subs = match std::mem::replace(self, Self::Absent) {
                    Self::Initialized(subs) => subs,
                    Self::Absent | Self::ExplicitNull => BTreeSet::new(),
                };
                subs.extend(events);
                let ack = ack_frame("subscribe_success", &subs);
                *self = Self::Initialized(subs);
                Inbound::Ack(ack)
            }
            Control::Unsubscribe => match self {
                // Python raises on the null set; its handler-error path replies.
                Self::ExplicitNull => Inbound::Rejected(INTERNAL),
                // Acknowledged against a throwaway set; the state stays absent.
                Self::Absent => Inbound::Ack(ack_frame("unsubscribe_success", &BTreeSet::new())),
                Self::Initialized(subs) => {
                    if events.is_empty() || events.iter().any(|event| event == WILDCARD) {
                        subs.clear();
                    } else {
                        for event in &events {
                            subs.remove(event);
                        }
                    }
                    Inbound::Ack(ack_frame("unsubscribe_success", subs))
                }
            },
        }
    }

    /// Whether a broadcast of `message` reaches this connection. Fragmented
    /// broadcasts are selected by their original message, never a fragment.
    pub fn matches_broadcast(&self, message: &Value) -> bool {
        let Self::Initialized(subs) = self else {
            return false;
        };
        if subs.contains(WILDCARD) {
            return true;
        }
        let Some(msg_type) = message
            .get("type")
            .and_then(Value::as_str)
            .filter(|kind| GATED_EVENT_TYPES.contains(kind))
        else {
            return true;
        };
        if subs.contains(msg_type) {
            return true;
        }
        // Parametric selector "type:key=value", split at the first ':' and then
        // the first '='; only a string field equals the value.
        let selected = subs.iter().any(|sub| {
            sub.split_once(':')
                .filter(|(sub_type, _)| *sub_type == msg_type)
                .and_then(|(_, params)| params.split_once('='))
                .is_some_and(|(key, value)| message.get(key).and_then(Value::as_str) == Some(value))
        });
        // hook_event also matches by subtype: a non-empty string event_type.
        selected
            || (msg_type == "hook_event"
                && message
                    .get("event_type")
                    .and_then(Value::as_str)
                    .is_some_and(|event_type| !event_type.is_empty() && subs.contains(event_type)))
    }
}

/// `Ok(None)` is an application frame. Values other than `type` and `events`
/// are skipped without being materialized.
fn parse_control(text: &str) -> Result<Option<(Control, Vec<String>)>, &'static str> {
    let fields: BTreeMap<String, &RawValue> = serde_json::from_str(text).map_err(|error| {
        if error.is_data() {
            NOT_OBJECT
        } else {
            INVALID_JSON
        }
    })?;
    let kind: String = fields
        .get("type")
        .and_then(|raw| serde_json::from_str(raw.get()).ok())
        .ok_or(TYPE_NOT_STRING)?;
    let control = match kind.as_str() {
        "subscribe" => Control::Subscribe,
        "unsubscribe" => Control::Unsubscribe,
        _ => return Ok(None),
    };
    let events = match fields.get("events") {
        None => Vec::new(),
        Some(raw) => serde_json::from_str(raw.get()).map_err(|_| EVENTS_NOT_STRINGS)?,
    };
    Ok(Some((control, events)))
}

fn ack_frame(kind: &str, events: &BTreeSet<String>) -> String {
    let mut frame = format!(r#"{{"type": "{kind}", "events": ["#);
    for (index, event) in events.iter().enumerate() {
        if index > 0 {
            frame.push_str(", ");
        }
        push_python_string(&mut frame, event);
    }
    frame.push_str("]}");
    frame
}

/// Python `json.dumps` string encoding with `ensure_ascii`: everything outside
/// printable ASCII is a lowercase `\uXXXX` escape, using surrogate pairs.
fn push_python_string(out: &mut String, value: &str) {
    out.push('"');
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(ch),
            _ => {
                for unit in ch.encode_utf16(&mut [0; 2]) {
                    // Writing to a String cannot fail.
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
        }
    }
    out.push('"');
}
