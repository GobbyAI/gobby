//! Inbox envelope schema v1.
//!
//! The envelope is what ghook enqueues to `~/.gobby/hooks/inbox/` and what
//! the daemon drain replays. Schema is frozen at v1 and validated in tests
//! against `schemas/inbox-envelope.v1.schema.json`.
//!
//! Omitted headers (no project id, no session id) are absent from the
//! `headers` object — never emitted as empty strings. This matches the
//! dispatcher's `_context_headers.setdefault` behavior where the key simply
//! isn't inserted (`hook_dispatcher.py:695-700`).

use serde::Serialize;
use serde_json::Value;
use std::collections::BTreeMap;

pub const SCHEMA_VERSION: u32 = 1;

/// Inbox-envelope schema v1.
///
/// Field order follows the schema. `headers` is serialized as a plain
/// object; absent headers are not keys. `input_data` is the original stdin
/// payload verbatim (with `terminal_context` injected when applicable).
#[derive(Debug, Serialize)]
pub struct Envelope {
    pub schema_version: u32,
    pub enqueued_at: String,
    pub critical: bool,
    pub hook_type: String,
    pub input_data: Value,
    pub source: String,
    pub headers: BTreeMap<String, String>,
}

impl Envelope {
    pub fn new(
        critical: bool,
        hook_type: String,
        input_data: Value,
        source: String,
        headers: BTreeMap<String, String>,
    ) -> Self {
        Self {
            schema_version: SCHEMA_VERSION,
            enqueued_at: chrono::Utc::now().to_rfc3339(),
            critical,
            hook_type,
            input_data,
            source,
            headers,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn example_envelope() -> Envelope {
        let mut headers = BTreeMap::new();
        headers.insert("X-Gobby-Project-Id".into(), "proj-123".into());
        headers.insert("X-Gobby-Session-Id".into(), "sess-abc".into());
        Envelope::new(
            true,
            "session-start".into(),
            json!({"session_id": "sess-abc"}),
            "claude".into(),
            headers,
        )
    }

    #[test]
    fn envelope_serializes_with_expected_fields() {
        let env = example_envelope();
        let v: Value = serde_json::to_value(&env).unwrap();
        assert_eq!(v["schema_version"], 1);
        assert_eq!(v["critical"], true);
        assert_eq!(v["hook_type"], "session-start");
        assert_eq!(v["source"], "claude");
        assert_eq!(v["headers"]["X-Gobby-Project-Id"], "proj-123");
        assert_eq!(v["headers"]["X-Gobby-Session-Id"], "sess-abc");
        assert_eq!(v["input_data"]["session_id"], "sess-abc");
        assert!(v["enqueued_at"].as_str().unwrap().contains('T'));
    }

    #[test]
    fn droid_envelope_preserves_pascal_hook_and_source() {
        let env = Envelope::new(
            false,
            "PreToolUse".into(),
            json!({
                "session_id": "droid-session",
                "transcript_path": "/tmp/droid.jsonl",
                "cwd": "/tmp/project",
                "permission_mode": "default",
                "hook_event_name": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": "src/main.rs"}
            }),
            "droid".into(),
            BTreeMap::new(),
        );
        let v: Value = serde_json::to_value(&env).unwrap();

        assert_eq!(v["hook_type"], "PreToolUse");
        assert_eq!(v["source"], "droid");
        assert_eq!(v["input_data"]["hook_event_name"], "PreToolUse");
        assert_eq!(v["input_data"]["tool_input"]["file_path"], "src/main.rs");
    }

    #[test]
    fn empty_headers_serialize_as_empty_object() {
        let env = Envelope::new(
            false,
            "session-end".into(),
            json!({}),
            "claude".into(),
            BTreeMap::new(),
        );
        let v: Value = serde_json::to_value(&env).unwrap();
        assert!(v["headers"].is_object());
        assert_eq!(v["headers"].as_object().unwrap().len(), 0);
    }

    #[test]
    fn envelope_validates_against_v1_schema() {
        let schema_bytes = include_bytes!("../schemas/inbox-envelope.v1.schema.json");
        let schema: Value = serde_json::from_slice(schema_bytes).unwrap();
        let compiled = jsonschema::JSONSchema::options()
            .with_draft(jsonschema::Draft::Draft7)
            .compile(&schema)
            .expect("schema compiles");
        let env = example_envelope();
        let instance = serde_json::to_value(&env).unwrap();
        let result = compiled.validate(&instance);
        if let Err(errors) = result {
            let errs: Vec<_> = errors.map(|e| format!("{e}")).collect();
            panic!("envelope failed schema validation: {errs:?}");
        }
    }

    #[test]
    fn envelope_without_headers_validates_against_v1_schema() {
        let schema_bytes = include_bytes!("../schemas/inbox-envelope.v1.schema.json");
        let schema: Value = serde_json::from_slice(schema_bytes).unwrap();
        let compiled = jsonschema::JSONSchema::options()
            .with_draft(jsonschema::Draft::Draft7)
            .compile(&schema)
            .expect("schema compiles");
        let env = Envelope::new(
            false,
            "pre-tool-use".into(),
            json!({"tool_name": "Read"}),
            "claude".into(),
            BTreeMap::new(),
        );
        let instance = serde_json::to_value(&env).unwrap();
        if let Err(errors) = compiled.validate(&instance) {
            let errs: Vec<_> = errors.map(|e| format!("{e}")).collect();
            panic!("envelope failed schema validation: {errs:?}");
        }
    }
}
