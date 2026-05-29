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
    let captured_from = object
        .remove("captured_from")
        .or_else(|| object.remove("source"))
        .and_then(|value| string_value(&value));

    WikiFrontmatter {
        title,
        aliases,
        tags,
        source_kind,
        captured_from,
        unknown: object.into_iter().collect(),
    }
}

fn string_value(value: &Value) -> Option<String> {
    value.as_str().map(str::trim).and_then(|value| {
        if value.is_empty() {
            None
        } else {
            Some(value.to_string())
        }
    })
}

fn string_list(value: &Value) -> Vec<String> {
    match value {
        Value::String(value) => string_value(&Value::String(value.clone()))
            .into_iter()
            .collect(),
        Value::Array(values) => values.iter().filter_map(string_value).collect(),
        _ => Vec::new(),
    }
}

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
}
