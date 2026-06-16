use serde::Deserialize;
use serde_json::Value;

use super::{
    ParsedSession, ParsedSessionMessage, SessionArchiveEnvelope, SessionTranscriptAdapter,
    json_string_field, non_empty_optional, non_empty_string, pretty_json,
};
use crate::WikiError;

pub(super) static DROID_SESSION_ADAPTER: DroidSessionAdapter = DroidSessionAdapter;

pub(super) struct DroidSessionAdapter;

impl SessionTranscriptAdapter for DroidSessionAdapter {
    fn supports(&self, envelope_type: &str) -> bool {
        matches!(envelope_type, "message" | "session_start")
    }

    fn supports_archive(&self, envelopes: &[SessionArchiveEnvelope]) -> bool {
        envelopes.iter().any(is_droid_session_start)
    }

    fn parse(&self, envelopes: &[SessionArchiveEnvelope]) -> Result<ParsedSession, WikiError> {
        let mut title = None;
        let mut started_at = None;
        let mut messages = Vec::new();

        for envelope in envelopes
            .iter()
            .filter(|envelope| matches!(droid_record_type(envelope), "message" | "session_start"))
        {
            let record = serde_json::from_value::<DroidRecord>(envelope.payload.clone()).map_err(
                |source| WikiError::Json {
                    action: "parse Droid session record",
                    path: None,
                    source,
                },
            )?;
            let record_type = record
                .record_type
                .as_deref()
                .unwrap_or(&envelope.envelope_type);

            match record_type {
                "session_start" => {
                    title = title
                        .or_else(|| non_empty_optional(record.title.clone()))
                        .or_else(|| non_empty_optional(record.session_title.clone()));
                }
                "message" => {
                    if let Some(message) =
                        parsed_droid_message(&record, envelope.timestamp.as_deref())
                    {
                        started_at = started_at.or_else(|| message.timestamp.clone());
                        messages.push(message);
                    }
                }
                _ => {}
            }
        }

        if messages.is_empty() {
            return Err(WikiError::InvalidInput {
                field: "session.payload",
                message: "Droid session archive must contain at least one visible message"
                    .to_string(),
            });
        }

        Ok(ParsedSession {
            title: title.unwrap_or_else(|| "Droid session".to_string()),
            session_type: "droid-cli".to_string(),
            started_at,
            messages,
        })
    }
}

#[derive(Debug, Clone, Deserialize)]
struct DroidRecord {
    #[serde(default, rename = "type")]
    record_type: Option<String>,
    id: Option<String>,
    timestamp: Option<String>,
    title: Option<String>,
    #[serde(rename = "sessionTitle")]
    session_title: Option<String>,
    message: Option<DroidMessage>,
    visibility: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
struct DroidMessage {
    role: Option<String>,
    content: Option<Value>,
}

fn droid_record_type(envelope: &SessionArchiveEnvelope) -> &str {
    envelope
        .payload
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or(&envelope.envelope_type)
}

fn is_droid_session_start(envelope: &SessionArchiveEnvelope) -> bool {
    droid_record_type(envelope) == "session_start"
        && (envelope.payload.get("version").is_some()
            || json_string_field(&envelope.payload, "hostId").is_some())
        && (json_string_field(&envelope.payload, "cwd").is_some()
            || json_string_field(&envelope.payload, "sessionTitle").is_some()
            || json_string_field(&envelope.payload, "owner").is_some())
}

fn parsed_droid_message(
    record: &DroidRecord,
    fallback_timestamp: Option<&str>,
) -> Option<ParsedSessionMessage> {
    if is_hidden_context_record(record) {
        return None;
    }

    let message = record.message.as_ref()?;
    let content = render_droid_content(message.content.as_ref()?)?;
    let timestamp = non_empty_optional(record.timestamp.clone())
        .or_else(|| fallback_timestamp.map(str::to_string));

    Some(ParsedSessionMessage {
        role: droid_message_role(message),
        timestamp,
        content,
    })
}

fn is_hidden_context_record(record: &DroidRecord) -> bool {
    record
        .id
        .as_deref()
        .is_some_and(|id| id.starts_with("context-"))
        || record
            .visibility
            .as_deref()
            .is_some_and(|visibility| visibility == "llm_only")
}

fn droid_message_role(message: &DroidMessage) -> String {
    if message
        .content
        .as_ref()
        .is_some_and(|content| content_has_block_type(content, "tool_result"))
    {
        "tool result".to_string()
    } else {
        non_empty_optional(message.role.clone()).unwrap_or_else(|| "message".to_string())
    }
}

fn render_droid_content(content: &Value) -> Option<String> {
    let mut parts = Vec::new();
    append_droid_content(content, &mut parts);
    non_empty_string(&parts.join("\n\n"))
}

fn append_droid_content(value: &Value, parts: &mut Vec<String>) {
    match value {
        Value::String(text) => {
            if let Some(text) = non_empty_string(text) {
                parts.push(text);
            }
        }
        Value::Array(items) => {
            for item in items {
                if let Some(part) = render_droid_content_block(item) {
                    parts.push(part);
                }
            }
        }
        Value::Object(_) => {
            if let Some(part) = render_droid_content_block(value) {
                parts.push(part);
            }
        }
        _ => {}
    }
}

fn render_droid_content_block(block: &Value) -> Option<String> {
    let block_type = json_string_field(block, "type");
    match block_type.as_deref() {
        Some("thinking" | "reasoning") => None,
        Some("text") => json_string_field(block, "text"),
        Some("tool_use") => render_droid_tool_use(block),
        Some("tool_result") => render_droid_tool_result(block),
        _ => json_string_field(block, "text")
            .or_else(|| json_string_field(block, "content"))
            .or_else(|| Some(pretty_json(block))),
    }
}

fn render_droid_tool_use(block: &Value) -> Option<String> {
    let name = json_string_field(block, "name").unwrap_or_else(|| "tool".to_string());
    let mut rendered = format!("Tool use: {name}");
    if let Some(id) = json_string_field(block, "id") {
        rendered.push_str(": ");
        rendered.push_str(&id);
    }
    if let Some(input) = block.get("input").filter(|value| !value.is_null()) {
        rendered.push_str("\n\n```json\n");
        rendered.push_str(&pretty_json(input));
        rendered.push_str("\n```");
    }
    Some(rendered)
}

fn render_droid_tool_result(block: &Value) -> Option<String> {
    let mut rendered = if block
        .get("is_error")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        "Tool result (error)".to_string()
    } else {
        "Tool result".to_string()
    };

    if let Some(tool_use_id) = json_string_field(block, "tool_use_id") {
        rendered.push_str(": ");
        rendered.push_str(&tool_use_id);
    }

    if let Some(content) = block.get("content") {
        let mut parts = Vec::new();
        append_droid_content(content, &mut parts);
        let content = if parts.is_empty() {
            Some(render_jsonish_or_text(content))
        } else {
            non_empty_string(&parts.join("\n\n"))
        };
        if let Some(content) = content.and_then(|content| non_empty_string(&content)) {
            rendered.push_str("\n\n");
            rendered.push_str(&content);
        }
    }

    Some(rendered)
}

fn render_jsonish_or_text(value: &Value) -> String {
    if let Some(text) = value.as_str() {
        return text.to_string();
    }
    format!("```json\n{}\n```", pretty_json(value))
}

fn content_has_block_type(value: &Value, expected_type: &str) -> bool {
    match value {
        Value::Array(items) => items
            .iter()
            .any(|item| content_has_block_type(item, expected_type)),
        Value::Object(_) => value
            .get("type")
            .and_then(Value::as_str)
            .is_some_and(|content_type| content_type == expected_type),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::super::{default_session_adapters, parse_session_archive, read_session_archive};
    use super::*;

    #[test]
    fn droid_adapter_parses_persisted_messages_and_tools() {
        let envelopes = vec![
            SessionArchiveEnvelope {
                envelope_type: "session_start".to_string(),
                timestamp: None,
                payload: serde_json::json!({
                    "type": "session_start",
                    "id": "session-1",
                    "title": "Droid Fixture",
                    "sessionTitle": "Droid Fixture",
                    "owner": "josh",
                    "version": 2,
                    "cwd": "/workspace",
                    "hostId": "host-1"
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "message".to_string(),
                timestamp: Some("2026-06-16T18:33:00Z".to_string()),
                payload: serde_json::json!({
                    "type": "message",
                    "id": "context-1",
                    "timestamp": "2026-06-16T18:33:00Z",
                    "visibility": "llm_only",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "<system-reminder>hidden</system-reminder>"}]
                    }
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "message".to_string(),
                timestamp: Some("2026-06-16T18:33:01Z".to_string()),
                payload: serde_json::json!({
                    "type": "message",
                    "id": "user-1",
                    "timestamp": "2026-06-16T18:33:01Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Inspect the repo."}]
                    },
                    "parentId": "context-1"
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "message".to_string(),
                timestamp: Some("2026-06-16T18:33:02Z".to_string()),
                payload: serde_json::json!({
                    "type": "message",
                    "id": "assistant-1",
                    "timestamp": "2026-06-16T18:33:02Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "I will list files."},
                            {"type": "tool_use", "id": "toolu_1", "name": "Execute", "input": {"command": "ls"}}
                        ]
                    },
                    "parentId": "user-1"
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "message".to_string(),
                timestamp: Some("2026-06-16T18:33:03Z".to_string()),
                payload: serde_json::json!({
                    "type": "message",
                    "id": "tool-result-1",
                    "timestamp": "2026-06-16T18:33:03Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_1",
                                "is_error": false,
                                "content": "Cargo.toml\ncrates\n"
                            }
                        ]
                    },
                    "parentId": "assistant-1"
                }),
            },
        ];
        let adapters = default_session_adapters();

        let parsed = parse_session_archive(&envelopes, &adapters).expect("parse Droid fixture");

        assert_eq!(parsed.title, "Droid Fixture");
        assert_eq!(parsed.session_type, "droid-cli");
        assert_eq!(parsed.started_at.as_deref(), Some("2026-06-16T18:33:01Z"));
        assert_eq!(parsed.messages.len(), 3);
        assert_eq!(parsed.messages[0].role, "user");
        assert_eq!(parsed.messages[0].content, "Inspect the repo.");
        assert_eq!(parsed.messages[1].role, "assistant");
        assert!(parsed.messages[1].content.contains("I will list files."));
        assert!(
            parsed.messages[1]
                .content
                .contains("Tool use: Execute: toolu_1")
        );
        assert_eq!(parsed.messages[2].role, "tool result");
        assert!(parsed.messages[2].content.contains("Cargo.toml"));
        assert!(
            !parsed
                .messages
                .iter()
                .any(|message| message.content.contains("system-reminder"))
        );
    }

    #[test]
    fn droid_adapter_accepts_envelope_wrapped_payload_records() {
        let bytes = br#"{"type":"factory.droid","timestamp":"2026-06-16T18:33:00Z","payload":{"type":"session_start","id":"session-1","title":"Wrapped Droid","version":2,"cwd":"/workspace","hostId":"host-1"}}
{"type":"factory.droid","timestamp":"2026-06-16T18:33:01Z","payload":{"type":"message","id":"user-1","timestamp":"2026-06-16T18:33:01Z","message":{"role":"user","content":[{"type":"text","text":"Hello Droid."}]}}}
"#;
        let envelopes =
            read_session_archive(Path::new("droid.jsonl"), bytes).expect("read Droid archive");
        let adapters = default_session_adapters();

        let parsed = parse_session_archive(&envelopes, &adapters).expect("parse Droid archive");

        assert_eq!(parsed.title, "Wrapped Droid");
        assert_eq!(parsed.session_type, "droid-cli");
        assert_eq!(parsed.messages.len(), 1);
        assert_eq!(parsed.messages[0].content, "Hello Droid.");
    }

    #[test]
    fn droid_adapter_parses_real_archive_when_fixture_is_set() {
        let Ok(path) = std::env::var("GWIKI_DROID_ARCHIVE_FIXTURE") else {
            return;
        };
        let bytes = std::fs::read(&path).expect("read real Droid fixture");
        let envelopes = read_session_archive(Path::new(&path), &bytes).expect("read Droid fixture");
        let adapters = default_session_adapters();

        let parsed = parse_session_archive(&envelopes, &adapters).expect("parse Droid fixture");

        assert_eq!(parsed.session_type, "droid-cli");
        assert!(parsed.messages.iter().any(|message| message.role == "user"));
        assert!(
            parsed
                .messages
                .iter()
                .any(|message| message.role == "assistant"),
            "expected at least one assistant message"
        );
    }
}
