use serde::Deserialize;
use serde_json::Value;

use super::{
    ParsedSession, ParsedSessionMessage, SessionArchiveEnvelope, SessionTranscriptAdapter,
    json_string_field, non_empty_optional, non_empty_string, pretty_json,
};
use crate::WikiError;

pub(super) static GEMINI_SESSION_ADAPTER: GeminiSessionAdapter = GeminiSessionAdapter;

pub(super) struct GeminiSessionAdapter;

impl SessionTranscriptAdapter for GeminiSessionAdapter {
    fn supports(&self, envelope_type: &str) -> bool {
        matches!(
            envelope_type,
            "init" | "message" | "result" | "tool_call" | "tool_result" | "tool_error"
        )
    }

    fn parse(&self, envelopes: &[SessionArchiveEnvelope]) -> Result<ParsedSession, WikiError> {
        let mut started_at = None;
        let mut messages = Vec::new();

        for envelope in envelopes
            .iter()
            .filter(|envelope| self.supports(&envelope.envelope_type))
        {
            let record = serde_json::from_value::<GeminiRecord>(envelope.payload.clone()).map_err(
                |source| WikiError::Json {
                    action: "parse Gemini session record",
                    path: None,
                    source,
                },
            )?;
            started_at = started_at.or_else(|| {
                non_empty_optional(record.timestamp.clone()).or_else(|| envelope.timestamp.clone())
            });

            match record.record_type.as_str() {
                "message" => {
                    if let Some(message) =
                        parsed_gemini_message(&record, envelope.timestamp.as_deref())
                    {
                        push_gemini_message(&mut messages, message, record.delta.unwrap_or(false));
                    }
                }
                "tool_call" => {
                    if let Some(message) =
                        parsed_gemini_tool_call(&record, envelope.timestamp.as_deref())
                    {
                        messages.push(message);
                    }
                }
                "tool_result" | "tool_error" => {
                    if let Some(message) =
                        parsed_gemini_tool_result(&record, envelope.timestamp.as_deref())
                    {
                        messages.push(message);
                    }
                }
                _ => {}
            }
        }

        if messages.is_empty() {
            return Err(WikiError::InvalidInput {
                field: "session.payload",
                message: "Gemini session archive must contain at least one message".to_string(),
            });
        }

        Ok(ParsedSession {
            title: "Gemini CLI session".to_string(),
            session_type: "gemini-cli".to_string(),
            started_at,
            messages,
        })
    }
}

#[derive(Debug, Clone, Deserialize)]
struct GeminiRecord {
    #[serde(rename = "type")]
    record_type: String,
    timestamp: Option<String>,
    role: Option<String>,
    content: Option<Value>,
    delta: Option<bool>,
    name: Option<String>,
    call_id: Option<String>,
    arguments: Option<Value>,
    args: Option<Value>,
    output: Option<Value>,
    result: Option<Value>,
    error: Option<Value>,
}

fn parsed_gemini_message(
    record: &GeminiRecord,
    fallback_timestamp: Option<&str>,
) -> Option<ParsedSessionMessage> {
    let role = non_empty_optional(record.role.clone())?;
    let content = render_gemini_content(record.content.as_ref()?)?;
    let timestamp = non_empty_optional(record.timestamp.clone())
        .or_else(|| fallback_timestamp.map(str::to_string));

    Some(ParsedSessionMessage {
        role,
        timestamp,
        content,
    })
}

fn parsed_gemini_tool_call(
    record: &GeminiRecord,
    fallback_timestamp: Option<&str>,
) -> Option<ParsedSessionMessage> {
    let name = non_empty_optional(record.name.clone()).unwrap_or_else(|| "tool".to_string());
    let mut content = format!("Tool call: {name}");
    append_call_id(&mut content, record.call_id.as_deref());
    if let Some(arguments) = record
        .arguments
        .as_ref()
        .or(record.args.as_ref())
        .filter(|value| !value.is_null())
    {
        content.push_str("\n\nArguments\n\n```json\n");
        content.push_str(&pretty_json(arguments));
        content.push_str("\n```");
    }

    Some(ParsedSessionMessage {
        role: "tool call".to_string(),
        timestamp: non_empty_optional(record.timestamp.clone())
            .or_else(|| fallback_timestamp.map(str::to_string)),
        content,
    })
}

fn parsed_gemini_tool_result(
    record: &GeminiRecord,
    fallback_timestamp: Option<&str>,
) -> Option<ParsedSessionMessage> {
    let mut content = if record.record_type == "tool_error" {
        "Tool result (error)".to_string()
    } else {
        "Tool result".to_string()
    };
    append_call_id(&mut content, record.call_id.as_deref());
    let body = record
        .output
        .as_ref()
        .or(record.result.as_ref())
        .or(record.content.as_ref())
        .or(record.error.as_ref())?;
    content.push_str("\n\n");
    content.push_str(&render_jsonish_or_text(body));

    Some(ParsedSessionMessage {
        role: "tool result".to_string(),
        timestamp: non_empty_optional(record.timestamp.clone())
            .or_else(|| fallback_timestamp.map(str::to_string)),
        content,
    })
}

fn push_gemini_message(
    messages: &mut Vec<ParsedSessionMessage>,
    message: ParsedSessionMessage,
    is_delta: bool,
) {
    if is_delta
        && let Some(previous) = messages.last_mut()
        && previous.role == message.role
    {
        previous.content.push_str(&message.content);
        previous.timestamp = previous.timestamp.clone().or(message.timestamp);
        return;
    }

    messages.push(message);
}

fn render_gemini_content(content: &Value) -> Option<String> {
    let mut parts = Vec::new();
    append_gemini_content(content, &mut parts);
    non_empty_string(&parts.join("\n\n"))
}

fn append_gemini_content(value: &Value, parts: &mut Vec<String>) {
    match value {
        Value::String(text) => {
            if let Some(text) = non_empty_string(text) {
                parts.push(text);
            }
        }
        Value::Array(items) => {
            for item in items {
                if let Some(part) = render_gemini_content_block(item) {
                    parts.push(part);
                }
            }
        }
        Value::Object(_) => {
            if let Some(part) = render_gemini_content_block(value) {
                parts.push(part);
            }
        }
        _ => {}
    }
}

fn render_gemini_content_block(block: &Value) -> Option<String> {
    json_string_field(block, "text")
        .or_else(|| json_string_field(block, "content"))
        .or_else(|| Some(pretty_json(block)))
}

fn append_call_id(content: &mut String, call_id: Option<&str>) {
    if let Some(call_id) = call_id.and_then(non_empty_string) {
        content.push_str(": ");
        content.push_str(&call_id);
    }
}

fn render_jsonish_or_text(value: &Value) -> String {
    if let Some(text) = value.as_str() {
        return text.to_string();
    }
    format!("```json\n{}\n```", pretty_json(value))
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::super::{default_session_adapters, parse_session_archive, read_session_archive};
    use super::*;

    #[test]
    fn gemini_adapter_parses_stream_json_messages_and_tools() {
        let envelopes = vec![
            SessionArchiveEnvelope {
                envelope_type: "init".to_string(),
                timestamp: Some("2026-06-16T20:00:00Z".to_string()),
                payload: serde_json::json!({
                    "type": "init",
                    "timestamp": "2026-06-16T20:00:00Z",
                    "session_id": "session-1",
                    "model": "gemini-3.5-flash"
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "message".to_string(),
                timestamp: Some("2026-06-16T20:00:01Z".to_string()),
                payload: serde_json::json!({
                    "type": "message",
                    "timestamp": "2026-06-16T20:00:01Z",
                    "role": "user",
                    "content": "Reply with exactly OK."
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "message".to_string(),
                timestamp: Some("2026-06-16T20:00:02Z".to_string()),
                payload: serde_json::json!({
                    "type": "message",
                    "timestamp": "2026-06-16T20:00:02Z",
                    "role": "assistant",
                    "content": "O",
                    "delta": true
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "message".to_string(),
                timestamp: Some("2026-06-16T20:00:03Z".to_string()),
                payload: serde_json::json!({
                    "type": "message",
                    "timestamp": "2026-06-16T20:00:03Z",
                    "role": "assistant",
                    "content": "K",
                    "delta": true
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "tool_call".to_string(),
                timestamp: Some("2026-06-16T20:00:04Z".to_string()),
                payload: serde_json::json!({
                    "type": "tool_call",
                    "timestamp": "2026-06-16T20:00:04Z",
                    "name": "read_file",
                    "call_id": "call_1",
                    "arguments": {"path": "README.md"}
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "tool_result".to_string(),
                timestamp: Some("2026-06-16T20:00:05Z".to_string()),
                payload: serde_json::json!({
                    "type": "tool_result",
                    "timestamp": "2026-06-16T20:00:05Z",
                    "call_id": "call_1",
                    "output": "README contents"
                }),
            },
        ];
        let adapters = default_session_adapters();

        let parsed = parse_session_archive(&envelopes, &adapters).expect("parse Gemini fixture");

        assert_eq!(parsed.title, "Gemini CLI session");
        assert_eq!(parsed.session_type, "gemini-cli");
        assert_eq!(parsed.started_at.as_deref(), Some("2026-06-16T20:00:00Z"));
        assert_eq!(parsed.messages.len(), 4);
        assert_eq!(parsed.messages[0].role, "user");
        assert_eq!(parsed.messages[0].content, "Reply with exactly OK.");
        assert_eq!(parsed.messages[1].role, "assistant");
        assert_eq!(parsed.messages[1].content, "OK");
        assert_eq!(parsed.messages[2].role, "tool call");
        assert!(parsed.messages[2].content.contains("Tool call: read_file"));
        assert_eq!(parsed.messages[3].role, "tool result");
        assert!(parsed.messages[3].content.contains("README contents"));
    }

    #[test]
    fn gemini_adapter_parses_real_stream_when_fixture_is_set() {
        let Ok(path) = std::env::var("GWIKI_GEMINI_ARCHIVE_FIXTURE") else {
            return;
        };
        let bytes = std::fs::read(&path).expect("read real Gemini stream fixture");
        let envelopes =
            read_session_archive(Path::new(&path), &bytes).expect("read Gemini stream fixture");
        let adapters = default_session_adapters();

        let parsed = parse_session_archive(&envelopes, &adapters).expect("parse Gemini stream");

        assert_eq!(parsed.session_type, "gemini-cli");
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
