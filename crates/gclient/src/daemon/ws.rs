//! Canonical terminal-WS JSON codec matching 2.5 goldens.

use serde_json::{Map, Value};
use thiserror::Error;

pub const TERMINAL_WS_SAFE_INTEGER_MAX: u64 = (1_u64 << 53) - 1;

pub const GOLDEN_NAMES: &[&str] = &[
    "attach.json",
    "attach_result.json",
    "attach_result_error.json",
    "detach.json",
    "resize.json",
    "set_viewport.json",
    "set_scroll_offset.json",
    "scroll_offset_applied.json",
    "list.json",
    "create.json",
    "create_result.json",
    "kill.json",
    "input.json",
    "write_outcome.json",
    "write_outcome_indeterminate.json",
    "write_outcome_refused.json",
    "write_outcome_conflict.json",
    "write_outcome_expired.json",
    "write_outcome_capacity.json",
    "output.json",
    "attach_history.json",
    "fragment.json",
    "fragment_last.json",
    "paste.json",
    "take_control.json",
    "release_control.json",
    "control_result.json",
    "lease_lost.json",
    "attachment_finalized.json",
    "event.json",
    "typed_error.json",
];

const SAFE_INTEGER_FIELDS: &[&str] = &[
    "message_seq",
    "lease_generation",
    "client_write_seq",
    "fragment_index",
];

#[derive(Debug, Error)]
pub enum WsCodecError {
    #[error("safe_integer_overflow")]
    SafeIntegerOverflow,
    #[error("terminal WS messages must not carry mode")]
    ModeForbidden,
    #[error("terminal WS payload must be an object")]
    NotObject,
    #[error("{0}")]
    Json(#[from] serde_json::Error),
}

fn check_safe_int(value: &Value) -> Result<u64, WsCodecError> {
    match value {
        Value::Number(number) => {
            if let Some(n) = number.as_u64() {
                if n <= TERMINAL_WS_SAFE_INTEGER_MAX {
                    return Ok(n);
                }
            }
            Err(WsCodecError::SafeIntegerOverflow)
        }
        _ => Err(WsCodecError::SafeIntegerOverflow),
    }
}

fn walk_safe_ints(payload: &Value) -> Result<(), WsCodecError> {
    match payload {
        Value::Object(map) => {
            for (key, value) in map {
                if SAFE_INTEGER_FIELDS.contains(&key.as_str()) {
                    check_safe_int(value)?;
                } else {
                    walk_safe_ints(value)?;
                }
            }
            Ok(())
        }
        Value::Array(items) => {
            for item in items {
                walk_safe_ints(item)?;
            }
            Ok(())
        }
        _ => Ok(()),
    }
}

fn sort_value(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut sorted = Map::new();
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            for key in keys {
                sorted.insert(key.clone(), sort_value(&map[key]));
            }
            Value::Object(sorted)
        }
        Value::Array(items) => Value::Array(items.iter().map(sort_value).collect()),
        other => other.clone(),
    }
}

/// Serialize a protocol object to canonical JSON bytes (sorted keys + newline).
pub fn encode_message(message: &Value) -> Result<Vec<u8>, WsCodecError> {
    if !message.is_object() {
        return Err(WsCodecError::NotObject);
    }
    if message.get("mode").is_some() {
        return Err(WsCodecError::ModeForbidden);
    }
    walk_safe_ints(message)?;
    let sorted = sort_value(message);
    let mut dumped = serde_json::to_string(&sorted)?;
    dumped.push('\n');
    Ok(dumped.into_bytes())
}

/// Parse a canonical protocol payload.
pub fn decode_message(raw: &[u8]) -> Result<Value, WsCodecError> {
    let parsed: Value = serde_json::from_slice(raw)?;
    if !parsed.is_object() {
        return Err(WsCodecError::NotObject);
    }
    walk_safe_ints(&parsed)?;
    Ok(parsed)
}
