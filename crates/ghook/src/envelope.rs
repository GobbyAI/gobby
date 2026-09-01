//! Inbox envelope schema v1.
//!
//! The envelope is what ghook enqueues to `~/.gobby/hooks/inbox/` and what
//! the daemon drain replays. Schema is frozen at v1 and validated in tests
//! against `schemas/inbox-envelope.v1.schema.json`.
//!
//! Omitted headers (no project id, no session id) are absent from the
//! `headers` object — never emitted as empty strings. The schema enforces
//! this with `additionalProperties.minLength: 1`, so an empty value is a
//! validation failure rather than a header the daemon has to special-case.

use serde::Serialize;
use serde_json::Value;
use std::collections::BTreeMap;

pub const SCHEMA_VERSION: u32 = 1;
pub const RESPONSE_CAPABILITY: &str = "hook-response.v1";

/// Inbox-envelope schema v1.
///
/// Field order follows the schema. `headers` is serialized as a plain
/// object; absent headers are not keys. `input_data` is the original stdin
/// payload verbatim (with valid tmux `terminal_context` injected when present).
#[derive(Debug, Serialize)]
pub struct Envelope {
    pub schema_version: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub response_capability: Option<String>,
    pub enqueued_at: String,
    pub critical: bool,
    pub hook_type: String,
    pub input_data: Value,
    pub source: String,
    pub headers: BTreeMap<String, String>,
}

pub const DELIVERY_RECEIPT_KIND: &str = "delivery-receipt";

/// Versioned delivery-receipt ghook enqueues after emission-plus-flush.
#[derive(Debug, Serialize)]
pub struct DeliveryReceipt {
    pub schema_version: u32,
    pub kind: String,
    pub enqueued_at: String,
    pub receipt_id: String,
    pub original_envelope_id: String,
    pub delivery_generation: u64,
}

impl DeliveryReceipt {
    pub fn new(
        receipt_id: impl Into<String>,
        original_envelope_id: impl Into<String>,
        delivery_generation: u64,
    ) -> Self {
        Self {
            schema_version: SCHEMA_VERSION,
            kind: DELIVERY_RECEIPT_KIND.to_string(),
            enqueued_at: chrono::Utc::now().to_rfc3339(),
            receipt_id: receipt_id.into(),
            original_envelope_id: original_envelope_id.into(),
            delivery_generation,
        }
    }
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
            response_capability: Some(RESPONSE_CAPABILITY.to_string()),
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
        assert_eq!(v["response_capability"], "hook-response.v1");
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
    fn inbox_envelope_schema_mirrors_are_byte_identical() {
        let crate_schema = include_bytes!("../schemas/inbox-envelope.v1.schema.json");
        let public_schema = include_bytes!("../../../schemas/inbox-envelope.v1.schema.json");
        assert_eq!(crate_schema.as_slice(), public_schema.as_slice());
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

    #[test]
    fn delivery_receipt_schema_mirrors_are_byte_identical() {
        let crate_schema = include_bytes!("../schemas/delivery-receipt.v1.schema.json");
        let public_schema = include_bytes!("../../../schemas/delivery-receipt.v1.schema.json");
        assert_eq!(crate_schema.as_slice(), public_schema.as_slice());
    }

    #[test]
    fn delivery_receipt_validates_against_v1_schema() {
        let schema_bytes = include_bytes!("../schemas/delivery-receipt.v1.schema.json");
        let schema: Value = serde_json::from_slice(schema_bytes).unwrap();
        let compiled = jsonschema::JSONSchema::options()
            .with_draft(jsonschema::Draft::Draft7)
            .compile(&schema)
            .expect("schema compiles");
        let receipt = DeliveryReceipt::new("r1", "env-1", 1);
        let instance = serde_json::to_value(&receipt).unwrap();
        if let Err(errors) = compiled.validate(&instance) {
            let errs: Vec<_> = errors.map(|e| format!("{e}")).collect();
            panic!("delivery receipt failed schema validation: {errs:?}");
        }
        assert_eq!(instance["kind"], "delivery-receipt");
        assert_eq!(instance["receipt_id"], "r1");
        assert_eq!(instance["original_envelope_id"], "env-1");
        assert_eq!(instance["delivery_generation"], 1);
    }

    #[test]
    fn envelope_without_response_capability_validates_as_legacy() {
        let schema_bytes = include_bytes!("../schemas/inbox-envelope.v1.schema.json");
        let schema: Value = serde_json::from_slice(schema_bytes).unwrap();
        let compiled = jsonschema::JSONSchema::options()
            .with_draft(jsonschema::Draft::Draft7)
            .compile(&schema)
            .expect("schema compiles");
        let instance = json!({
            "schema_version": 1,
            "enqueued_at": "2026-04-16T12:00:00Z",
            "critical": false,
            "hook_type": "session-start",
            "input_data": {},
            "source": "claude",
            "headers": {}
        });
        if let Err(errors) = compiled.validate(&instance) {
            let errs: Vec<_> = errors.map(|e| format!("{e}")).collect();
            panic!("absent response_capability must parse as legacy, not malformed: {errs:?}");
        }
    }
}
