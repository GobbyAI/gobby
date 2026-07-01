use serde::Deserialize;
use serde_json::Value;

use super::{
    ParsedSession, ParsedSessionMessage, ParsedSessionMetadata, SessionArchiveEnvelope,
    SessionTranscriptAdapter, json_string_field, non_empty_optional, non_empty_string, pretty_json,
};
use crate::WikiError;

pub(super) static CODEX_SESSION_ADAPTER: CodexSessionAdapter = CodexSessionAdapter;

pub(super) struct CodexSessionAdapter;

impl SessionTranscriptAdapter for CodexSessionAdapter {
    fn supports(&self, envelope_type: &str) -> bool {
        matches!(
            envelope_type,
            "event_msg" | "response_item" | "session_meta" | "turn_context"
        )
    }

    fn parse(&self, envelopes: &[SessionArchiveEnvelope]) -> Result<ParsedSession, WikiError> {
        let mut started_at = None;
        let mut session_type = None;
        let mut metadata = ParsedSessionMetadata::default();
        let mut messages = Vec::new();

        for envelope in envelopes
            .iter()
            .filter(|envelope| self.supports(&envelope.envelope_type))
        {
            started_at = started_at.or_else(|| envelope.timestamp.clone());

            match envelope.envelope_type.as_str() {
                "session_meta" => {
                    let meta = serde_json::from_value::<CodexSessionMeta>(envelope.payload.clone())
                        .map_err(|source| WikiError::Json {
                            action: "parse Codex session metadata",
                            path: None,
                            source,
                        })?;
                    started_at = started_at.or_else(|| non_empty_optional(meta.timestamp.clone()));
                    session_type = session_type.or_else(|| {
                        meta.originator
                            .as_deref()
                            .and_then(non_empty_string)
                            .filter(|originator| originator.contains("codex"))
                    });
                    metadata.set_model_once(meta.model.as_deref());
                    metadata.set_git_branch_once(
                        meta.git.as_ref().and_then(|git| git.branch.as_deref()),
                    );
                }
                "turn_context" => {
                    metadata
                        .set_model_once(json_string_field(&envelope.payload, "model").as_deref());
                    metadata.set_git_branch_once(
                        envelope
                            .payload
                            .pointer("/git/branch")
                            .and_then(Value::as_str),
                    );
                }
                "event_msg" => {
                    if let Some(token_totals) = envelope.payload.pointer("/info/total_token_usage")
                    {
                        metadata.set_token_totals(token_totals);
                    }
                    if let Some(message) = parsed_codex_event_message(envelope)? {
                        messages.push(message);
                    }
                }
                "response_item" => {
                    if let Some(message) = parsed_codex_response_item(envelope)? {
                        messages.push(message);
                    }
                }
                _ => {}
            }
        }

        if messages.is_empty() {
            return Err(WikiError::InvalidInput {
                field: "session.payload",
                message: "Codex session archive must contain at least one message".to_string(),
            });
        }

        Ok(ParsedSession {
            title: "Codex session".to_string(),
            session_type: session_type.unwrap_or_else(|| "codex-tui".to_string()),
            started_at,
            metadata,
            messages,
        })
    }
}

#[derive(Debug, Clone, Deserialize)]
struct CodexSessionMeta {
    timestamp: Option<String>,
    originator: Option<String>,
    model: Option<String>,
    git: Option<CodexGitMetadata>,
}

#[derive(Debug, Clone, Deserialize)]
struct CodexGitMetadata {
    branch: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
struct CodexEventPayload {
    #[serde(rename = "type")]
    event_type: String,
    message: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
struct CodexResponseItem {
    #[serde(rename = "type")]
    item_type: String,
    role: Option<String>,
    phase: Option<String>,
    content: Option<Value>,
    name: Option<String>,
    arguments: Option<Value>,
    call_id: Option<String>,
    output: Option<Value>,
    tools: Option<Value>,
}

fn parsed_codex_event_message(
    envelope: &SessionArchiveEnvelope,
) -> Result<Option<ParsedSessionMessage>, WikiError> {
    let payload = serde_json::from_value::<CodexEventPayload>(envelope.payload.clone()).map_err(
        |source| WikiError::Json {
            action: "parse Codex event message",
            path: None,
            source,
        },
    )?;

    if payload.event_type != "user_message" {
        return Ok(None);
    }

    Ok(
        non_empty_optional(payload.message).map(|content| ParsedSessionMessage {
            role: "user".to_string(),
            timestamp: envelope.timestamp.clone(),
            content,
            tool_names: Vec::new(),
        }),
    )
}

fn parsed_codex_response_item(
    envelope: &SessionArchiveEnvelope,
) -> Result<Option<ParsedSessionMessage>, WikiError> {
    let item = serde_json::from_value::<CodexResponseItem>(envelope.payload.clone()).map_err(
        |source| WikiError::Json {
            action: "parse Codex response item",
            path: None,
            source,
        },
    )?;

    let timestamp = envelope.timestamp.clone();
    let message = match item.item_type.as_str() {
        "message" => parsed_codex_response_message(item, timestamp),
        "function_call" => parsed_codex_function_call(item, timestamp),
        "function_call_output" => parsed_codex_function_output(item, timestamp),
        "tool_search_call" => parsed_codex_tool_search_call(item, timestamp),
        "tool_search_output" => parsed_codex_tool_search_output(item, timestamp),
        _ => None,
    };

    Ok(message)
}

fn parsed_codex_response_message(
    item: CodexResponseItem,
    timestamp: Option<String>,
) -> Option<ParsedSessionMessage> {
    if item.role.as_deref() != Some("assistant") {
        return None;
    }

    let content = render_codex_content(item.content.as_ref()?)?;
    let mut role = "assistant".to_string();
    if let Some(phase) = non_empty_optional(item.phase) {
        role.push_str(" (");
        role.push_str(&phase);
        role.push(')');
    }

    Some(ParsedSessionMessage {
        role,
        timestamp,
        content,
        tool_names: Vec::new(),
    })
}

fn parsed_codex_function_call(
    item: CodexResponseItem,
    timestamp: Option<String>,
) -> Option<ParsedSessionMessage> {
    let name = non_empty_optional(item.name).unwrap_or_else(|| "tool".to_string());
    let tool_name = name.clone();
    let mut content = format!("Function call: {name}");
    append_call_id(&mut content, item.call_id.as_deref());
    if let Some(arguments) = item.arguments.as_ref().filter(|value| !value.is_null()) {
        content.push_str("\n\nArguments\n\n```json\n");
        content.push_str(&pretty_jsonish(arguments));
        content.push_str("\n```");
    }

    Some(ParsedSessionMessage {
        role: "tool call".to_string(),
        timestamp,
        content,
        tool_names: vec![tool_name],
    })
}

fn parsed_codex_function_output(
    item: CodexResponseItem,
    timestamp: Option<String>,
) -> Option<ParsedSessionMessage> {
    let mut content = "Function output".to_string();
    append_call_id(&mut content, item.call_id.as_deref());
    append_value_body(&mut content, item.output.as_ref())?;

    Some(ParsedSessionMessage {
        role: "tool result".to_string(),
        timestamp,
        content,
        tool_names: Vec::new(),
    })
}

fn parsed_codex_tool_search_call(
    item: CodexResponseItem,
    timestamp: Option<String>,
) -> Option<ParsedSessionMessage> {
    let mut content = "Tool search call".to_string();
    append_call_id(&mut content, item.call_id.as_deref());
    append_value_body(&mut content, item.arguments.as_ref())?;

    Some(ParsedSessionMessage {
        role: "tool call".to_string(),
        timestamp,
        content,
        tool_names: vec!["tool_search".to_string()],
    })
}

fn parsed_codex_tool_search_output(
    item: CodexResponseItem,
    timestamp: Option<String>,
) -> Option<ParsedSessionMessage> {
    let mut content = "Tool search output".to_string();
    append_call_id(&mut content, item.call_id.as_deref());
    append_value_body(&mut content, item.tools.as_ref())?;

    Some(ParsedSessionMessage {
        role: "tool result".to_string(),
        timestamp,
        content,
        tool_names: Vec::new(),
    })
}

fn render_codex_content(content: &Value) -> Option<String> {
    let mut parts = Vec::new();
    append_codex_content(content, &mut parts);
    non_empty_string(&parts.join("\n\n"))
}

fn append_codex_content(value: &Value, parts: &mut Vec<String>) {
    match value {
        Value::String(text) => {
            if let Some(text) = non_empty_string(text) {
                parts.push(text);
            }
        }
        Value::Array(items) => {
            for item in items {
                if let Some(part) = render_codex_content_block(item) {
                    parts.push(part);
                }
            }
        }
        Value::Object(_) => {
            if let Some(part) = render_codex_content_block(value) {
                parts.push(part);
            }
        }
        _ => {}
    }
}

fn render_codex_content_block(block: &Value) -> Option<String> {
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

fn append_value_body(content: &mut String, value: Option<&Value>) -> Option<()> {
    let value = value.filter(|value| !value.is_null())?;
    content.push_str("\n\n");
    content.push_str(&render_jsonish_or_text(value));
    Some(())
}

fn render_jsonish_or_text(value: &Value) -> String {
    if let Some(text) = value.as_str() {
        return text.to_string();
    }
    format!("```json\n{}\n```", pretty_json(value))
}

fn pretty_jsonish(value: &Value) -> String {
    if let Some(text) = value.as_str() {
        return serde_json::from_str::<Value>(text)
            .map(|value| pretty_json(&value))
            .unwrap_or_else(|_| text.to_string());
    }
    pretty_json(value)
}

#[cfg(test)]
mod tests {
    use std::path::{Path, PathBuf};

    use super::super::{
        SessionFileSnapshot, default_session_adapters, parse_session_archive, read_session_archive,
        render_session_markdown,
    };
    use super::*;

    #[test]
    fn codex_adapter_parses_messages_and_tool_items() {
        let envelopes = vec![
            SessionArchiveEnvelope {
                envelope_type: "session_meta".to_string(),
                timestamp: Some("2026-06-16T20:00:00Z".to_string()),
                payload: serde_json::json!({
                    "originator": "codex-tui",
                    "timestamp": "2026-06-16T20:00:00Z",
                    "git": {"branch": "dev"}
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "turn_context".to_string(),
                timestamp: Some("2026-06-16T20:00:00Z".to_string()),
                payload: serde_json::json!({
                    "model": "gpt-5.5"
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "event_msg".to_string(),
                timestamp: Some("2026-06-16T20:00:01Z".to_string()),
                payload: serde_json::json!({
                    "type": "user_message",
                    "message": "Explain the code path.",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 100,
                            "output_tokens": 7,
                            "total_tokens": 107
                        }
                    }
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "response_item".to_string(),
                timestamp: Some("2026-06-16T20:00:02Z".to_string()),
                payload: serde_json::json!({
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "I will trace it."}]
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "response_item".to_string(),
                timestamp: Some("2026-06-16T20:00:03Z".to_string()),
                payload: serde_json::json!({
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call_1",
                    "arguments": "{\"cmd\":\"pwd\"}"
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "response_item".to_string(),
                timestamp: Some("2026-06-16T20:00:04Z".to_string()),
                payload: serde_json::json!({
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "Output:\n/workspace\n"
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "response_item".to_string(),
                timestamp: Some("2026-06-16T20:00:05Z".to_string()),
                payload: serde_json::json!({
                    "type": "tool_search_call",
                    "call_id": "call_2",
                    "arguments": {"query": "list_mcp_servers", "limit": 5}
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "response_item".to_string(),
                timestamp: Some("2026-06-16T20:00:06Z".to_string()),
                payload: serde_json::json!({
                    "type": "tool_search_output",
                    "call_id": "call_2",
                    "tools": []
                }),
            },
        ];
        let adapters = default_session_adapters();

        let parsed = parse_session_archive(&envelopes, &adapters).expect("parse Codex fixture");

        assert_eq!(parsed.title, "Codex session");
        assert_eq!(parsed.session_type, "codex-tui");
        assert_eq!(parsed.started_at.as_deref(), Some("2026-06-16T20:00:00Z"));
        assert_eq!(parsed.metadata.model.as_deref(), Some("gpt-5.5"));
        assert_eq!(parsed.metadata.git_branch.as_deref(), Some("dev"));
        assert_eq!(parsed.metadata.token_totals.get("input_tokens"), Some(&100));
        assert_eq!(parsed.metadata.token_totals.get("output_tokens"), Some(&7));
        assert_eq!(parsed.metadata.token_totals.get("total_tokens"), Some(&107));
        assert_eq!(parsed.messages.len(), 6);
        assert_eq!(parsed.messages[0].role, "user");
        assert_eq!(parsed.messages[0].content, "Explain the code path.");
        assert_eq!(parsed.messages[1].role, "assistant (commentary)");
        assert_eq!(parsed.messages[1].content, "I will trace it.");
        assert_eq!(parsed.messages[2].role, "tool call");
        assert_eq!(parsed.messages[2].tool_names, vec!["exec_command"]);
        assert!(
            parsed.messages[2]
                .content
                .contains("Function call: exec_command")
        );
        assert!(parsed.messages[2].content.contains("\"cmd\": \"pwd\""));
        assert_eq!(parsed.messages[3].role, "tool result");
        assert!(parsed.messages[3].content.contains("/workspace"));
        assert!(
            parsed.messages[4]
                .content
                .contains("Tool search call: call_2")
        );
        assert_eq!(parsed.messages[4].tool_names, vec!["tool_search"]);
        assert!(
            parsed.messages[5]
                .content
                .contains("Tool search output: call_2")
        );
    }

    #[test]
    fn codex_adapter_parses_real_archive_when_fixture_is_set() {
        let Ok(path) = std::env::var("GWIKI_CODEX_ARCHIVE_FIXTURE") else {
            return;
        };
        let bytes = std::fs::read(&path).expect("read real Codex archive fixture");
        let envelopes = read_session_archive(Path::new(&path), &bytes).expect("read Codex archive");
        let adapters = default_session_adapters();

        let parsed =
            parse_session_archive(&envelopes, &adapters).expect("parse real Codex archive");

        assert_eq!(parsed.session_type, "codex-tui");
        assert!(parsed.messages.iter().any(|message| message.role == "user"));
        assert!(
            parsed
                .messages
                .iter()
                .any(|message| message.role.starts_with("assistant")),
            "expected at least one assistant message"
        );
        assert!(
            parsed
                .messages
                .iter()
                .any(|message| message.role == "tool call"),
            "expected at least one parsed tool call"
        );
        assert!(
            parsed
                .messages
                .iter()
                .any(|message| message.role == "tool result"),
            "expected at least one parsed tool result"
        );
        assert!(parsed.metadata.model.is_some(), "expected model metadata");
        assert!(
            parsed.metadata.git_branch.is_some(),
            "expected gitBranch metadata"
        );
        assert!(
            !parsed.metadata.token_totals.is_empty(),
            "expected token totals metadata"
        );
        assert!(
            parsed
                .messages
                .iter()
                .any(|message| !message.tool_names.is_empty()),
            "expected tool name metadata"
        );

        let snapshot = SessionFileSnapshot {
            location: path.clone(),
            file_name: "codex-real.jsonl".to_string(),
            fetched_at: "2026-06-16T00:00:00Z".to_string(),
            path: PathBuf::from(&path),
            bytes,
        };
        let markdown = render_session_markdown(&snapshot, &parsed, &parsed.title, "fixture-hash");

        assert!(markdown.contains("model: "));
        assert!(markdown.contains("tool_counts: {"));
        assert!(markdown.contains("token_totals: {"));
        assert!(markdown.contains("duration_seconds: "));
        assert!(markdown.contains("hour_buckets: {"));
        assert!(markdown.contains("is_subagent: false\n"));
        assert!(markdown.contains("gitBranch: "));
    }
}
