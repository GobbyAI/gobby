use std::collections::BTreeMap;
use std::fmt;
use std::ops::Range;

use serde_json::{Map, Value};

use crate::models::WikiSourceKind;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FrontmatterFormat {
    Yaml,
    Toml,
}

/// Workflow state of a wiki page, orthogonal to the `trust` reporting axis.
///
/// Transitions are owned by the maintenance loops: compile/upkeep create at
/// `draft`, a clean lint+librarian pass promotes to `reviewed`, an audit
/// citation pass promotes to `verified`, health demotes to `stale`, and
/// upkeep archives long-stale pages. `archived` pages stay on disk at their
/// stable paths but are excluded from catalog indexes, agent exports, and
/// default retrieval.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WikiLifecycle {
    Draft,
    Reviewed,
    Verified,
    Stale,
    Archived,
}

impl WikiLifecycle {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Draft => "draft",
            Self::Reviewed => "reviewed",
            Self::Verified => "verified",
            Self::Stale => "stale",
            Self::Archived => "archived",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "draft" => Some(Self::Draft),
            "reviewed" => Some(Self::Reviewed),
            "verified" => Some(Self::Verified),
            "stale" => Some(Self::Stale),
            "archived" => Some(Self::Archived),
            _ => None,
        }
    }
}

impl fmt::Display for WikiLifecycle {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WikiFrontmatter {
    pub title: Option<String>,
    pub aliases: Vec<String>,
    pub tags: Vec<String>,
    pub source_kind: Option<WikiSourceKind>,
    pub captured_from: Option<String>,
    pub source: Option<Value>,
    pub provenance: Option<Value>,
    pub generated_by: Option<String>,
    pub trust: Option<String>,
    pub freshness: Option<String>,
    pub indexed_at: Option<String>,
    pub lifecycle: Option<WikiLifecycle>,
    /// Quarantine marker for LLM-proposed pages that have not yet accrued
    /// corroboration (#17727). Candidates stay at their stable paths but are
    /// excluded from default retrieval/export surfaces until promotion
    /// clears the flag.
    pub candidate: bool,
    // Frontmatter keys gwiki does not own (operator- or other-tool-authored).
    // Rewrites re-serialize from this struct, so anything not carried here
    // would be silently deleted from the user's file.
    pub unknown: BTreeMap<String, Value>,
}

impl WikiFrontmatter {
    pub fn empty() -> Self {
        Self {
            title: None,
            aliases: Vec::new(),
            tags: Vec::new(),
            source_kind: None,
            captured_from: None,
            source: None,
            provenance: None,
            generated_by: None,
            trust: None,
            freshness: None,
            indexed_at: None,
            lifecycle: None,
            candidate: false,
            unknown: BTreeMap::new(),
        }
    }

    #[allow(dead_code, reason = "reserved gwiki CLI/API split")]
    pub fn as_json(&self) -> Value {
        let mut object = Map::new();
        for (key, value) in &self.unknown {
            object.insert(key.clone(), value.clone());
        }
        if let Some(title) = &self.title {
            object.insert("title".to_string(), Value::String(title.clone()));
        }
        if !self.aliases.is_empty() {
            object.insert(
                "aliases".to_string(),
                Value::Array(
                    self.aliases
                        .iter()
                        .map(|alias| Value::String(alias.clone()))
                        .collect(),
                ),
            );
        }
        if !self.tags.is_empty() {
            object.insert(
                "tags".to_string(),
                Value::Array(
                    self.tags
                        .iter()
                        .map(|tag| Value::String(tag.clone()))
                        .collect(),
                ),
            );
        }
        if let Some(source_kind) = self.source_kind {
            object.insert(
                "source_kind".to_string(),
                Value::String(source_kind.as_str().to_string()),
            );
        }
        if let Some(captured_from) = &self.captured_from {
            object.insert(
                "captured_from".to_string(),
                Value::String(captured_from.clone()),
            );
        }
        if let Some(source) = &self.source {
            object.insert("source".to_string(), source.clone());
        }
        if let Some(provenance) = &self.provenance {
            object.insert("provenance".to_string(), provenance.clone());
        }
        if let Some(generated_by) = &self.generated_by {
            object.insert(
                "generated_by".to_string(),
                Value::String(generated_by.clone()),
            );
        }
        if let Some(trust) = &self.trust {
            object.insert("trust".to_string(), Value::String(trust.clone()));
        }
        if let Some(freshness) = &self.freshness {
            object.insert("freshness".to_string(), Value::String(freshness.clone()));
        }
        if let Some(indexed_at) = &self.indexed_at {
            object.insert("indexed_at".to_string(), Value::String(indexed_at.clone()));
        }
        if let Some(lifecycle) = self.lifecycle {
            object.insert(
                "lifecycle".to_string(),
                Value::String(lifecycle.as_str().to_string()),
            );
        }
        if self.candidate {
            object.insert("candidate".to_string(), Value::Bool(true));
        }
        Value::Object(object)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedFrontmatter<'a> {
    pub format: Option<FrontmatterFormat>,
    pub range: Option<Range<usize>>,
    pub body_start: usize,
    pub body: &'a str,
    pub metadata: WikiFrontmatter,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FrontmatterError {
    detail: String,
}

impl fmt::Display for FrontmatterError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.detail)
    }
}

impl std::error::Error for FrontmatterError {}

pub fn parse_frontmatter(markdown: &str) -> Result<ParsedFrontmatter<'_>, FrontmatterError> {
    let Some(opening) = opening_delimiter(markdown) else {
        return Ok(ParsedFrontmatter {
            format: None,
            range: None,
            body_start: 0,
            body: markdown,
            metadata: WikiFrontmatter::empty(),
        });
    };

    let Some((raw_end, body_start)) =
        find_closing_delimiter(markdown, opening.marker, opening.content_start)
    else {
        return Err(FrontmatterError::new(format!(
            "unterminated {} frontmatter block",
            opening.marker
        )));
    };

    let raw = &markdown[opening.content_start..raw_end];
    let metadata = parse_metadata(opening.format, raw)?;

    Ok(ParsedFrontmatter {
        format: Some(opening.format),
        range: Some(0..body_start),
        body_start,
        body: &markdown[body_start..],
        metadata,
    })
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub fn mark_stale_markdown(markdown: &str, reason: &str) -> Result<String, FrontmatterError> {
    mark_stale_markdown_at(markdown, reason, &chrono::Utc::now().to_rfc3339())
}

/// [`mark_stale_markdown`] with an injected timestamp for deterministic tests.
///
/// Sets `lifecycle: stale` alongside the legacy `stale`/`stale_reason` keys,
/// and records `stale_at` (first demotion only) so upkeep can age long-stale
/// pages toward `archived`.
pub fn mark_stale_markdown_at(
    markdown: &str,
    reason: &str,
    stale_at: &str,
) -> Result<String, FrontmatterError> {
    let mut parsed = parse_frontmatter(markdown)?;
    parsed
        .metadata
        .unknown
        .insert("stale".to_string(), Value::Bool(true));
    parsed.metadata.unknown.insert(
        "stale_reason".to_string(),
        Value::String(reason.trim().to_string()),
    );
    parsed.metadata.lifecycle = Some(WikiLifecycle::Stale);
    parsed
        .metadata
        .unknown
        .entry("stale_at".to_string())
        .or_insert_with(|| Value::String(stale_at.to_string()));
    render_markdown_with_metadata(parsed.format, &parsed.metadata, parsed.body)
}

/// Re-render a page from parsed metadata and body, preserving the original
/// frontmatter format (YAML by default for pages without frontmatter).
pub(crate) fn render_markdown_with_metadata(
    format: Option<FrontmatterFormat>,
    metadata: &WikiFrontmatter,
    body: &str,
) -> Result<String, FrontmatterError> {
    let (delimiter, frontmatter) = match format.unwrap_or(FrontmatterFormat::Yaml) {
        FrontmatterFormat::Yaml => ("---", serialize_yaml_frontmatter(metadata)?),
        FrontmatterFormat::Toml => ("+++", serialize_toml_frontmatter(metadata)?),
    };
    Ok(format!("{delimiter}\n{frontmatter}{delimiter}\n{body}"))
}

impl FrontmatterError {
    fn new(detail: impl Into<String>) -> Self {
        Self {
            detail: detail.into(),
        }
    }
}

struct OpeningDelimiter {
    format: FrontmatterFormat,
    marker: &'static str,
    content_start: usize,
}

fn opening_delimiter(markdown: &str) -> Option<OpeningDelimiter> {
    delimiter_content_start(markdown, "---")
        .map(|content_start| OpeningDelimiter {
            format: FrontmatterFormat::Yaml,
            marker: "---",
            content_start,
        })
        .or_else(|| {
            delimiter_content_start(markdown, "+++").map(|content_start| OpeningDelimiter {
                format: FrontmatterFormat::Toml,
                marker: "+++",
                content_start,
            })
        })
}

fn delimiter_content_start(markdown: &str, marker: &str) -> Option<usize> {
    let rest = markdown.strip_prefix(marker)?;
    if rest.starts_with("\r\n") {
        Some(marker.len() + 2)
    } else if rest.starts_with('\n') {
        Some(marker.len() + 1)
    } else {
        None
    }
}

fn find_closing_delimiter(
    markdown: &str,
    marker: &str,
    mut offset: usize,
) -> Option<(usize, usize)> {
    while offset <= markdown.len() {
        let line_end = markdown[offset..]
            .find('\n')
            .map_or(markdown.len(), |relative| offset + relative);
        let line_content_end = markdown[..line_end]
            .strip_suffix('\r')
            .map_or(line_end, |line| line.len());
        let line = &markdown[offset..line_content_end];

        if line.trim() == marker {
            let body_start = if line_end < markdown.len() {
                line_end + 1
            } else {
                line_end
            };
            return Some((offset, body_start));
        }

        if line_end == markdown.len() {
            break;
        }
        offset = line_end + 1;
    }

    None
}

fn parse_metadata(
    format: FrontmatterFormat,
    raw: &str,
) -> Result<WikiFrontmatter, FrontmatterError> {
    let value = match format {
        FrontmatterFormat::Yaml => parse_yaml(raw)?,
        FrontmatterFormat::Toml => parse_toml(raw)?,
    };

    let object = match value {
        Value::Null => Map::new(),
        Value::Object(object) => object,
        other => {
            return Err(FrontmatterError::new(format!(
                "frontmatter must be a table/object, got {other}"
            )));
        }
    };

    Ok(frontmatter_from_object(object))
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
fn serialize_yaml_frontmatter(metadata: &WikiFrontmatter) -> Result<String, FrontmatterError> {
    let mut yaml = serde_yaml::to_string(&metadata.as_json()).map_err(|error| {
        FrontmatterError::new(format!("failed to serialize YAML frontmatter: {error}"))
    })?;
    if let Some(stripped) = yaml.strip_prefix("---\n") {
        yaml = stripped.to_string();
    }
    if let Some(stripped) = yaml.strip_suffix("...\n") {
        yaml = stripped.to_string();
    }
    if !yaml.ends_with('\n') {
        yaml.push('\n');
    }
    Ok(yaml)
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
fn serialize_toml_frontmatter(metadata: &WikiFrontmatter) -> Result<String, FrontmatterError> {
    let mut toml = toml::to_string(&metadata.as_json()).map_err(|error| {
        FrontmatterError::new(format!("failed to serialize TOML frontmatter: {error}"))
    })?;
    if !toml.ends_with('\n') {
        toml.push('\n');
    }
    Ok(toml)
}

fn parse_yaml(raw: &str) -> Result<Value, FrontmatterError> {
    if raw.trim().is_empty() {
        return Ok(Value::Object(Map::new()));
    }

    let value: serde_yaml::Value = serde_yaml::from_str(raw).map_err(|error| {
        FrontmatterError::new(format!("failed to parse YAML frontmatter: {error}"))
    })?;
    serde_json::to_value(value).map_err(|error| {
        FrontmatterError::new(format!(
            "failed to convert YAML frontmatter to JSON value: {error}"
        ))
    })
}

fn parse_toml(raw: &str) -> Result<Value, FrontmatterError> {
    if raw.trim().is_empty() {
        return Ok(Value::Object(Map::new()));
    }

    let value: toml::Table = toml::from_str(raw).map_err(|error| {
        FrontmatterError::new(format!("failed to parse TOML frontmatter: {error}"))
    })?;
    serde_json::to_value(value).map_err(|error| {
        FrontmatterError::new(format!(
            "failed to convert TOML frontmatter to JSON value: {error}"
        ))
    })
}

fn frontmatter_from_object(mut object: Map<String, Value>) -> WikiFrontmatter {
    use gobby_core::codewiki_contract;

    let title = object
        .remove(codewiki_contract::TITLE_KEY)
        .and_then(|value| string_value(&value));
    let aliases = object
        .remove("aliases")
        .map_or_else(Vec::new, |value| string_list(&value));
    let tags = object
        .remove("tags")
        .map_or_else(Vec::new, |value| tag_list(&value));
    let source_kind = object
        .remove("source_kind")
        .or_else(|| object.remove("kind"))
        .and_then(|value| string_value(&value))
        .and_then(|value| parse_source_kind(&value));
    let source = object.remove("source");
    let captured_from = object
        .remove("captured_from")
        .and_then(|value| string_value(&value));
    let provenance = object.remove(codewiki_contract::PROVENANCE_KEY);
    let generated_by = object
        .remove(codewiki_contract::GENERATED_BY_KEY)
        .and_then(|value| string_value(&value));
    let trust = object
        .remove(codewiki_contract::TRUST_KEY)
        .and_then(|value| string_value(&value));
    let freshness = object
        .remove(codewiki_contract::FRESHNESS_KEY)
        .and_then(|value| string_value(&value));
    let indexed_at = object
        .remove("indexed_at")
        .and_then(|value| string_value(&value));
    // Unrecognized lifecycle values stay in `unknown` so round-trips preserve
    // them instead of silently dropping operator-authored state.
    let lifecycle = match object.remove("lifecycle") {
        Some(value) => match string_value(&value)
            .as_deref()
            .and_then(WikiLifecycle::parse)
        {
            Some(lifecycle) => Some(lifecycle),
            None => {
                object.insert("lifecycle".to_string(), value);
                None
            }
        },
        None => None,
    };
    // Non-boolean `candidate` values stay in `unknown` so round-trips preserve
    // them instead of silently coercing operator-authored state.
    let candidate = match object.remove("candidate") {
        Some(Value::Bool(value)) => value,
        Some(value) => {
            object.insert("candidate".to_string(), value);
            false
        }
        None => false,
    };
    WikiFrontmatter {
        title,
        aliases,
        tags,
        source_kind,
        captured_from,
        source,
        provenance,
        generated_by,
        trust,
        freshness,
        indexed_at,
        lifecycle,
        candidate,
        unknown: object.into_iter().collect(),
    }
}

fn string_value(value: &Value) -> Option<String> {
    value.as_str().and_then(string_value_str)
}

fn string_list(value: &Value) -> Vec<String> {
    match value {
        Value::String(value) => string_value_str(value).into_iter().collect(),
        Value::Array(values) => values.iter().filter_map(string_value).collect(),
        _ => Vec::new(),
    }
}

fn string_value_str(value: &str) -> Option<String> {
    let value = value.trim();
    if value.is_empty() {
        None
    } else {
        Some(value.to_string())
    }
}

/// Parse tags from either a string (`"#rust wiki"`, `"rust, wiki"`) or an
/// array (`["#rust", "wiki"]`); a leading `#` is stripped from each tag.
fn tag_list(value: &Value) -> Vec<String> {
    match value {
        Value::String(value) => value
            .split(|character: char| character == ',' || character.is_whitespace())
            .map(|tag| tag.trim().trim_start_matches('#'))
            .filter(|tag| !tag.is_empty())
            .map(ToOwned::to_owned)
            .collect(),
        Value::Array(values) => values
            .iter()
            .filter_map(string_value)
            .map(|tag| tag.trim_start_matches('#').to_string())
            .collect(),
        _ => Vec::new(),
    }
}

fn parse_source_kind(value: &str) -> Option<WikiSourceKind> {
    match value
        .trim()
        .to_ascii_lowercase()
        .replace(['-', ' '], "_")
        .as_str()
    {
        "raw" => Some(WikiSourceKind::Raw),
        "source_note" => Some(WikiSourceKind::SourceNote),
        "concept" => Some(WikiSourceKind::Concept),
        "topic" => Some(WikiSourceKind::Topic),
        "inbox" => Some(WikiSourceKind::Inbox),
        "session" => Some(WikiSourceKind::Session),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lifecycle_round_trips_through_parse_and_as_json() {
        let markdown = "---\ntitle: Page\nlifecycle: reviewed\n---\n\nBody.\n";
        let parsed = parse_frontmatter(markdown).expect("parse");
        assert_eq!(parsed.metadata.lifecycle, Some(WikiLifecycle::Reviewed));
        assert!(!parsed.metadata.unknown.contains_key("lifecycle"));

        let json = parsed.metadata.as_json();
        assert_eq!(
            json.get("lifecycle").and_then(Value::as_str),
            Some("reviewed")
        );

        let rendered = render_markdown_with_metadata(parsed.format, &parsed.metadata, parsed.body)
            .expect("render");
        let reparsed = parse_frontmatter(&rendered).expect("reparse");
        assert_eq!(reparsed.metadata.lifecycle, Some(WikiLifecycle::Reviewed));
    }

    #[test]
    fn pages_without_lifecycle_default_to_none() {
        let parsed =
            parse_frontmatter("---\ntitle: Legacy\n---\n\nBody.\n").expect("parse legacy page");
        assert_eq!(parsed.metadata.lifecycle, None);
        assert!(parsed.metadata.as_json().get("lifecycle").is_none());
    }

    #[test]
    fn candidate_flag_round_trips_and_serializes_only_when_true() {
        let markdown = "---\ntitle: Page\nlifecycle: draft\ncandidate: true\n---\n\nBody.\n";
        let parsed = parse_frontmatter(markdown).expect("parse");
        assert!(parsed.metadata.candidate);
        assert!(!parsed.metadata.unknown.contains_key("candidate"));

        let rendered = render_markdown_with_metadata(parsed.format, &parsed.metadata, parsed.body)
            .expect("render");
        let reparsed = parse_frontmatter(&rendered).expect("reparse");
        assert!(reparsed.metadata.candidate);

        // Non-candidate pages never carry the key at all.
        let plain = parse_frontmatter("---\ntitle: Page\n---\n\nBody.\n").expect("parse plain");
        assert!(!plain.metadata.candidate);
        assert!(plain.metadata.as_json().get("candidate").is_none());
    }

    #[test]
    fn non_bool_candidate_values_stay_in_unknown() {
        let markdown = "---\ntitle: Page\ncandidate: pending\n---\n\nBody.\n";
        let parsed = parse_frontmatter(markdown).expect("parse");
        assert!(!parsed.metadata.candidate);
        assert_eq!(
            parsed
                .metadata
                .unknown
                .get("candidate")
                .and_then(Value::as_str),
            Some("pending")
        );
        // Round-trip preserves the operator-authored value verbatim.
        assert_eq!(
            parsed
                .metadata
                .as_json()
                .get("candidate")
                .and_then(Value::as_str),
            Some("pending")
        );
    }

    #[test]
    fn unrecognized_lifecycle_values_stay_in_unknown() {
        let markdown = "---\ntitle: Page\nlifecycle: mothballed\n---\n\nBody.\n";
        let parsed = parse_frontmatter(markdown).expect("parse");
        assert_eq!(parsed.metadata.lifecycle, None);
        assert_eq!(
            parsed
                .metadata
                .unknown
                .get("lifecycle")
                .and_then(Value::as_str),
            Some("mothballed")
        );
        // Round-trip preserves the operator-authored value verbatim.
        let json = parsed.metadata.as_json();
        assert_eq!(
            json.get("lifecycle").and_then(Value::as_str),
            Some("mothballed")
        );
    }

    #[test]
    fn every_lifecycle_state_parses_from_its_label() {
        for state in [
            WikiLifecycle::Draft,
            WikiLifecycle::Reviewed,
            WikiLifecycle::Verified,
            WikiLifecycle::Stale,
            WikiLifecycle::Archived,
        ] {
            assert_eq!(WikiLifecycle::parse(state.as_str()), Some(state));
            assert_eq!(
                WikiLifecycle::parse(&state.as_str().to_ascii_uppercase()),
                Some(state)
            );
        }
        assert_eq!(WikiLifecycle::parse("unknown"), None);
    }

    #[test]
    fn mark_stale_sets_lifecycle_and_first_demotion_timestamp() {
        let markdown = "---\ntitle: Page\nlifecycle: verified\n---\n\nBody.\n";
        let marked = mark_stale_markdown_at(markdown, "src/lib.rs changed", "2026-07-01T00:00:00Z")
            .expect("mark stale");
        let parsed = parse_frontmatter(&marked).expect("parse marked page");
        assert_eq!(parsed.metadata.lifecycle, Some(WikiLifecycle::Stale));
        assert_eq!(
            parsed
                .metadata
                .unknown
                .get("stale_at")
                .and_then(Value::as_str),
            Some("2026-07-01T00:00:00Z")
        );

        // A second demotion keeps the original stale_at: the page ages from
        // its FIRST demotion, and the new reason wins.
        let remarked = mark_stale_markdown_at(&marked, "still stale", "2026-07-08T00:00:00Z")
            .expect("re-mark stale");
        let reparsed = parse_frontmatter(&remarked).expect("parse re-marked page");
        assert_eq!(
            reparsed
                .metadata
                .unknown
                .get("stale_at")
                .and_then(Value::as_str),
            Some("2026-07-01T00:00:00Z")
        );
        assert_eq!(
            reparsed
                .metadata
                .unknown
                .get("stale_reason")
                .and_then(Value::as_str),
            Some("still stale")
        );
    }

    #[test]
    fn preserves_unknown_frontmatter() {
        let markdown = concat!(
            "---\n",
            "title: Build Notes\n",
            "aliases:\n",
            "  - Build Home\n",
            "tags: [rust, wiki]\n",
            "source_kind: source_note\n",
            "captured_from: https://example.com/original\n",
            "rank: 7\n",
            "nested:\n",
            "  keep: true\n",
            "---\n",
            "# Body\n",
        );

        let parsed = parse_frontmatter(markdown).expect("parse YAML frontmatter");

        assert_eq!(parsed.format, Some(FrontmatterFormat::Yaml));
        assert_eq!(
            parsed.body_start,
            markdown.find("# Body").expect("body offset")
        );
        assert_eq!(parsed.body, "# Body\n");
        assert_eq!(parsed.metadata.title.as_deref(), Some("Build Notes"));
        assert_eq!(parsed.metadata.aliases, vec!["Build Home"]);
        assert_eq!(parsed.metadata.tags, vec!["rust", "wiki"]);
        assert_eq!(
            parsed.metadata.source_kind,
            Some(WikiSourceKind::SourceNote)
        );
        assert_eq!(
            parsed.metadata.captured_from.as_deref(),
            Some("https://example.com/original")
        );
        assert_eq!(
            parsed.metadata.unknown.get("rank").and_then(Value::as_i64),
            Some(7)
        );
        assert_eq!(
            parsed
                .metadata
                .unknown
                .get("nested")
                .and_then(|value| value.get("keep"))
                .and_then(Value::as_bool),
            Some(true)
        );

        let toml = concat!(
            "+++\n",
            "title = \"TOML Page\"\n",
            "aliases = [\"TOML Alias\"]\n",
            "extra = \"preserved\"\n",
            "+++\n",
            "Body\n",
        );

        let parsed = parse_frontmatter(toml).expect("parse TOML frontmatter");

        assert_eq!(parsed.format, Some(FrontmatterFormat::Toml));
        assert_eq!(parsed.metadata.title.as_deref(), Some("TOML Page"));
        assert_eq!(parsed.metadata.aliases, vec!["TOML Alias"]);
        assert_eq!(
            parsed.metadata.unknown.get("extra").and_then(Value::as_str),
            Some("preserved")
        );
    }

    #[test]
    fn legacy_source_files_remain_unknown_metadata() {
        let markdown = concat!(
            "---\n",
            "title: Legacy Code Page\n",
            "source_files:\n",
            "  - file: src/lib.rs\n",
            "    ranges:\n",
            "      - 7-9\n",
            "---\n",
            "# Body\n",
        );

        let parsed = parse_frontmatter(markdown).expect("parse legacy frontmatter");

        assert!(parsed.metadata.source.is_none());
        assert!(parsed.metadata.provenance.is_none());
        assert!(parsed.metadata.unknown.contains_key("source_files"));
        assert_eq!(parsed.body, "# Body\n");
        assert!(markdown[..parsed.body_start].contains("source_files:"));
    }

    #[test]
    fn frontmatter_migration_parses_shared_contract_keys() {
        let markdown = concat!(
            "---\n",
            "title: Code Page\n",
            "source:\n",
            "  - file: src/lib.rs\n",
            "    ranges: [7-9]\n",
            "provenance:\n",
            "  - file: src/lib.rs\n",
            "    ranges: [7-9]\n",
            "generated_by: gwiki-code\n",
            "trust: generated\n",
            "freshness: indexed\n",
            "---\n",
            "# Body\n",
        );

        let parsed = parse_frontmatter(markdown).expect("parse shared frontmatter");

        assert_eq!(parsed.metadata.generated_by.as_deref(), Some("gwiki-code"));
        assert_eq!(parsed.metadata.trust.as_deref(), Some("generated"));
        assert_eq!(parsed.metadata.freshness.as_deref(), Some("indexed"));
        assert!(parsed.metadata.source.is_some());
        assert!(parsed.metadata.provenance.is_some());
        assert!(!parsed.metadata.unknown.contains_key("source"));
        assert!(!parsed.metadata.unknown.contains_key("provenance"));
    }

    #[test]
    fn codewiki_contract_golden_page_parses_into_contract_fields() {
        use gobby_core::codewiki_contract;

        let parsed = parse_frontmatter(codewiki_contract::GOLDEN_PAGE).expect("parse golden page");

        assert_eq!(parsed.metadata.title.as_deref(), Some("src/lib.rs"));
        assert_eq!(
            parsed.metadata.generated_by.as_deref(),
            Some(codewiki_contract::GENERATED_BY_GWIKI_CODE)
        );
        assert_eq!(
            parsed.metadata.trust.as_deref(),
            Some(codewiki_contract::TRUST_GENERATED)
        );
        assert_eq!(
            parsed.metadata.freshness.as_deref(),
            Some(codewiki_contract::FRESHNESS_INDEXED)
        );

        let provenance = parsed.metadata.provenance.as_ref().expect("provenance");
        let entry = provenance
            .as_array()
            .and_then(|entries| entries.first())
            .and_then(Value::as_object)
            .expect("provenance entry");
        assert_eq!(
            entry
                .get(codewiki_contract::PROVENANCE_FILE_KEY)
                .and_then(Value::as_str),
            Some("src/lib.rs")
        );
        assert!(
            entry
                .get(codewiki_contract::PROVENANCE_RANGES_KEY)
                .and_then(Value::as_array)
                .is_some_and(|ranges| !ranges.is_empty())
        );

        let links = crate::links::extract_links(parsed.body, std::iter::empty::<&str>());
        assert!(
            links
                .iter()
                .any(|link| link.target == "code/files/src/lib.rs"),
            "golden page wikilink must resolve to the code/files target: {links:?}"
        );
    }

    #[test]
    fn change_triggered_refresh_marks_page_stale_with_reason() {
        let markdown = concat!(
            "---\n",
            "title: Code Page\n",
            "generated_by: gwiki-code\n",
            "---\n",
            "# Code Page\n",
        );

        let marked = mark_stale_markdown(markdown, "src/lib.rs changed").expect("mark stale");
        let parsed = parse_frontmatter(&marked).expect("parse marked markdown");

        assert_eq!(
            parsed
                .metadata
                .unknown
                .get("stale")
                .and_then(Value::as_bool),
            Some(true)
        );
        assert_eq!(
            parsed
                .metadata
                .unknown
                .get("stale_reason")
                .and_then(Value::as_str),
            Some("src/lib.rs changed")
        );
        assert_eq!(parsed.metadata.title.as_deref(), Some("Code Page"));
        assert_eq!(parsed.body, "# Code Page\n");
    }

    #[test]
    fn change_triggered_refresh_preserves_toml_frontmatter() {
        let markdown = concat!(
            "+++\n",
            "title = \"Code Page\"\n",
            "generated_by = \"gwiki-code\"\n",
            "+++\n",
            "# Code Page\n",
        );

        let marked = mark_stale_markdown(markdown, "src/lib.rs changed").expect("mark stale");
        assert!(marked.starts_with("+++\n"), "{marked}");
        assert!(marked.contains("stale = true"), "{marked}");
        assert!(
            marked.contains("stale_reason = \"src/lib.rs changed\""),
            "{marked}"
        );
        let parsed = parse_frontmatter(&marked).expect("parse marked markdown");

        assert_eq!(parsed.format, Some(FrontmatterFormat::Toml));
        assert_eq!(
            parsed
                .metadata
                .unknown
                .get("stale")
                .and_then(Value::as_bool),
            Some(true)
        );
        assert_eq!(parsed.metadata.title.as_deref(), Some("Code Page"));
        assert_eq!(parsed.body, "# Code Page\n");
    }
}
