use std::collections::BTreeMap;

use super::SessionWikiFileSnapshot;
use super::non_empty_string;
use super::redaction::redact_session_text;
use crate::ingest::{MetadataValue, markdown_metadata_values, path_to_string};
use crate::sources::SourceKind;

/// Parsed daemon-owned wiki frontmatter plus the synthesized body.
///
/// The daemon writes one leading `---`-fenced YAML frontmatter block followed
/// by the knowledge body; this splits the two and exposes the simple
/// `key: value` frontmatter fields gwiki carries forward (`title`, `source`,
/// `model`, `tags`). Anything malformed degrades to "treat the whole document
/// as body" so a missing fence never drops content.
pub(super) struct DaemonWikiPage {
    frontmatter: BTreeMap<String, String>,
    body: String,
}

impl DaemonWikiPage {
    pub(super) fn parse(text: &str) -> Self {
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

    pub(super) fn field(&self, key: &str) -> Option<&String> {
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

pub(super) fn render_session_wiki_markdown(
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
