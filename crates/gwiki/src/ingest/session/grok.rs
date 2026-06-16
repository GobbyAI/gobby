use serde::Deserialize;
use serde_json::Value;

use super::{
    ParsedSession, ParsedSessionMessage, ParsedSessionMetadata, SessionArchiveEnvelope,
    SessionTranscriptAdapter, json_string_field, non_empty_optional, non_empty_string, pretty_json,
};
use crate::WikiError;

pub(super) static GROK_SESSION_ADAPTER: GrokSessionAdapter = GrokSessionAdapter;

pub(super) struct GrokSessionAdapter;

impl SessionTranscriptAdapter for GrokSessionAdapter {
    fn supports(&self, envelope_type: &str) -> bool {
        matches!(
            envelope_type,
            "assistant" | "end" | "error" | "system" | "text" | "thought" | "tool_result" | "user"
        )
    }

    fn supports_archive(&self, envelopes: &[SessionArchiveEnvelope]) -> bool {
        envelopes
            .iter()
            .any(|envelope| matches!(envelope.envelope_type.as_str(), "end" | "text"))
            || envelopes.iter().any(|envelope| {
                envelope.payload.get("tool_calls").is_some()
                    || envelope.payload.get("tool_call_id").is_some()
                    || json_string_field(&envelope.payload, "model_id")
                        .is_some_and(|model| model.contains("grok"))
            })
    }

    fn parse(&self, envelopes: &[SessionArchiveEnvelope]) -> Result<ParsedSession, WikiError> {
        let mut started_at = None;
        let mut metadata = ParsedSessionMetadata::default();
        let mut messages = Vec::new();

        for envelope in envelopes
            .iter()
            .filter(|envelope| self.supports(&envelope.envelope_type))
        {
            let record = serde_json::from_value::<GrokRecord>(envelope.payload.clone()).map_err(
                |source| WikiError::Json {
                    action: "parse Grok session record",
                    path: None,
                    source,
                },
            )?;
            started_at = started_at.or_else(|| {
                non_empty_optional(record.timestamp.clone()).or_else(|| envelope.timestamp.clone())
            });
            metadata.set_model_once(record.model_id.as_deref());

            match record.record_type.as_str() {
                "user" | "assistant" => {
                    if let Some(message) =
                        parsed_grok_chat_message(&record, envelope.timestamp.as_deref())
                    {
                        push_or_append_message(&mut messages, message, false);
                    }
                    for message in parsed_grok_tool_calls(&record, envelope.timestamp.as_deref()) {
                        messages.push(message);
                    }
                }
                "tool_result" => {
                    if let Some(message) =
                        parsed_grok_tool_result(&record, envelope.timestamp.as_deref())
                    {
                        messages.push(message);
                    }
                }
                "text" => {
                    if let Some(message) =
                        parsed_grok_stream_text(&record, envelope.timestamp.as_deref())
                    {
                        push_or_append_message(&mut messages, message, true);
                    }
                }
                "error" => {
                    if let Some(message) = parsed_grok_error(&record, envelope.timestamp.as_deref())
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
                message: "Grok session archive must contain at least one message".to_string(),
            });
        }

        Ok(ParsedSession {
            title: "Grok session".to_string(),
            session_type: "grok-cli".to_string(),
            started_at,
            metadata,
            messages,
        })
    }
}

#[derive(Debug, Clone, Deserialize)]
struct GrokRecord {
    #[serde(rename = "type")]
    record_type: String,
    timestamp: Option<String>,
    model_id: Option<String>,
    content: Option<Value>,
    data: Option<Value>,
    message: Option<String>,
    tool_calls: Option<Vec<GrokToolCall>>,
    tool_call_id: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
struct GrokToolCall {
    id: Option<String>,
    name: Option<String>,
    arguments: Option<Value>,
}

fn parsed_grok_chat_message(
    record: &GrokRecord,
    fallback_timestamp: Option<&str>,
) -> Option<ParsedSessionMessage> {
    let content = render_grok_content(record.content.as_ref()?)?;
    Some(ParsedSessionMessage {
        role: record.record_type.clone(),
        timestamp: non_empty_optional(record.timestamp.clone())
            .or_else(|| fallback_timestamp.map(str::to_string)),
        content,
        tool_names: Vec::new(),
    })
}

fn parsed_grok_stream_text(
    record: &GrokRecord,
    fallback_timestamp: Option<&str>,
) -> Option<ParsedSessionMessage> {
    let content = render_grok_content(record.data.as_ref()?)?;
    Some(ParsedSessionMessage {
        role: "assistant".to_string(),
        timestamp: non_empty_optional(record.timestamp.clone())
            .or_else(|| fallback_timestamp.map(str::to_string)),
        content,
        tool_names: Vec::new(),
    })
}

fn parsed_grok_tool_calls(
    record: &GrokRecord,
    fallback_timestamp: Option<&str>,
) -> Vec<ParsedSessionMessage> {
    record
        .tool_calls
        .as_deref()
        .unwrap_or_default()
        .iter()
        .map(|tool_call| {
            let name =
                non_empty_optional(tool_call.name.clone()).unwrap_or_else(|| "tool".to_string());
            let tool_name = name.clone();
            let mut content = format!("Tool call: {name}");
            append_call_id(&mut content, tool_call.id.as_deref());
            if let Some(arguments) = tool_call
                .arguments
                .as_ref()
                .filter(|value| !value.is_null())
            {
                content.push_str("\n\nArguments\n\n```json\n");
                content.push_str(&pretty_json(arguments));
                content.push_str("\n```");
            }
            ParsedSessionMessage {
                role: "tool call".to_string(),
                timestamp: non_empty_optional(record.timestamp.clone())
                    .or_else(|| fallback_timestamp.map(str::to_string)),
                content,
                tool_names: vec![tool_name],
            }
        })
        .collect()
}

fn parsed_grok_tool_result(
    record: &GrokRecord,
    fallback_timestamp: Option<&str>,
) -> Option<ParsedSessionMessage> {
    let mut content = "Tool result".to_string();
    append_call_id(&mut content, record.tool_call_id.as_deref());
    let body = record
        .content
        .as_ref()
        .or(record.data.as_ref())
        .map(render_jsonish_or_text)
        .or_else(|| record.message.as_deref().and_then(non_empty_string))?;
    content.push_str("\n\n");
    content.push_str(&body);

    Some(ParsedSessionMessage {
        role: "tool result".to_string(),
        timestamp: non_empty_optional(record.timestamp.clone())
            .or_else(|| fallback_timestamp.map(str::to_string)),
        content,
        tool_names: Vec::new(),
    })
}

fn parsed_grok_error(
    record: &GrokRecord,
    fallback_timestamp: Option<&str>,
) -> Option<ParsedSessionMessage> {
    let content = record
        .message
        .as_ref()
        .and_then(|message| non_empty_string(message))
        .or_else(|| record.data.as_ref().and_then(render_grok_content))
        .or_else(|| record.content.as_ref().and_then(render_grok_content))?;

    Some(ParsedSessionMessage {
        role: "error".to_string(),
        timestamp: non_empty_optional(record.timestamp.clone())
            .or_else(|| fallback_timestamp.map(str::to_string)),
        content,
        tool_names: Vec::new(),
    })
}

fn push_or_append_message(
    messages: &mut Vec<ParsedSessionMessage>,
    message: ParsedSessionMessage,
    append_to_previous: bool,
) {
    if append_to_previous
        && let Some(previous) = messages.last_mut()
        && previous.role == message.role
    {
        previous.content.push_str(&message.content);
        previous.timestamp = previous.timestamp.clone().or(message.timestamp);
        previous.tool_names.extend(message.tool_names);
        return;
    }

    messages.push(message);
}

fn render_grok_content(content: &Value) -> Option<String> {
    let mut parts = Vec::new();
    append_grok_content(content, &mut parts);
    non_empty_string(&parts.join("\n\n"))
}

fn append_grok_content(value: &Value, parts: &mut Vec<String>) {
    match value {
        Value::String(text) => {
            if let Some(text) = non_empty_string(text) {
                parts.push(text);
            }
        }
        Value::Array(items) => {
            for item in items {
                if let Some(part) = render_grok_content_block(item) {
                    parts.push(part);
                }
            }
        }
        Value::Object(_) => {
            if let Some(part) = render_grok_content_block(value) {
                parts.push(part);
            }
        }
        _ => {}
    }
}

fn render_grok_content_block(block: &Value) -> Option<String> {
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
    fn grok_adapter_parses_local_chat_history_messages_and_tools() {
        let envelopes = vec![
            SessionArchiveEnvelope {
                envelope_type: "system".to_string(),
                timestamp: None,
                payload: serde_json::json!({
                    "type": "system",
                    "content": "You are Grok."
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "user".to_string(),
                timestamp: None,
                payload: serde_json::json!({
                    "type": "user",
                    "content": [{"type": "text", "text": "Run the command."}]
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "assistant".to_string(),
                timestamp: None,
                payload: serde_json::json!({
                    "type": "assistant",
                    "content": "",
                    "model_id": "grok-build",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "name": "run_terminal_command",
                            "arguments": {"command": "echo hello"}
                        }
                    ]
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "tool_result".to_string(),
                timestamp: None,
                payload: serde_json::json!({
                    "type": "tool_result",
                    "tool_call_id": "call_1",
                    "content": "exit: 0\nhello\n"
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "assistant".to_string(),
                timestamp: None,
                payload: serde_json::json!({
                    "type": "assistant",
                    "content": "exit: 0\nhello",
                    "model_id": "grok-build"
                }),
            },
        ];
        let adapters = default_session_adapters();

        let parsed = parse_session_archive(&envelopes, &adapters).expect("parse Grok fixture");

        assert_eq!(parsed.title, "Grok session");
        assert_eq!(parsed.session_type, "grok-cli");
        assert_eq!(parsed.messages.len(), 4);
        assert_eq!(parsed.messages[0].role, "user");
        assert_eq!(parsed.messages[0].content, "Run the command.");
        assert_eq!(parsed.messages[1].role, "tool call");
        assert!(parsed.messages[1].content.contains("run_terminal_command"));
        assert_eq!(parsed.messages[2].role, "tool result");
        assert!(parsed.messages[2].content.contains("hello"));
        assert_eq!(parsed.messages[3].role, "assistant");
    }

    #[test]
    fn grok_adapter_parses_streaming_json_text_chunks() {
        let envelopes = vec![
            SessionArchiveEnvelope {
                envelope_type: "text".to_string(),
                timestamp: None,
                payload: serde_json::json!({"type": "text", "data": "hel"}),
            },
            SessionArchiveEnvelope {
                envelope_type: "text".to_string(),
                timestamp: None,
                payload: serde_json::json!({"type": "text", "data": "lo"}),
            },
            SessionArchiveEnvelope {
                envelope_type: "end".to_string(),
                timestamp: None,
                payload: serde_json::json!({
                    "type": "end",
                    "stopReason": "EndTurn",
                    "sessionId": "session-1"
                }),
            },
        ];
        let adapters = default_session_adapters();

        let parsed = parse_session_archive(&envelopes, &adapters).expect("parse Grok stream");

        assert_eq!(parsed.messages.len(), 1);
        assert_eq!(parsed.messages[0].role, "assistant");
        assert_eq!(parsed.messages[0].content, "hello");
    }

    #[test]
    fn grok_adapter_parses_real_archive_when_fixture_is_set() {
        let Ok(path) = std::env::var("GWIKI_GROK_ARCHIVE_FIXTURE") else {
            return;
        };
        let bytes = std::fs::read(&path).expect("read real Grok fixture");
        let envelopes = read_session_archive(Path::new(&path), &bytes).expect("read Grok fixture");
        let adapters = default_session_adapters();

        let parsed = parse_session_archive(&envelopes, &adapters).expect("parse Grok fixture");

        assert_eq!(parsed.session_type, "grok-cli");
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
