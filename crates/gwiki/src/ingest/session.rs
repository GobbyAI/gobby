use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use serde::Deserialize;
use serde_json::Value;

mod codex;
mod daemon_wiki;
mod derived;
mod droid;
mod grok;
mod metadata;
mod qwen;
mod redaction;
mod summarize;

use codex::CODEX_SESSION_ADAPTER;
use daemon_wiki::{DaemonWikiPage, render_session_wiki_markdown};
use derived::write_session_derived_markdown;
use droid::DROID_SESSION_ADAPTER;
use grok::GROK_SESSION_ADAPTER;
pub(crate) use metadata::ParsedSessionMetadata;
use metadata::session_metadata_fields;
use qwen::QWEN_SESSION_ADAPTER;
use redaction::{redact_session_markdown, redact_session_text};
pub(crate) use summarize::SessionSummarizer;

use crate::WikiError;
use crate::ingest::{
    IngestResult, MetadataValue, markdown_metadata_values, markdown_title, path_to_string,
    single_line, text_from_utf8_lossy, write_raw_markdown,
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

/// A daemon-synthesized session wiki `.md` file pulled from the wiki directory.
///
/// Unlike [`SessionFileSnapshot`] (raw transcript archives), this carries the
/// already-synthesized knowledge page. Ingest parses and strips the daemon
/// frontmatter, then rewrites a gwiki-owned page keyed on the canonical
/// `session:{external_id}` location shared with the raw fallback so a fresh
/// synthesis can supersede a previously raw-parsed page.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionWikiFileSnapshot {
    /// Canonical session external id (the `.md` filename stem).
    pub external_id: String,
    /// Original daemon mirror path (e.g. `~/.gobby/session_wiki/{external_id}.md`).
    pub path: PathBuf,
    /// Timestamp this snapshot was read.
    pub fetched_at: String,
    /// Raw bytes of the daemon `.md` file (frontmatter + synthesized body).
    pub bytes: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ParsedSession {
    pub title: String,
    pub session_type: String,
    pub started_at: Option<String>,
    pub metadata: ParsedSessionMetadata,
    pub messages: Vec<ParsedSessionMessage>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ParsedSessionMessage {
    pub role: String,
    pub timestamp: Option<String>,
    pub content: String,
    pub tool_names: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub(crate) struct SessionArchiveEnvelope {
    pub payload: Value,
    pub timestamp: Option<String>,
    #[serde(rename = "type")]
    pub envelope_type: String,
}

pub(crate) trait SessionTranscriptAdapter {
    fn supports(&self, envelope_type: &str) -> bool;

    fn supports_archive(&self, envelopes: &[SessionArchiveEnvelope]) -> bool {
        envelopes
            .iter()
            .any(|envelope| self.supports(&envelope.envelope_type))
    }

    fn parse(&self, envelopes: &[SessionArchiveEnvelope]) -> Result<ParsedSession, WikiError>;
}

pub(crate) fn ingest_session_file_without_index(
    vault_root: &Path,
    snapshot: SessionFileSnapshot,
) -> Result<IngestResult, WikiError> {
    let parsed = parse_session_archive_bytes(&snapshot.path, &snapshot.bytes)?;
    let title =
        non_empty_string(&parsed.title).unwrap_or_else(|| markdown_title(&snapshot.file_name));
    let source_location = redact_session_text(&snapshot.location);
    let source_title = redact_session_text(&title);
    let record = SourceManifest::register_borrowed(
        vault_root,
        SourceDraftRef {
            location: source_location.clone(),
            kind: SourceKind::Session,
            fetched_at: snapshot.fetched_at.clone(),
            content: &snapshot.bytes,
            title: Some(source_title),
            citation: Some(source_location),
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
        },
    )?;
    let markdown = render_session_markdown(&snapshot, &parsed, &title, &record.content_hash);
    let markdown = redact_session_markdown(&markdown);
    let raw_path = write_raw_markdown(vault_root, &record, &markdown)?;
    write_session_derived_markdown(vault_root, &record, &markdown)?;

    Ok(IngestResult {
        record,
        raw_path,
        asset_path: None,
    })
}

/// Ingest a daemon-synthesized session wiki page into the vault.
///
/// Parallel to [`ingest_session_file_without_index`] but sourced from the
/// daemon's `session_wiki/{external_id}.md` synthesis rather than a raw
/// transcript archive: the daemon frontmatter is parsed and stripped, the
/// session is registered under the canonical `session:{external_id}` location,
/// and a single gwiki-owned frontmatter block plus the synthesized body is
/// written to `knowledge/sources/{id}.md`. The body is re-redacted defensively
/// before write — the synthesis can lift secrets from the digest verbatim.
pub(crate) fn ingest_session_wiki_file_without_index(
    vault_root: &Path,
    snapshot: SessionWikiFileSnapshot,
) -> Result<IngestResult, WikiError> {
    let text = text_from_utf8_lossy(&snapshot.bytes);
    let page = DaemonWikiPage::parse(&text);

    // Canonical, content-stable session location shared with the raw fallback,
    // so a fresh synthesis can supersede a previously raw-parsed page.
    let location = format!("session:{}", snapshot.external_id);
    let title = page
        .field("title")
        .map(|value| single_line(value))
        .and_then(|value| non_empty_string(&value))
        .unwrap_or_else(|| format!("Session {}", snapshot.external_id));
    let source_title = redact_session_text(&title);

    let record = SourceManifest::register_borrowed(
        vault_root,
        SourceDraftRef {
            location: location.clone(),
            kind: SourceKind::Session,
            fetched_at: snapshot.fetched_at.clone(),
            content: &snapshot.bytes,
            title: Some(source_title.clone()),
            citation: Some(location.clone()),
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
        },
    )?;

    let markdown = render_session_wiki_markdown(
        &snapshot,
        &page,
        &location,
        &source_title,
        &record.content_hash,
    );
    let markdown = redact_session_markdown(&markdown);
    let raw_path = write_raw_markdown(vault_root, &record, &markdown)?;
    write_session_derived_markdown(vault_root, &record, &markdown)?;

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
    let Some(adapter) = adapters
        .iter()
        .copied()
        .find(|adapter| adapter.supports_archive(envelopes))
    else {
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

/// Decode a raw `.jsonl.gz` archive's bytes into a [`ParsedSession`] using the
/// default per-CLI adapters. Shared by the skeleton ingest path and the
/// standalone `--summarize` generator.
pub(crate) fn parse_session_archive_bytes(
    path: &Path,
    bytes: &[u8],
) -> Result<ParsedSession, WikiError> {
    let envelopes = read_session_archive(path, bytes)?;
    let adapters = default_session_adapters();
    parse_session_archive(&envelopes, &adapters)
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
        let value = serde_json::from_str::<Value>(line).map_err(|source| WikiError::Json {
            action: "parse session archive envelope",
            path: Some(path.to_path_buf()),
            source,
        })?;
        let envelope = normalize_session_archive_value(value)?;
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

fn normalize_session_archive_value(value: Value) -> Result<SessionArchiveEnvelope, WikiError> {
    if !value.is_object() {
        return Err(WikiError::InvalidInput {
            field: "session",
            message: "session archive line must be a JSON object".to_string(),
        });
    }

    let envelope_type =
        json_string_field(&value, "type").ok_or_else(|| WikiError::InvalidInput {
            field: "session.type",
            message: "session archive envelope type must not be empty".to_string(),
        })?;
    let timestamp = json_string_field(&value, "timestamp");

    if let Some(payload) = value.get("payload").cloned() {
        return Ok(SessionArchiveEnvelope {
            payload,
            timestamp,
            envelope_type,
        });
    }

    Ok(SessionArchiveEnvelope {
        payload: value,
        timestamp,
        envelope_type,
    })
}

fn default_session_adapters() -> [&'static dyn SessionTranscriptAdapter; 6] {
    [
        &COMMON_SESSION_ADAPTER,
        &GROK_SESSION_ADAPTER,
        &DROID_SESSION_ADAPTER,
        &QWEN_SESSION_ADAPTER,
        &CLAUDE_CODE_ADAPTER,
        &CODEX_SESSION_ADAPTER,
    ]
}

static COMMON_SESSION_ADAPTER: CommonSessionAdapter = CommonSessionAdapter;
static CLAUDE_CODE_ADAPTER: ClaudeCodeAdapter = ClaudeCodeAdapter;

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
            started_at = started_at.or_else(|| envelope.timestamp.clone());
            session_type = session_type.or_else(|| Some(envelope.envelope_type.clone()));

            if let Some(message) =
                parsed_common_payload_message(&payload, envelope.timestamp.as_deref())
            {
                messages.push(message);
            }

            for message in payload.messages {
                if let Some(message) = parsed_common_message(message, envelope.timestamp.as_deref())
                {
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
            metadata: ParsedSessionMetadata::default(),
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
        tool_names: Vec::new(),
    })
}

fn parsed_common_payload_message(
    payload: &CommonSessionPayload,
    fallback_timestamp: Option<&str>,
) -> Option<ParsedSessionMessage> {
    let content = non_empty_optional(payload.content.clone())?;
    Some(ParsedSessionMessage {
        role: non_empty_optional(payload.role.clone()).unwrap_or_else(|| "speaker".to_string()),
        timestamp: fallback_timestamp.map(str::to_string),
        content,
        tool_names: Vec::new(),
    })
}

struct ClaudeCodeAdapter;

impl SessionTranscriptAdapter for ClaudeCodeAdapter {
    fn supports(&self, envelope_type: &str) -> bool {
        matches!(
            envelope_type,
            "ai-title"
                | "assistant"
                | "attachment"
                | "last-prompt"
                | "mode"
                | "permission-mode"
                | "queue-operation"
                | "system"
                | "user"
        )
    }

    fn parse(&self, envelopes: &[SessionArchiveEnvelope]) -> Result<ParsedSession, WikiError> {
        let mut title = None;
        let mut started_at = None;
        let mut metadata = ParsedSessionMetadata::default();
        let mut token_usage_message_ids = BTreeSet::new();
        let mut messages = Vec::new();

        for envelope in envelopes
            .iter()
            .filter(|envelope| self.supports(&envelope.envelope_type))
        {
            let record = serde_json::from_value::<ClaudeCodeRecord>(envelope.payload.clone())
                .map_err(|source| WikiError::Json {
                    action: "parse Claude Code session record",
                    path: None,
                    source,
                })?;
            title = title.or_else(|| record.ai_title.as_deref().and_then(non_empty_string));
            title = title.or_else(|| {
                record
                    .last_prompt
                    .as_deref()
                    .and_then(non_empty_string)
                    .map(|prompt| markdown_title(&prompt))
            });
            started_at = started_at.or_else(|| {
                non_empty_optional(record.timestamp.clone()).or_else(|| envelope.timestamp.clone())
            });
            metadata.set_git_branch_once(record.git_branch.as_deref());
            if record.is_sidechain.unwrap_or(false) {
                metadata.mark_subagent();
            }
            if let Some(message) = &record.message {
                metadata.set_model_once(message.model.as_deref());
                if let Some(usage) = &message.usage {
                    if let Some(id) = message.id.as_deref().and_then(non_empty_string) {
                        if token_usage_message_ids.insert(id) {
                            metadata.add_token_usage(usage);
                        }
                    } else {
                        metadata.add_token_usage(usage);
                    }
                }
            }

            if let Some(message) =
                parsed_claude_code_message(&record, envelope.timestamp.as_deref())
            {
                messages.push(message);
            }
        }

        if messages.is_empty() {
            return Err(WikiError::InvalidInput {
                field: "session.payload",
                message: "Claude Code session archive must contain at least one message"
                    .to_string(),
            });
        }

        Ok(ParsedSession {
            title: title.unwrap_or_else(|| "Claude Code session".to_string()),
            session_type: "claude-code".to_string(),
            started_at,
            metadata,
            messages,
        })
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ClaudeCodeRecord {
    #[serde(rename = "type")]
    record_type: String,
    timestamp: Option<String>,
    is_sidechain: Option<bool>,
    message: Option<ClaudeCodeMessage>,
    tool_use_result: Option<Value>,
    ai_title: Option<String>,
    last_prompt: Option<String>,
    git_branch: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
struct ClaudeCodeMessage {
    id: Option<String>,
    model: Option<String>,
    role: Option<String>,
    content: Option<Value>,
    usage: Option<Value>,
}

fn parsed_claude_code_message(
    record: &ClaudeCodeRecord,
    fallback_timestamp: Option<&str>,
) -> Option<ParsedSessionMessage> {
    let message = record.message.as_ref()?;
    let content = message.content.as_ref()?;
    let tool_names = claude_code_tool_names(content);
    let content = render_claude_code_content(content, record.tool_use_result.as_ref())?;
    let timestamp = non_empty_optional(record.timestamp.clone())
        .or_else(|| fallback_timestamp.map(str::to_string));

    Some(ParsedSessionMessage {
        role: claude_code_message_role(record, message),
        timestamp,
        content,
        tool_names,
    })
}

fn claude_code_message_role(record: &ClaudeCodeRecord, message: &ClaudeCodeMessage) -> String {
    let mut role = if record_has_tool_result(record) {
        "tool result".to_string()
    } else {
        non_empty_optional(message.role.clone()).unwrap_or_else(|| record.record_type.clone())
    };

    if record.is_sidechain.unwrap_or(false) && !role.to_lowercase().contains("sidechain") {
        role.push_str(" (sidechain)");
    }

    role
}

fn record_has_tool_result(record: &ClaudeCodeRecord) -> bool {
    record
        .tool_use_result
        .as_ref()
        .is_some_and(|value| !value.is_null())
        || record
            .message
            .as_ref()
            .and_then(|message| message.content.as_ref())
            .is_some_and(|content| content_has_block_type(content, "tool_result"))
}

fn claude_code_tool_names(content: &Value) -> Vec<String> {
    let mut names = Vec::new();
    collect_claude_code_tool_names(content, &mut names);
    names
}

fn collect_claude_code_tool_names(value: &Value, names: &mut Vec<String>) {
    match value {
        Value::Array(items) => {
            for item in items {
                collect_claude_code_tool_names(item, names);
            }
        }
        Value::Object(_) => {
            if value
                .get("type")
                .and_then(Value::as_str)
                .is_some_and(|content_type| content_type == "tool_use")
                && let Some(name) = json_string_field(value, "name")
            {
                names.push(name);
            }
        }
        _ => {}
    }
}

fn render_claude_code_content(content: &Value, tool_use_result: Option<&Value>) -> Option<String> {
    let mut parts = Vec::new();
    append_claude_code_content(content, &mut parts);

    if let Some(tool_use_result) = tool_use_result
        .filter(|value| !value.is_null() && !content_has_block_type(content, "tool_result"))
    {
        parts.push(format!(
            "Tool use result\n\n```json\n{}\n```",
            pretty_json(tool_use_result)
        ));
    }

    non_empty_string(&parts.join("\n\n"))
}

fn append_claude_code_content(value: &Value, parts: &mut Vec<String>) {
    match value {
        Value::String(text) => {
            if let Some(text) = non_empty_string(text) {
                parts.push(text);
            }
        }
        Value::Array(items) => {
            for item in items {
                if let Some(part) = render_claude_code_content_block(item) {
                    parts.push(part);
                }
            }
        }
        Value::Object(_) => {
            if let Some(part) = render_claude_code_content_block(value) {
                parts.push(part);
            }
        }
        _ => {}
    }
}

fn render_claude_code_content_block(block: &Value) -> Option<String> {
    let block_type = json_string_field(block, "type");
    match block_type.as_deref() {
        Some("thinking") => None,
        Some("text") => json_string_field(block, "text"),
        Some("tool_use") => render_claude_code_tool_use(block),
        Some("tool_result") => render_claude_code_tool_result(block),
        _ => json_string_field(block, "text")
            .or_else(|| json_string_field(block, "content"))
            .or_else(|| Some(pretty_json(block))),
    }
}

fn render_claude_code_tool_use(block: &Value) -> Option<String> {
    let name = json_string_field(block, "name").unwrap_or_else(|| "tool".to_string());
    let mut rendered = format!("Tool use: {name}");
    if let Some(input) = block.get("input").filter(|value| !value.is_null()) {
        rendered.push_str("\n\n```json\n");
        rendered.push_str(&pretty_json(input));
        rendered.push_str("\n```");
    }
    Some(rendered)
}

fn render_claude_code_tool_result(block: &Value) -> Option<String> {
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
        append_claude_code_content(content, &mut parts);
        let content = parts.join("\n\n");
        if let Some(content) = non_empty_string(&content) {
            rendered.push_str("\n\n");
            rendered.push_str(&content);
        }
    }

    Some(rendered)
}

fn content_has_block_type(value: &Value, expected_type: &str) -> bool {
    match value {
        Value::Array(items) => items
            .iter()
            .any(|item| content_has_block_type(item, expected_type)),
        Value::Object(_) => {
            json_string_field(value, "type").as_deref() == Some(expected_type)
                || value
                    .get("content")
                    .is_some_and(|content| content_has_block_type(content, expected_type))
        }
        _ => false,
    }
}

fn pretty_json(value: &Value) -> String {
    serde_json::to_string_pretty(value).unwrap_or_else(|_| value.to_string())
}

fn render_session_markdown(
    snapshot: &SessionFileSnapshot,
    parsed: &ParsedSession,
    title: &str,
    source_hash: &str,
) -> String {
    let mut fields = vec![
        (
            "source_kind",
            MetadataValue::string(SourceKind::Session.to_string()),
        ),
        (
            "source_location",
            MetadataValue::string(snapshot.location.clone()),
        ),
        (
            "source_archive",
            MetadataValue::string(redact_session_text(&path_to_string(&snapshot.path))),
        ),
        (
            "fetched_at",
            MetadataValue::string(snapshot.fetched_at.clone()),
        ),
        (
            "source_hash",
            MetadataValue::string(source_hash.to_string()),
        ),
        (
            "session_type",
            MetadataValue::string(parsed.session_type.clone()),
        ),
    ];
    if let Some(started_at) = &parsed.started_at {
        fields.push((
            "session_started_at",
            MetadataValue::string(started_at.clone()),
        ));
    }
    fields.extend(session_metadata_fields(parsed));

    let mut markdown = markdown_metadata_values(&fields);
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

fn json_string_field(value: &Value, field: &str) -> Option<String> {
    value
        .get(field)
        .and_then(Value::as_str)
        .and_then(non_empty_string)
}

#[cfg(test)]
mod tests;
