use std::path::{Path, PathBuf};

use serde::Deserialize;
use serde_json::Value;

use crate::WikiError;
use crate::ingest::{
    IngestResult, markdown_metadata, markdown_title, single_line, text_from_utf8_lossy,
    write_raw_markdown,
};
use crate::sources::{CompileStatus, IngestionMethod, SourceDraftRef, SourceKind, SourceManifest};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionFileSnapshot {
    pub location: String,
    pub file_name: String,
    pub fetched_at: String,
    pub path: PathBuf,
    pub bytes: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ParsedSession {
    pub title: String,
    pub session_type: String,
    pub started_at: Option<String>,
    pub messages: Vec<ParsedSessionMessage>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ParsedSessionMessage {
    pub role: String,
    pub timestamp: Option<String>,
    pub content: String,
}

#[derive(Debug, Clone, Deserialize)]
pub(crate) struct SessionArchiveEnvelope {
    pub payload: Value,
    pub timestamp: String,
    #[serde(rename = "type")]
    pub envelope_type: String,
}

pub(crate) trait SessionTranscriptAdapter {
    fn supports(&self, envelope_type: &str) -> bool;
    fn parse(&self, envelopes: &[SessionArchiveEnvelope]) -> Result<ParsedSession, WikiError>;
}

pub(crate) fn ingest_session_file_without_index(
    vault_root: &Path,
    snapshot: SessionFileSnapshot,
) -> Result<IngestResult, WikiError> {
    let envelopes = read_session_archive(&snapshot.path, &snapshot.bytes)?;
    let adapters = default_session_adapters();
    let parsed = parse_session_archive(&envelopes, &adapters)?;
    let title =
        non_empty_string(&parsed.title).unwrap_or_else(|| markdown_title(&snapshot.file_name));
    let record = SourceManifest::register_borrowed(
        vault_root,
        SourceDraftRef {
            location: snapshot.location.clone(),
            kind: SourceKind::Session,
            fetched_at: snapshot.fetched_at.clone(),
            content: &snapshot.bytes,
            title: Some(title.clone()),
            citation: Some(snapshot.location.clone()),
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
        },
    )?;
    let markdown = render_session_markdown(&snapshot, &parsed, &title, &record.content_hash);
    let raw_path = write_raw_markdown(vault_root, &record, &markdown)?;

    Ok(IngestResult {
        record,
        raw_path,
        asset_path: None,
    })
}

pub(crate) fn parse_session_archive(
    envelopes: &[SessionArchiveEnvelope],
    adapters: &[&dyn SessionTranscriptAdapter],
) -> Result<ParsedSession, WikiError> {
    let Some(adapter) = adapters.iter().copied().find(|adapter| {
        envelopes
            .iter()
            .any(|envelope| adapter.supports(&envelope.envelope_type))
    }) else {
        let types = envelopes
            .iter()
            .map(|envelope| envelope.envelope_type.as_str())
            .collect::<Vec<_>>()
            .join(", ");
        return Err(WikiError::InvalidInput {
            field: "session.type",
            message: format!("unsupported session transcript archive type(s): {types}"),
        });
    };

    adapter.parse(envelopes)
}

fn read_session_archive(
    path: &Path,
    bytes: &[u8],
) -> Result<Vec<SessionArchiveEnvelope>, WikiError> {
    let mut envelopes = Vec::new();
    for line in text_from_utf8_lossy(bytes).lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let envelope = serde_json::from_str::<SessionArchiveEnvelope>(line).map_err(|source| {
            WikiError::Json {
                action: "parse session archive envelope",
                path: Some(path.to_path_buf()),
                source,
            }
        })?;
        if envelope.envelope_type.trim().is_empty() {
            return Err(WikiError::InvalidInput {
                field: "session.type",
                message: "session archive envelope type must not be empty".to_string(),
            });
        }
        if envelope.timestamp.trim().is_empty() {
            return Err(WikiError::InvalidInput {
                field: "session.timestamp",
                message: "session archive envelope timestamp must not be empty".to_string(),
            });
        }
        envelopes.push(envelope);
    }

    if envelopes.is_empty() {
        return Err(WikiError::InvalidInput {
            field: "session",
            message: "session archive must contain at least one envelope".to_string(),
        });
    }

    Ok(envelopes)
}

fn default_session_adapters() -> [&'static dyn SessionTranscriptAdapter; 1] {
    [&COMMON_SESSION_ADAPTER]
}

static COMMON_SESSION_ADAPTER: CommonSessionAdapter = CommonSessionAdapter;

struct CommonSessionAdapter;

impl SessionTranscriptAdapter for CommonSessionAdapter {
    fn supports(&self, envelope_type: &str) -> bool {
        matches!(
            envelope_type,
            "session" | "gobby.session" | "gobby.session.v1"
        )
    }

    fn parse(&self, envelopes: &[SessionArchiveEnvelope]) -> Result<ParsedSession, WikiError> {
        let mut title = None;
        let mut started_at = None;
        let mut messages = Vec::new();
        let mut session_type = None;

        for envelope in envelopes
            .iter()
            .filter(|envelope| self.supports(&envelope.envelope_type))
        {
            let payload = serde_json::from_value::<CommonSessionPayload>(envelope.payload.clone())
                .map_err(|source| WikiError::Json {
                    action: "parse common session payload",
                    path: None,
                    source,
                })?;
            title = title.or_else(|| payload.title.as_deref().and_then(non_empty_string));
            started_at = started_at.or_else(|| Some(envelope.timestamp.clone()));
            session_type = session_type.or_else(|| Some(envelope.envelope_type.clone()));

            if let Some(message) = parsed_common_payload_message(&payload, &envelope.timestamp) {
                messages.push(message);
            }

            for message in payload.messages {
                if let Some(message) = parsed_common_message(message, Some(&envelope.timestamp)) {
                    messages.push(message);
                }
            }
        }

        if messages.is_empty() {
            return Err(WikiError::InvalidInput {
                field: "session.payload",
                message: "common session payload must contain at least one message".to_string(),
            });
        }

        Ok(ParsedSession {
            title: title.unwrap_or_else(|| "Session transcript".to_string()),
            session_type: session_type.unwrap_or_else(|| "session".to_string()),
            started_at,
            messages,
        })
    }
}

#[derive(Debug, Clone, Deserialize)]
struct CommonSessionPayload {
    title: Option<String>,
    role: Option<String>,
    content: Option<String>,
    #[serde(default)]
    messages: Vec<CommonSessionMessage>,
}

#[derive(Debug, Clone, Deserialize)]
struct CommonSessionMessage {
    role: Option<String>,
    timestamp: Option<String>,
    content: String,
}

fn parsed_common_message(
    message: CommonSessionMessage,
    fallback_timestamp: Option<&str>,
) -> Option<ParsedSessionMessage> {
    let content = non_empty_string(&message.content)?;
    Some(ParsedSessionMessage {
        role: non_empty_optional(message.role).unwrap_or_else(|| "speaker".to_string()),
        timestamp: non_empty_optional(message.timestamp)
            .or_else(|| fallback_timestamp.map(str::to_string)),
        content,
    })
}

fn parsed_common_payload_message(
    payload: &CommonSessionPayload,
    fallback_timestamp: &str,
) -> Option<ParsedSessionMessage> {
    let content = non_empty_optional(payload.content.clone())?;
    Some(ParsedSessionMessage {
        role: non_empty_optional(payload.role.clone()).unwrap_or_else(|| "speaker".to_string()),
        timestamp: Some(fallback_timestamp.to_string()),
        content,
    })
}

fn render_session_markdown(
    snapshot: &SessionFileSnapshot,
    parsed: &ParsedSession,
    title: &str,
    source_hash: &str,
) -> String {
    let mut fields = vec![
        ("source_kind", SourceKind::Session.to_string()),
        ("source_location", snapshot.location.clone()),
        ("fetched_at", snapshot.fetched_at.clone()),
        ("source_hash", source_hash.to_string()),
        ("session_type", parsed.session_type.clone()),
    ];
    if let Some(started_at) = &parsed.started_at {
        fields.push(("session_started_at", started_at.clone()));
    }

    let mut markdown = markdown_metadata(&fields);
    markdown.push_str("# ");
    markdown.push_str(title);
    markdown.push_str("\n\n");
    markdown.push_str("Session transcript extracted from `");
    markdown.push_str(&snapshot.location);
    markdown.push_str("`.\n\n");
    markdown.push_str("## Messages\n\n");

    for message in &parsed.messages {
        markdown.push_str("### ");
        markdown.push_str(&message_heading(&message.role));
        markdown.push_str("\n\n");
        if let Some(timestamp) = &message.timestamp {
            markdown.push('_');
            markdown.push_str(&single_line(timestamp));
            markdown.push_str("_\n\n");
        }
        markdown.push_str(&message.content);
        if !message.content.ends_with('\n') {
            markdown.push('\n');
        }
        markdown.push('\n');
    }

    markdown
}

fn message_heading(role: &str) -> String {
    non_empty_string(&markdown_title(role)).unwrap_or_else(|| "Speaker".to_string())
}

fn non_empty_optional(value: Option<String>) -> Option<String> {
    value.and_then(|value| non_empty_string(&value))
}

fn non_empty_string(value: &str) -> Option<String> {
    let value = value.trim();
    (!value.is_empty()).then(|| value.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn common_session_adapter_accepts_fixture_payload_messages() {
        let envelopes = vec![SessionArchiveEnvelope {
            envelope_type: "session".to_string(),
            timestamp: "2026-06-16T20:00:00Z".to_string(),
            payload: serde_json::json!({
                "title": "Fixture import",
                "messages": [
                    {"role": "user", "content": "Can you summarize this?"},
                    {"role": "assistant", "timestamp": "2026-06-16T20:00:05Z", "content": "Yes."}
                ]
            }),
        }];

        let adapters = default_session_adapters();
        let parsed = parse_session_archive(&envelopes, &adapters).expect("parse session fixture");

        assert_eq!(parsed.title, "Fixture import");
        assert_eq!(parsed.session_type, "session");
        assert_eq!(parsed.started_at.as_deref(), Some("2026-06-16T20:00:00Z"));
        assert_eq!(parsed.messages.len(), 2);
        assert_eq!(parsed.messages[0].role, "user");
        assert_eq!(parsed.messages[0].content, "Can you summarize this?");
        assert_eq!(
            parsed.messages[1].timestamp.as_deref(),
            Some("2026-06-16T20:00:05Z")
        );
    }
}
