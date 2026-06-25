use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use serde::Deserialize;
use serde_json::Value;

mod codex;
mod derived;
mod droid;
mod gemini;
mod grok;
mod metadata;
mod qwen;
mod redaction;

use codex::CODEX_SESSION_ADAPTER;
use derived::write_session_derived_markdown;
use droid::DROID_SESSION_ADAPTER;
use gemini::GEMINI_SESSION_ADAPTER;
use grok::GROK_SESSION_ADAPTER;
pub(crate) use metadata::ParsedSessionMetadata;
use metadata::session_metadata_fields;
use qwen::QWEN_SESSION_ADAPTER;
use redaction::{redact_session_markdown, redact_session_text};

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
    let envelopes = read_session_archive(&snapshot.path, &snapshot.bytes)?;
    let adapters = default_session_adapters();
    let parsed = parse_session_archive(&envelopes, &adapters)?;
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

fn default_session_adapters() -> [&'static dyn SessionTranscriptAdapter; 7] {
    [
        &COMMON_SESSION_ADAPTER,
        &GROK_SESSION_ADAPTER,
        &DROID_SESSION_ADAPTER,
        &QWEN_SESSION_ADAPTER,
        &CLAUDE_CODE_ADAPTER,
        &CODEX_SESSION_ADAPTER,
        &GEMINI_SESSION_ADAPTER,
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

/// Parsed daemon-owned wiki frontmatter plus the synthesized body.
///
/// The daemon writes one leading `---`-fenced YAML frontmatter block followed
/// by the knowledge body; this splits the two and exposes the simple
/// `key: value` frontmatter fields gwiki carries forward (`title`, `source`,
/// `model`, `tags`). Anything malformed degrades to "treat the whole document
/// as body" so a missing fence never drops content.
struct DaemonWikiPage {
    frontmatter: BTreeMap<String, String>,
    body: String,
}

impl DaemonWikiPage {
    fn parse(text: &str) -> Self {
        let text = text.strip_prefix('\u{feff}').unwrap_or(text);
        let mut lines = text.lines();
        if lines.next().map(str::trim_end) != Some("---") {
            return Self::body_only(text);
        }

        let mut frontmatter = BTreeMap::new();
        let mut closed = false;
        let mut body_lines: Vec<&str> = Vec::new();
        for line in lines {
            if !closed {
                if line.trim_end() == "---" {
                    closed = true;
                    continue;
                }
                if let Some((key, value)) = line.split_once(':') {
                    let key = key.trim();
                    if !key.is_empty() {
                        frontmatter.insert(key.to_string(), unquote_frontmatter_value(value));
                    }
                }
                continue;
            }
            body_lines.push(line);
        }

        if !closed {
            // No closing fence: keep the whole document as body, defensively.
            return Self::body_only(text);
        }

        Self {
            frontmatter,
            body: body_lines.join("\n").trim().to_string(),
        }
    }

    fn body_only(text: &str) -> Self {
        Self {
            frontmatter: BTreeMap::new(),
            body: text.trim().to_string(),
        }
    }

    fn field(&self, key: &str) -> Option<&String> {
        self.frontmatter.get(key)
    }

    fn tags(&self) -> Vec<String> {
        let Some(raw) = self.frontmatter.get("tags") else {
            return Vec::new();
        };
        let trimmed = raw.trim();
        let inner = trimmed
            .strip_prefix('[')
            .and_then(|rest| rest.strip_suffix(']'))
            .unwrap_or(trimmed);
        inner
            .split(',')
            .map(|tag| tag.trim().trim_matches('"').trim())
            .filter(|tag| !tag.is_empty())
            .map(str::to_string)
            .collect()
    }
}

fn unquote_frontmatter_value(value: &str) -> String {
    let value = value.trim();
    let bytes = value.as_bytes();
    if bytes.len() >= 2
        && ((bytes[0] == b'"' && bytes[bytes.len() - 1] == b'"')
            || (bytes[0] == b'\'' && bytes[bytes.len() - 1] == b'\''))
    {
        return value[1..value.len() - 1].to_string();
    }
    value.to_string()
}

fn render_session_wiki_markdown(
    snapshot: &SessionWikiFileSnapshot,
    page: &DaemonWikiPage,
    location: &str,
    title: &str,
    source_hash: &str,
) -> String {
    let source_archive = redact_session_text(&path_to_string(&snapshot.path));
    let mut fields = vec![
        (
            "source_kind",
            MetadataValue::string(SourceKind::Session.to_string()),
        ),
        (
            "source_location",
            MetadataValue::string(location.to_string()),
        ),
        ("source_archive", MetadataValue::string(source_archive)),
        (
            "fetched_at",
            MetadataValue::string(snapshot.fetched_at.clone()),
        ),
        (
            "source_hash",
            MetadataValue::string(source_hash.to_string()),
        ),
    ];
    if let Some(session_type) = page
        .field("source")
        .and_then(|value| non_empty_string(value))
    {
        fields.push(("session_type", MetadataValue::string(session_type)));
    }
    if let Some(model) = page
        .field("model")
        .and_then(|value| non_empty_string(value))
    {
        fields.push(("model", MetadataValue::string(model)));
    }
    // Preserve the daemon page's `project:` provenance inside the machine-global
    // `sessions` topic. Named `session_project` so it reads as provenance rather
    // than a gwiki scope key. See gobby-cli #940 / session-wiki Model B.
    if let Some(project) = page
        .field("project")
        .and_then(|value| non_empty_string(value))
    {
        fields.push(("session_project", MetadataValue::string(project)));
    }
    let tags = page.tags();
    if !tags.is_empty() {
        fields.push(("tags", MetadataValue::json(&tags)));
    }

    let mut markdown = markdown_metadata_values(&fields);
    markdown.push_str("# ");
    markdown.push_str(title);
    markdown.push_str("\n\n");
    markdown.push_str(page.body.trim());
    markdown.push('\n');
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
mod tests {
    use super::*;

    #[test]
    fn render_session_markdown_emits_deterministic_session_frontmatter() {
        let snapshot = SessionFileSnapshot {
            location: "/tmp/session.jsonl".to_string(),
            file_name: "session.jsonl".to_string(),
            fetched_at: "2026-06-16T20:00:00Z".to_string(),
            path: PathBuf::from("/tmp/session.jsonl"),
            bytes: Vec::new(),
        };
        let parsed = ParsedSession {
            title: "Fixture session".to_string(),
            session_type: "claude-code".to_string(),
            started_at: Some("2026-06-16T20:00:00Z".to_string()),
            metadata: ParsedSessionMetadata {
                model: Some("claude-opus-4-8".to_string()),
                token_totals: std::collections::BTreeMap::from([
                    ("input_tokens".to_string(), 10),
                    ("output_tokens".to_string(), 5),
                ]),
                git_branch: Some("dev".to_string()),
                is_subagent: true,
            },
            messages: vec![
                ParsedSessionMessage {
                    role: "assistant".to_string(),
                    timestamp: Some("2026-06-16T20:00:00Z".to_string()),
                    content: "I will inspect.".to_string(),
                    tool_names: vec!["Read".to_string(), "Bash".to_string()],
                },
                ParsedSessionMessage {
                    role: "assistant".to_string(),
                    timestamp: Some("2026-06-16T21:01:01Z".to_string()),
                    content: "Done.".to_string(),
                    tool_names: vec!["Read".to_string()],
                },
            ],
        };

        let markdown = render_session_markdown(&snapshot, &parsed, &parsed.title, "hash");

        assert!(markdown.contains("model: claude-opus-4-8\n"));
        assert!(markdown.contains("tool_counts: {\"Bash\":1,\"Read\":2}\n"));
        assert!(markdown.contains("token_totals: {\"input_tokens\":10,\"output_tokens\":5}\n"));
        assert!(markdown.contains("duration_seconds: 3661\n"));
        assert!(
            markdown.contains(
                "hour_buckets: {\"2026-06-16T20:00:00Z\":1,\"2026-06-16T21:00:00Z\":1}\n"
            )
        );
        assert!(markdown.contains("is_subagent: true\n"));
        assert!(markdown.contains("gitBranch: dev\n"));
    }

    #[test]
    fn session_wiki_ingest_strips_daemon_frontmatter_and_redacts() {
        let temp = tempfile::tempdir().expect("tempdir");
        let openai_key = format!("{}{}", "sk-proj-", "abcdefghijklmnopqrstuvwxyz123456");
        let daemon_md = format!(
            concat!(
                "---\n",
                "title: \"Session: abcd1234 — 2026-06-24\"\n",
                "type: source\n",
                "tags: [rust, sessions]\n",
                "date: 2026-06-24\n",
                "model: claude-opus-4-8\n",
                "project: proj-1\n",
                "session_id: sess-1\n",
                "source: claude\n",
                "---\n",
                "\n",
                "## Summary\n\nWired the synthesis ingest.\n\n",
                "## Key Quotes\n\n> Token {key} lives at /Users/casey/secret.env\n\n",
                "## Connections\n\n- [[session-transcript-wiki-fix]]\n"
            ),
            key = openai_key
        );
        let snapshot = SessionWikiFileSnapshot {
            external_id: "abcd1234-0000-4000-8000-000000000000".to_string(),
            path: PathBuf::from(
                "/Users/josh/.gobby/session_wiki/abcd1234-0000-4000-8000-000000000000.md",
            ),
            fetched_at: "2026-06-24T00:00:00Z".to_string(),
            bytes: daemon_md.into_bytes(),
        };

        let result =
            ingest_session_wiki_file_without_index(temp.path(), snapshot).expect("ingest wiki");

        assert_eq!(
            result.record.location,
            "session:abcd1234-0000-4000-8000-000000000000"
        );

        let derived = std::fs::read_to_string(
            temp.path()
                .join("knowledge")
                .join("sources")
                .join(format!("{}.md", result.record.id)),
        )
        .expect("derived markdown");

        // Exactly one frontmatter block: the daemon block was stripped.
        let fence_lines = derived
            .lines()
            .filter(|line| line.trim_end() == "---")
            .count();
        assert_eq!(
            fence_lines, 2,
            "expected one frontmatter block, got:\n{derived}"
        );
        assert!(!derived.contains("type: source"));
        assert!(!derived.contains("session_id: sess-1"));
        // The daemon `project:` line is stripped, but its value is preserved as a
        // gwiki-owned `session_project` field. Match on exact lines: a substring
        // check is ambiguous because `session_project: proj-1` contains the
        // `project: proj-1` substring.
        assert!(
            derived
                .lines()
                .any(|line| line.trim() == "session_project: proj-1"),
            "expected preserved session_project provenance, got:\n{derived}"
        );
        assert!(
            !derived
                .lines()
                .any(|line| line.trim_start().starts_with("project:")),
            "expected raw daemon project: line to be stripped, got:\n{derived}"
        );

        // gwiki-owned frontmatter.
        assert!(derived.contains("source_kind: session"));
        assert!(derived.contains("session:abcd1234-0000-4000-8000-000000000000"));
        assert!(derived.contains("source_archive:"));
        assert!(derived.contains("session_type: claude"));
        assert!(derived.contains("model: claude-opus-4-8"));
        assert!(derived.contains("tags:"));
        assert!(derived.contains("rust"));

        // Body kept: H1 title + sections + wikilink, no `## Messages` dump.
        assert!(derived.contains("# Session: abcd1234"));
        assert!(derived.contains("## Summary"));
        assert!(derived.contains("## Connections"));
        assert!(derived.contains("[[session-transcript-wiki-fix]]"));
        assert!(!derived.contains("## Messages"));

        // Defensive re-redaction over the synthesized body and the mirror path.
        assert!(derived.contains("[REDACTED_API_KEY]"));
        assert!(!derived.contains(&openai_key));
        assert!(!derived.contains("/Users/casey"));
        assert!(!derived.contains("/Users/josh"));
        assert!(derived.contains("[REDACTED_HOME]"));
    }

    #[test]
    fn common_session_adapter_accepts_fixture_payload_messages() {
        let envelopes = vec![SessionArchiveEnvelope {
            envelope_type: "session".to_string(),
            timestamp: Some("2026-06-16T20:00:00Z".to_string()),
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

    #[test]
    fn read_session_archive_accepts_raw_claude_code_records() {
        let bytes = br#"{"type":"user","sessionId":"session-1","message":{"role":"user","content":"Hello"}}
{"type":"assistant","timestamp":"2026-06-16T20:00:01Z","message":{"role":"assistant","content":[{"type":"text","text":"Hi."}]}}
"#;

        let envelopes =
            read_session_archive(Path::new("claude.jsonl"), bytes).expect("read raw archive");

        assert_eq!(envelopes.len(), 2);
        assert_eq!(envelopes[0].envelope_type, "user");
        assert_eq!(envelopes[0].timestamp, None);
        assert!(envelopes[0].payload.get("message").is_some());
        assert_eq!(
            envelopes[1].timestamp.as_deref(),
            Some("2026-06-16T20:00:01Z")
        );
    }

    #[test]
    fn claude_code_adapter_parses_messages_tools_and_sidechains() {
        let envelopes = vec![
            SessionArchiveEnvelope {
                envelope_type: "ai-title".to_string(),
                timestamp: Some("2026-06-16T20:00:00Z".to_string()),
                payload: serde_json::json!({
                    "type": "ai-title",
                    "timestamp": "2026-06-16T20:00:00Z",
                    "aiTitle": "Claude Fixture"
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "user".to_string(),
                timestamp: Some("2026-06-16T20:00:01Z".to_string()),
                payload: serde_json::json!({
                    "type": "user",
                    "timestamp": "2026-06-16T20:00:01Z",
                    "message": {"role": "user", "content": "Inspect this repo."}
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "assistant".to_string(),
                timestamp: Some("2026-06-16T20:00:02Z".to_string()),
                payload: serde_json::json!({
                    "type": "assistant",
                    "timestamp": "2026-06-16T20:00:02Z",
                    "gitBranch": "dev",
                    "message": {
                        "id": "msg_1",
                        "model": "claude-opus-4-8",
                        "role": "assistant",
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 5
                        },
                        "content": [
                            {"type": "thinking", "thinking": "internal"},
                            {"type": "text", "text": "I will inspect it."},
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "Cargo.toml"}}
                        ]
                    }
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "user".to_string(),
                timestamp: Some("2026-06-16T20:00:03Z".to_string()),
                payload: serde_json::json!({
                    "type": "user",
                    "timestamp": "2026-06-16T20:00:03Z",
                    "toolUseResult": {"file": "Cargo.toml"},
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_1",
                                "content": "workspace manifest",
                                "is_error": false
                            }
                        ]
                    }
                }),
            },
            SessionArchiveEnvelope {
                envelope_type: "assistant".to_string(),
                timestamp: Some("2026-06-16T20:00:04Z".to_string()),
                payload: serde_json::json!({
                    "type": "assistant",
                    "timestamp": "2026-06-16T20:00:04Z",
                    "isSidechain": true,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Sidechain note."}]
                    }
                }),
            },
        ];

        let adapters = default_session_adapters();
        let parsed =
            parse_session_archive(&envelopes, &adapters).expect("parse Claude Code fixture");

        assert_eq!(parsed.title, "Claude Fixture");
        assert_eq!(parsed.session_type, "claude-code");
        assert_eq!(parsed.started_at.as_deref(), Some("2026-06-16T20:00:00Z"));
        assert_eq!(parsed.metadata.model.as_deref(), Some("claude-opus-4-8"));
        assert_eq!(parsed.metadata.git_branch.as_deref(), Some("dev"));
        assert!(parsed.metadata.is_subagent);
        assert_eq!(parsed.metadata.token_totals.get("input_tokens"), Some(&10));
        assert_eq!(parsed.metadata.token_totals.get("output_tokens"), Some(&5));
        assert_eq!(parsed.messages.len(), 4);
        assert_eq!(parsed.messages[0].role, "user");
        assert_eq!(parsed.messages[0].content, "Inspect this repo.");
        assert_eq!(parsed.messages[1].role, "assistant");
        assert_eq!(parsed.messages[1].tool_names, vec!["Read"]);
        assert!(parsed.messages[1].content.contains("I will inspect it."));
        assert!(parsed.messages[1].content.contains("Tool use: Read"));
        assert!(!parsed.messages[1].content.contains("internal"));
        assert_eq!(parsed.messages[2].role, "tool result");
        assert!(parsed.messages[2].content.contains("Tool result: toolu_1"));
        assert!(parsed.messages[2].content.contains("workspace manifest"));
        assert_eq!(parsed.messages[3].role, "assistant (sidechain)");
        assert_eq!(parsed.messages[3].content, "Sidechain note.");
    }

    #[test]
    fn claude_code_adapter_parses_real_archive_when_fixture_is_set() {
        let Ok(path) = std::env::var("GWIKI_CLAUDE_CODE_ARCHIVE_FIXTURE") else {
            return;
        };
        let bytes = std::fs::read(&path).expect("read real Claude Code archive fixture");
        let envelopes =
            read_session_archive(Path::new(&path), &bytes).expect("read real Claude Code archive");
        let adapters = default_session_adapters();

        let parsed =
            parse_session_archive(&envelopes, &adapters).expect("parse real Claude Code archive");

        assert_eq!(parsed.session_type, "claude-code");
        assert!(
            parsed.messages.len() > 1,
            "expected more than one parsed Claude Code message"
        );
        assert!(parsed.messages.iter().any(|message| message.role == "user"));
        assert!(parsed.messages.iter().any(|message| {
            message.role == "assistant" || message.role == "assistant (sidechain)"
        }));
        assert!(
            parsed
                .messages
                .iter()
                .any(|message| message.role.starts_with("tool result")),
            "expected real archive to include at least one tool result"
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
            file_name: "claude-code-real.jsonl".to_string(),
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
