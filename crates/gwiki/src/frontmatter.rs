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
            unknown: BTreeMap::new(),
        }
    }

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
    let title = object
        .remove("title")
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
    let provenance = object.remove("provenance");
    let generated_by = object
        .remove("generated_by")
        .and_then(|value| string_value(&value));
    let trust = object
        .remove("trust")
        .and_then(|value| string_value(&value));
    let freshness = object
        .remove("freshness")
        .and_then(|value| string_value(&value));
    let indexed_at = object
        .remove("indexed_at")
        .and_then(|value| string_value(&value));
    let legacy_source = object
        .get("source_files")
        .cloned()
        .or_else(|| object.get("sources").cloned());
    let captured_from = captured_from.or_else(|| source.as_ref().and_then(string_value));
    let source = source.or_else(|| legacy_source.clone());
    let provenance = provenance.or(legacy_source);

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
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
    fn frontmatter_migration_normalizes_legacy_source_files_without_rewriting() {
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

        let source = parsed
            .metadata
            .source
            .as_ref()
            .expect("legacy source_files normalized to source");
        assert_eq!(
            source,
            parsed.metadata.provenance.as_ref().expect("provenance")
        );
        assert_eq!(
            source
                .as_array()
                .and_then(|items| items.first())
                .and_then(|item| item.get("file"))
                .and_then(Value::as_str),
            Some("src/lib.rs")
        );
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
            "generated_by: gcode-codewiki\n",
            "trust: generated\n",
            "freshness: indexed\n",
            "---\n",
            "# Body\n",
        );

        let parsed = parse_frontmatter(markdown).expect("parse shared frontmatter");

        assert_eq!(
            parsed.metadata.generated_by.as_deref(),
            Some("gcode-codewiki")
        );
        assert_eq!(parsed.metadata.trust.as_deref(), Some("generated"));
        assert_eq!(parsed.metadata.freshness.as_deref(), Some("indexed"));
        assert!(parsed.metadata.source.is_some());
        assert!(parsed.metadata.provenance.is_some());
        assert!(!parsed.metadata.unknown.contains_key("source"));
        assert!(!parsed.metadata.unknown.contains_key("provenance"));
    }
}
