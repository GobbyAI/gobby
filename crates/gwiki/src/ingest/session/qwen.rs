use serde::Deserialize;
use serde_json::Value;

use super::{
    ParsedSession, ParsedSessionMessage, ParsedSessionMetadata, SessionArchiveEnvelope,
    SessionTranscriptAdapter, json_string_field, non_empty_optional, non_empty_string, pretty_json,
};
use crate::WikiError;

pub(super) static QWEN_SESSION_ADAPTER: QwenSessionAdapter = QwenSessionAdapter;

pub(super) struct QwenSessionAdapter;

impl SessionTranscriptAdapter for QwenSessionAdapter {
    fn supports(&self, envelope_type: &str) -> bool {
        matches!(
            envelope_type,
            "assistant" | "system" | "tool_result" | "user"
        )
    }

    fn supports_archive(&self, envelopes: &[SessionArchiveEnvelope]) -> bool {
        envelopes.iter().any(is_qwen_record)
    }

    fn parse(&self, envelopes: &[SessionArchiveEnvelope]) -> Result<ParsedSession, WikiError> {
        let mut started_at = None;
        let mut metadata = ParsedSessionMetadata::default();
        let mut messages = Vec::new();

        for envelope in envelopes
            .iter()
            .filter(|envelope| self.supports(&envelope.envelope_type))
        {
            let record = serde_json::from_value::<QwenRecord>(envelope.payload.clone()).map_err(
                |source| WikiError::Json {
                    action: "parse Qwen session record",
                    path: None,
                    source,
                },
            )?;
            let record_type = record
                .record_type
                .as_deref()
                .unwrap_or(&envelope.envelope_type);
            metadata.set_model_once(record.model.as_deref());
            metadata.set_git_branch_once(record.git_branch.as_deref());

            match record_type {
                "assistant" | "tool_result" | "user" => {
                    if let Some(message) =
                        parsed_qwen_message(record_type, &record, envelope.timestamp.as_deref())
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
                message: "Qwen session archive must contain at least one visible message"
                    .to_string(),
            });
        }

        Ok(ParsedSession {
            title: "Qwen session".to_string(),
            session_type: "qwen-code".to_string(),
            started_at,
            metadata,
            messages,
        })
    }
}

#[derive(Deserialize)]
struct QwenRecord {
    #[serde(default, rename = "type")]
    record_type: Option<String>,
    timestamp: Option<String>,
    model: Option<String>,
    #[serde(rename = "gitBranch")]
    git_branch: Option<String>,
    message: Option<QwenMessage>,
}

#[derive(Deserialize)]
struct QwenMessage {
    role: Option<String>,
    #[serde(default)]
    parts: Vec<Value>,
}

fn qwen_record_type(envelope: &SessionArchiveEnvelope) -> &str {
    envelope
        .payload
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or(&envelope.envelope_type)
}

fn is_qwen_record(envelope: &SessionArchiveEnvelope) -> bool {
    if !matches!(
        qwen_record_type(envelope),
        "assistant" | "system" | "tool_result" | "user"
    ) {
        return false;
    }

    json_string_field(&envelope.payload, "sessionId").is_some()
        && json_string_field(&envelope.payload, "uuid").is_some()
        && envelope.payload.get("parentUuid").is_some()
        && json_string_field(&envelope.payload, "cwd").is_some()
        && json_string_field(&envelope.payload, "version").is_some()
        && (envelope
            .payload
            .pointer("/message/parts")
            .and_then(Value::as_array)
            .is_some()
            || envelope.payload.get("systemPayload").is_some()
            || envelope.payload.get("toolCallResult").is_some())
}

fn parsed_qwen_message(
    record_type: &str,
    record: &QwenRecord,
    fallback_timestamp: Option<&str>,
) -> Option<ParsedSessionMessage> {
    let message = record.message.as_ref()?;
    let content = render_qwen_parts(&message.parts)?;
    let tool_names = qwen_tool_names(&message.parts);
    let timestamp = non_empty_optional(record.timestamp.clone())
        .or_else(|| fallback_timestamp.map(str::to_string));

    Some(ParsedSessionMessage {
        role: qwen_message_role(record_type, message),
        timestamp,
        content,
        tool_names,
    })
}

fn qwen_message_role(record_type: &str, message: &QwenMessage) -> String {
    if record_type == "tool_result" || message.parts.iter().any(qwen_part_has_function_response) {
        return "tool result".to_string();
    }

    match message.role.as_deref().and_then(non_empty_string) {
        Some(role) if role == "model" => "assistant".to_string(),
        Some(role) => role,
        None if record_type == "assistant" => "assistant".to_string(),
        None if record_type == "user" => "user".to_string(),
        None => record_type.to_string(),
    }
}

fn render_qwen_parts(parts: &[Value]) -> Option<String> {
    let mut rendered = Vec::new();
    for part in parts {
        if let Some(rendered_part) = render_qwen_part(part) {
            rendered.push(rendered_part);
        }
    }
    non_empty_string(&rendered.join("\n\n"))
}

fn render_qwen_part(part: &Value) -> Option<String> {
    if part
        .get("thought")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return None;
    }

    if let Some(text) = json_string_field(part, "text") {
        return Some(text);
    }

    if let Some(function_call) = part.get("functionCall") {
        return render_qwen_function_call(function_call);
    }

    if let Some(function_response) = part.get("functionResponse") {
        return render_qwen_function_response(function_response);
    }

    match part {
        Value::Object(_) => Some(format!("```json\n{}\n```", pretty_json(part))),
        _ => None,
    }
}

fn render_qwen_function_call(function_call: &Value) -> Option<String> {
    let name = json_string_field(function_call, "name").unwrap_or_else(|| "tool".to_string());
    let mut rendered = format!("Tool use: {name}");
    if let Some(id) = json_string_field(function_call, "id") {
        rendered.push_str(": ");
        rendered.push_str(&id);
    }
    if let Some(args) = function_call.get("args").filter(|value| !value.is_null()) {
        rendered.push_str("\n\n```json\n");
        rendered.push_str(&pretty_json(args));
        rendered.push_str("\n```");
    }
    Some(rendered)
}

fn render_qwen_function_response(function_response: &Value) -> Option<String> {
    let mut rendered = "Tool result".to_string();
    if let Some(name) = json_string_field(function_response, "name") {
        rendered.push_str(": ");
        rendered.push_str(&name);
    }
    if let Some(id) = json_string_field(function_response, "id") {
        rendered.push_str(": ");
        rendered.push_str(&id);
    }
    if let Some(response) = function_response.get("response") {
        rendered.push_str("\n\n");
        rendered.push_str(&render_jsonish_or_text(response));
    }
    Some(rendered)
}

fn qwen_tool_names(parts: &[Value]) -> Vec<String> {
    parts
        .iter()
        .filter_map(|part| part.get("functionCall"))
        .filter_map(|function_call| json_string_field(function_call, "name"))
        .collect()
}

fn render_jsonish_or_text(value: &Value) -> String {
    if let Some(text) = value.as_str() {
        return text.to_string();
    }
    format!("```json\n{}\n```", pretty_json(value))
}

fn qwen_part_has_function_response(part: &Value) -> bool {
    part.get("functionResponse").is_some()
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::super::{default_session_adapters, parse_session_archive, read_session_archive};
    use super::*;

    #[test]
    fn qwen_adapter_parses_messages_tools_and_skips_thoughts() {
        let envelopes = vec![
            SessionArchiveEnvelope {
                envelope_type: "user".to_string(),
                timestamp: Some("2026-06-16T21:01:00Z".to_string()),
                payload: serde_json::json!({
                    "type": "user",
                    "uuid": "user-1",
                    "parentUuid": null,
                    "sessionId": "session-1",
                    "timestamp": "2026-06-16T21:01:00Z",
                    "cwd": "/workspace",
                    "version": "0.18.0",
                    "message": {"role": "user", "parts": [{"text": "Create cap_probe.txt."}]}
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "system".to_string(),
                timestamp: Some("2026-06-16T21:01:01Z".to_string()),
                payload: serde_json::json!({
                    "type": "system",
                    "subtype": "ui_telemetry",
                    "uuid": "system-1",
                    "parentUuid": "user-1",
                    "sessionId": "session-1",
                    "timestamp": "2026-06-16T21:01:01Z",
                    "cwd": "/workspace",
                    "version": "0.18.0",
                    "systemPayload": {"hidden": "telemetry"}
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "assistant".to_string(),
                timestamp: Some("2026-06-16T21:01:02Z".to_string()),
                payload: serde_json::json!({
                    "type": "assistant",
                    "uuid": "assistant-1",
                    "parentUuid": "system-1",
                    "sessionId": "session-1",
                    "timestamp": "2026-06-16T21:01:02Z",
                    "cwd": "/workspace",
                    "version": "0.18.0",
                    "model": "qwen/qwen3",
                    "message": {
                        "role": "model",
                        "parts": [
                            {"text": "private chain of thought", "thought": true},
                            {"text": "I will write the file."},
                            {
                                "functionCall": {
                                    "id": "call_1",
                                    "name": "write_file",
                                    "args": {"file_path": "cap_probe.txt", "content": "hello"}
                                }
                            }
                        ]
                    }
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "tool_result".to_string(),
                timestamp: Some("2026-06-16T21:01:03Z".to_string()),
                payload: serde_json::json!({
                    "type": "tool_result",
                    "uuid": "tool-result-1",
                    "parentUuid": "assistant-1",
                    "sessionId": "session-1",
                    "timestamp": "2026-06-16T21:01:03Z",
                    "cwd": "/workspace",
                    "version": "0.18.0",
                    "toolCallResult": {"callId": "call_1", "status": "cancelled"},
                    "message": {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "id": "call_1",
                                    "name": "write_file",
                                    "response": {"error": "[Operation Cancelled]"}
                                }
                            }
                        ]
                    }
                }),
            },
        ];
        let adapters = default_session_adapters();

        let parsed = parse_session_archive(&envelopes, &adapters).expect("parse Qwen fixture");

        assert_eq!(parsed.title, "Qwen session");
        assert_eq!(parsed.session_type, "qwen-code");
        assert_eq!(parsed.started_at.as_deref(), Some("2026-06-16T21:01:00Z"));
        assert_eq!(parsed.messages.len(), 3);
        assert_eq!(parsed.messages[0].role, "user");
        assert_eq!(parsed.messages[0].content, "Create cap_probe.txt.");
        assert_eq!(parsed.messages[1].role, "assistant");
        assert!(
            parsed.messages[1]
                .content
                .contains("I will write the file.")
        );
        assert!(
            parsed.messages[1]
                .content
                .contains("Tool use: write_file: call_1")
        );
        assert!(
            !parsed.messages[1]
                .content
                .contains("private chain of thought")
        );
        assert_eq!(parsed.messages[2].role, "tool result");
        assert!(
            parsed.messages[2]
                .content
                .contains("Tool result: write_file: call_1")
        );
        assert!(parsed.messages[2].content.contains("[Operation Cancelled]"));
        assert!(
            !parsed
                .messages
                .iter()
                .any(|message| message.content.contains("telemetry"))
        );
    }

    #[test]
    fn qwen_adapter_accepts_envelope_wrapped_payload_records() {
        let envelopes = vec![SessionArchiveEnvelope {
            envelope_type: "user".to_string(),
            timestamp: Some("2026-06-16T21:02:00Z".to_string()),
            payload: serde_json::json!({
                "type": "user",
                "uuid": "user-1",
                "parentUuid": null,
                "sessionId": "session-1",
                "timestamp": "2026-06-16T21:02:00Z",
                "cwd": "/workspace",
                "version": "0.18.0",
                "message": {"role": "user", "parts": [{"text": "Hello Qwen."}]}
            }),
        }];
        let adapters = default_session_adapters();

        let parsed = parse_session_archive(&envelopes, &adapters).expect("parse Qwen envelope");

        assert_eq!(parsed.session_type, "qwen-code");
        assert_eq!(parsed.messages.len(), 1);
        assert_eq!(parsed.messages[0].content, "Hello Qwen.");
    }

    #[test]
    fn qwen_adapter_parses_real_archive_when_fixture_is_set() {
        let Ok(path) = std::env::var("GWIKI_QWEN_ARCHIVE_FIXTURE") else {
            return;
        };
        let path = Path::new(&path);
        let bytes = std::fs::read(path).expect("read real Qwen archive fixture");
        let envelopes = read_session_archive(path, &bytes).expect("read real Qwen archive");
        let adapters = default_session_adapters();

        let parsed = parse_session_archive(&envelopes, &adapters).expect("parse real Qwen archive");

        assert_eq!(parsed.session_type, "qwen-code");
        assert!(!parsed.messages.is_empty());
        assert!(parsed.messages.iter().any(|message| message.role == "user"));
        assert!(
            parsed
                .messages
                .iter()
                .any(|message| message.role == "assistant" || message.role == "tool result")
        );
    }
}
