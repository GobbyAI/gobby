//! Source manifest records for immutable raw wiki sources.

use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::WikiError;

const SOURCE_MARKER: &str = "<!-- gwiki-source:";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SourceKind {
    Url,
    Audio,
    Image,
    Video,
    Pdf,
    Markdown,
    Text,
    File,
    Stdin,
    ResearchNote,
    #[serde(rename = "mediawiki")]
    MediaWiki,
    Wayback,
    GitRepository,
}

impl fmt::Display for SourceKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Url => "url",
            Self::Audio => "audio",
            Self::Image => "image",
            Self::Video => "video",
            Self::Pdf => "pdf",
            Self::Markdown => "markdown",
            Self::Text => "text",
            Self::File => "file",
            Self::Stdin => "stdin",
            Self::ResearchNote => "research_note",
            Self::MediaWiki => "mediawiki",
            Self::Wayback => "wayback",
            Self::GitRepository => "git_repository",
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IngestionMethod {
    Manual,
    Research,
}

impl fmt::Display for IngestionMethod {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Manual => "manual",
            Self::Research => "research",
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CompileStatus {
    Pending,
    Compiled,
}

impl fmt::Display for CompileStatus {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Pending => "pending",
            Self::Compiled => "compiled",
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SourceDraft {
    pub location: String,
    pub kind: SourceKind,
    pub fetched_at: String,
    pub content: Vec<u8>,
    pub title: Option<String>,
    pub citation: Option<String>,
    pub license: Option<String>,
    pub ingestion_method: IngestionMethod,
    pub compile_status: CompileStatus,
}

impl SourceDraft {
    pub fn url(
        location: impl Into<String>,
        fetched_at: impl Into<String>,
        content: impl Into<Vec<u8>>,
    ) -> Self {
        Self {
            location: location.into(),
            kind: SourceKind::Url,
            fetched_at: fetched_at.into(),
            content: content.into(),
            title: None,
            citation: None,
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
        }
    }

    pub fn with_title(mut self, title: impl Into<String>) -> Self {
        self.title = Some(title.into());
        self
    }

    pub fn with_citation(mut self, citation: impl Into<String>) -> Self {
        self.citation = Some(citation.into());
        self
    }

    pub fn with_license(mut self, license: impl Into<String>) -> Self {
        self.license = Some(license.into());
        self
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceRecord {
    pub id: String,
    pub location: String,
    pub canonical_location: String,
    pub kind: SourceKind,
    pub fetched_at: String,
    pub content_hash: String,
    pub title: Option<String>,
    pub citation: Option<String>,
    pub license: Option<String>,
    pub ingestion_method: IngestionMethod,
    pub compile_status: CompileStatus,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SourceManifest {
    pub entries: Vec<SourceRecord>,
}

impl SourceManifest {
    pub fn read(vault_root: &Path) -> Result<Self, WikiError> {
        let index_path = Self::index_path(vault_root);
        if !index_path.exists() {
            return Ok(Self::default());
        }

        let index = fs::read_to_string(&index_path).map_err(|error| WikiError::Io {
            action: "read raw source index",
            path: Some(index_path.clone()),
            source: error,
        })?;

        let mut entries = Vec::new();
        for line in index.lines() {
            let Some(marker_start) = line.find(SOURCE_MARKER) else {
                continue;
            };
            let json_start = marker_start + SOURCE_MARKER.len();
            let Some(marker_end) = line[json_start..].rfind("-->") else {
                return Err(WikiError::Json {
                    action: "parse raw source index marker",
                    path: Some(index_path),
                    source: serde_json::Error::io(std::io::Error::new(
                        std::io::ErrorKind::InvalidData,
                        "unterminated gwiki-source marker",
                    )),
                });
            };
            let json = line[json_start..json_start + marker_end].trim();
            let record = serde_json::from_str(json).map_err(|error| WikiError::Json {
                action: "parse raw source index marker",
                path: Some(index_path.clone()),
                source: error,
            })?;
            entries.push(record);
        }

        Ok(Self { entries })
    }

    pub fn register(vault_root: &Path, draft: SourceDraft) -> Result<SourceRecord, WikiError> {
        let content_hash = gobby_core::indexing::content_hash(&draft.content);
        Self::register_with_content_hash(vault_root, draft, content_hash)
    }

    pub fn register_with_content_hash(
        vault_root: &Path,
        draft: SourceDraft,
        content_hash: String,
    ) -> Result<SourceRecord, WikiError> {
        let mut manifest = Self::read(vault_root)?;
        let canonical_location = canonicalize_location(&draft.location);
        if let Some(existing) = manifest.entries.iter().find(|entry| {
            entry.canonical_location == canonical_location && entry.content_hash == content_hash
        }) {
            return Ok(existing.clone());
        }

        let record = SourceRecord {
            id: source_id(&canonical_location, &content_hash),
            location: draft.location,
            canonical_location,
            kind: draft.kind,
            fetched_at: draft.fetched_at,
            content_hash,
            title: draft.title,
            citation: draft.citation,
            license: draft.license,
            ingestion_method: draft.ingestion_method,
            compile_status: draft.compile_status,
        };
        manifest.entries.push(record.clone());
        manifest.write(vault_root)?;
        Ok(record)
    }

    pub fn write(&self, vault_root: &Path) -> Result<(), WikiError> {
        let raw_dir = vault_root.join("raw");
        fs::create_dir_all(&raw_dir).map_err(|error| WikiError::Io {
            action: "create raw source directory",
            path: Some(raw_dir.clone()),
            source: error,
        })?;

        let index_path = Self::index_path(vault_root);
        let mut index = existing_index_without_manifest(&index_path)?;
        if !self.entries.is_empty() {
            index.push_str("## Source manifest\n\n");
        }
        for entry in &self.entries {
            render_entry(entry, &mut index)?;
        }

        fs::write(&index_path, index).map_err(|error| WikiError::Io {
            action: "write raw source index",
            path: Some(index_path),
            source: error,
        })
    }

    pub fn index_path(vault_root: &Path) -> PathBuf {
        vault_root.join("raw").join("INDEX.md")
    }
}

fn render_entry(entry: &SourceRecord, index: &mut String) -> Result<(), WikiError> {
    let title = entry.title.as_deref().unwrap_or(&entry.location);
    index.push_str("- [");
    index.push_str(&escape_markdown_text(title));
    index.push_str("](");
    index.push_str(&escape_markdown_destination(&entry.location));
    index.push_str(")\n");
    index.push_str("  - id: `");
    index.push_str(&entry.id);
    index.push_str("`\n");
    index.push_str("  - canonical: `");
    index.push_str(&entry.canonical_location);
    index.push_str("`\n");
    index.push_str("  - kind: `");
    index.push_str(&entry.kind.to_string());
    index.push_str("`\n");
    index.push_str("  - fetched_at: `");
    index.push_str(&entry.fetched_at);
    index.push_str("`\n");
    index.push_str("  - hash: `");
    index.push_str(&entry.content_hash);
    index.push_str("`\n");
    if let Some(citation) = &entry.citation {
        index.push_str("  - citation: ");
        index.push_str(&inline_text(citation));
        index.push('\n');
    }
    if let Some(license) = &entry.license {
        index.push_str("  - license: ");
        index.push_str(&inline_text(license));
        index.push('\n');
    }
    index.push_str("  - ingestion_method: `");
    index.push_str(&entry.ingestion_method.to_string());
    index.push_str("`\n");
    index.push_str("  - compile_status: `");
    index.push_str(&entry.compile_status.to_string());
    index.push_str("`\n");

    let metadata = serde_json::to_string(entry).map_err(|error| WikiError::Json {
        action: "serialize raw source index marker",
        path: None,
        source: error,
    })?;
    index.push_str("  - ");
    index.push_str(SOURCE_MARKER);
    index.push_str(&metadata);
    index.push_str(" -->\n");
    Ok(())
}

fn canonicalize_location(location: &str) -> String {
    let without_fragment = location.trim().split('#').next().unwrap_or("").trim();
    let canonical = lower_url_scheme_and_authority(without_fragment);
    let (mut base, query) = split_sorted_query(&canonical);
    while base.ends_with('/') && base != "/" && !base.ends_with("://") {
        base.pop();
    }
    match query {
        Some(query) if !query.is_empty() => format!("{base}?{query}"),
        _ => base,
    }
}

fn split_sorted_query(location: &str) -> (String, Option<String>) {
    let Some((base, query)) = location.split_once('?') else {
        return (location.to_string(), None);
    };
    let mut params = query
        .split('&')
        .filter(|param| !param.is_empty())
        .collect::<Vec<_>>();
    params.sort_unstable();
    (base.to_string(), Some(params.join("&")))
}

fn existing_index_without_manifest(index_path: &Path) -> Result<String, WikiError> {
    if !index_path.exists() {
        return Ok(String::from("# Raw sources\n\n"));
    }

    let existing = fs::read_to_string(index_path).map_err(|error| WikiError::Io {
        action: "read raw source index",
        path: Some(index_path.to_path_buf()),
        source: error,
    })?;
    let mut preserved = Vec::new();
    let mut skipping_manifest = false;

    for line in existing.lines() {
        if line.trim() == "## Source manifest" {
            skipping_manifest = true;
            continue;
        }
        if skipping_manifest && line.starts_with("## ") {
            skipping_manifest = false;
        }
        if !skipping_manifest {
            preserved.push(line);
        }
    }

    let mut index = preserved.join("\n");
    if index.trim().is_empty() {
        index.push_str("# Raw sources");
    }
    while index.ends_with('\n') {
        index.pop();
    }
    index.push_str("\n\n");
    Ok(index)
}

fn lower_url_scheme_and_authority(location: &str) -> String {
    let Some((scheme, rest)) = location.split_once("://") else {
        return location.replace('\\', "/");
    };
    let (authority, path) = rest.split_once('/').unwrap_or((rest, ""));
    if path.is_empty() {
        format!(
            "{}://{}",
            scheme.to_ascii_lowercase(),
            authority.to_ascii_lowercase()
        )
    } else {
        format!(
            "{}://{}/{}",
            scheme.to_ascii_lowercase(),
            authority.to_ascii_lowercase(),
            path
        )
    }
}

fn source_id(canonical_location: &str, content_hash: &str) -> String {
    let hash_prefix = &content_hash[..content_hash.len().min(12)];
    let mut slug = String::new();
    let mut last_was_dash = false;
    for ch in canonical_location.chars().flat_map(char::to_lowercase) {
        if ch.is_ascii_alphanumeric() {
            slug.push(ch);
            last_was_dash = false;
        } else if !last_was_dash && !slug.is_empty() {
            slug.push('-');
            last_was_dash = true;
        }
        if slug.len() >= 48 {
            break;
        }
    }
    while slug.ends_with('-') {
        slug.pop();
    }

    if slug.is_empty() {
        format!("src-{hash_prefix}")
    } else {
        format!("src-{hash_prefix}-{slug}")
    }
}

fn escape_markdown_text(text: &str) -> String {
    text.replace('[', "\\[").replace(']', "\\]")
}

fn escape_markdown_destination(destination: &str) -> String {
    destination.replace('(', "\\(").replace(')', "\\)")
}

fn inline_text(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dedupes_by_canonical_identity_and_hash() {
        let temp = tempfile::tempdir().expect("tempdir");
        let content = b"Source material about durable wiki provenance.".to_vec();

        let first = SourceManifest::register(
            temp.path(),
            SourceDraft::url(
                "https://Example.com/docs/provenance#chunk-1",
                "2026-05-29T15:00:00Z",
                content.clone(),
            )
            .with_title("Durable provenance")
            .with_citation("Example Docs, Durable provenance")
            .with_license("Apache-2.0"),
        )
        .expect("first source registered");
        let duplicate = SourceManifest::register(
            temp.path(),
            SourceDraft::url(
                "https://example.com/docs/provenance/",
                "2026-05-29T16:00:00Z",
                content,
            )
            .with_title("Duplicate durable provenance")
            .with_citation("Duplicate citation should not replace record")
            .with_license("MIT"),
        )
        .expect("duplicate source reused");

        assert_eq!(duplicate, first);

        let index = std::fs::read_to_string(SourceManifest::index_path(temp.path()))
            .expect("raw index written");
        assert_eq!(index.matches("gwiki-source:").count(), 1);
        assert!(index.contains("https://Example.com/docs/provenance#chunk-1"));
        assert!(index.contains("kind: `url`"));
        assert!(index.contains("fetched_at: `2026-05-29T15:00:00Z`"));
        assert!(index.contains(&first.content_hash));
        assert!(index.contains("citation: Example Docs, Durable provenance"));
        assert!(index.contains("license: Apache-2.0"));
        assert!(index.contains("ingestion_method: `manual`"));
        assert!(index.contains("compile_status: `pending`"));
    }

    #[test]
    fn canonical_location_sorts_query_before_trimming_slash() {
        assert_eq!(
            canonicalize_location("https://Example.com/docs/?b=2&a=1#frag"),
            "https://example.com/docs?a=1&b=2"
        );
    }
}
