use std::path::{Path, PathBuf};

use chrono::FixedOffset;

use crate::WikiError;
use crate::sources::{SourceManifest, SourceRecord};
use crate::support::time::{ParsedTimestamp, format_timestamp, local_offset_for, parse_timestamp};

pub fn render_citations(
    vault_root: &Path,
    source_paths: &[PathBuf],
) -> Result<Vec<String>, WikiError> {
    Ok(source_records_for_paths(vault_root, source_paths)?
        .iter()
        .map(render_source_citation)
        .collect())
}

pub fn source_records_for_paths(
    vault_root: &Path,
    source_paths: &[PathBuf],
) -> Result<Vec<SourceRecord>, WikiError> {
    let manifest = SourceManifest::read(vault_root)?;
    if source_paths.is_empty() {
        return Ok(manifest.entries);
    }

    let mut records = Vec::new();
    for entry in manifest.entries {
        if source_paths
            .iter()
            .any(|path| source_record_matches_path(&entry, vault_root, path))
        {
            records.push(entry);
        }
    }
    Ok(records)
}

pub fn source_record_matches_path(entry: &SourceRecord, vault_root: &Path, path: &Path) -> bool {
    let location = normalize_path_text(&entry.location);
    let absolute = normalize_path_text(&path.to_string_lossy());
    let relative = path
        .strip_prefix(vault_root)
        .map(|path| normalize_path_text(&path.to_string_lossy()))
        .unwrap_or_else(|_| absolute.clone());

    if location == relative || location == absolute {
        return true;
    }
    // The record's canonical raw asset (`raw/<id>.md`) also identifies the
    // source: selecting a source by id resolves to that path, not to
    // `location` (which may be a URL or an off-vault file path).
    crate::paths::raw_source_path(&entry.id)
        .map(|raw| normalize_path_text(&raw.to_string_lossy()))
        .is_ok_and(|raw| raw == relative || raw == absolute)
}

fn render_source_citation(entry: &SourceRecord) -> String {
    let fetched_at = parse_timestamp(&entry.fetched_at);
    let offset = local_offset_for(&fetched_at);
    render_source_citation_at_offset(entry, &fetched_at, offset)
}

#[cfg(test)]
fn render_source_citation_with_offset(entry: &SourceRecord, offset: FixedOffset) -> String {
    let fetched_at = parse_timestamp(&entry.fetched_at);
    render_source_citation_at_offset(entry, &fetched_at, offset)
}

fn render_source_citation_at_offset(
    entry: &SourceRecord,
    fetched_at: &ParsedTimestamp,
    offset: FixedOffset,
) -> String {
    let mut parts = Vec::new();
    let primary = entry.citation.clone().unwrap_or_else(|| {
        entry
            .title
            .clone()
            .unwrap_or_else(|| entry.location.clone())
    });
    let primary_is_location = normalize_path_text(&primary) == normalize_path_text(&entry.location);
    parts.push(primary);
    if !primary_is_location {
        parts.push(format!("Source: {}", entry.location));
    }
    parts.push(format!("Kind: {}", entry.kind));
    parts.push(format!("Fetched: {}", format_timestamp(fetched_at, offset)));
    if let Some(license) = &entry.license {
        parts.push(format!("License: {license}"));
    }
    parts.push(format!("Hash: {}", entry.content_hash));

    join_citation_parts(&parts)
}

fn join_citation_parts(parts: &[String]) -> String {
    let mut rendered = String::new();
    for part in parts {
        let part = part.trim();
        if part.is_empty() {
            continue;
        }
        if !rendered.is_empty() {
            if rendered.ends_with(['.', '!', '?']) {
                rendered.push(' ');
            } else {
                rendered.push_str(". ");
            }
        }
        rendered.push_str(part);
    }
    rendered
}

fn normalize_path_text(value: &str) -> String {
    value.trim().replace('\\', "/")
}

#[cfg(test)]
mod tests {
    use chrono::FixedOffset;

    use super::*;
    use crate::sources::{SourceDraft, SourceManifest};

    fn source_record(fetched_at: &str) -> SourceRecord {
        SourceRecord {
            id: "src".to_string(),
            location: "raw/research/source.md".to_string(),
            canonical_location: "raw/research/source.md".to_string(),
            kind: crate::sources::SourceKind::Url,
            fetched_at: fetched_at.to_string(),
            last_verified_at: fetched_at.to_string(),
            fetch_provenance: crate::sources::FetchProvenance::Stub,
            content_hash: "hash".to_string(),
            title: None,
            citation: Some("Source citation".to_string()),
            license: None,
            ingestion_method: crate::sources::IngestionMethod::Manual,
            compile_status: crate::sources::CompileStatus::Pending,
            replay: None,
        }
    }

    #[test]
    fn citation_timestamps_use_injected_offsets_and_preserve_source_precision() {
        let cases = [
            (
                FixedOffset::west_opt(7 * 60 * 60).expect("valid negative offset"),
                "2026-07-04 18:30 -07:00 (unix-ms:1783215000000)",
            ),
            (
                FixedOffset::east_opt(5 * 60 * 60 + 30 * 60).expect("valid positive offset"),
                "2026-07-05 07:00 +05:30 (unix-ms:1783215000000)",
            ),
        ];

        for (offset, expected_instant) in cases {
            for instant in ["unix-ms:1783215000000", "2026-07-05T01:30:00Z"] {
                let rendered = render_source_citation_with_offset(&source_record(instant), offset);
                assert!(
                    rendered.contains(&format!("Fetched: {expected_instant}")),
                    "{rendered}"
                );
            }
            for preserved in ["2026-07-05", "2026-07-05 (approximate)", "last tuesday"] {
                let rendered =
                    render_source_citation_with_offset(&source_record(preserved), offset);
                assert!(
                    rendered.contains(&format!("Fetched: {preserved}")),
                    "{rendered}"
                );
            }
        }
    }

    #[test]
    fn renders_source_citations() {
        let temp = tempfile::tempdir().expect("tempdir");
        SourceManifest::register(
            temp.path(),
            SourceDraft::url(
                "raw/research/compile.md",
                "2026-05-29T15:00:00Z",
                b"Compile evidence".to_vec(),
            )
            .with_title("Compile Evidence")
            .with_citation("Example Docs, Compile Evidence")
            .with_license("CC-BY-4.0"),
        )
        .expect("source registered");

        let citations =
            render_citations(temp.path(), &[temp.path().join("raw/research/compile.md")])
                .expect("citations rendered");

        assert_eq!(citations.len(), 1);
        assert!(citations[0].contains("Example Docs, Compile Evidence"));
        assert!(citations[0].contains("raw/research/compile.md"));
        assert!(citations[0].contains("CC-BY-4.0"));
        assert!(citations[0].contains(" (unix-ms:"));
    }

    #[test]
    fn citation_renderer_does_not_add_wrapper_punctuation() {
        let entry = SourceRecord {
            id: "src".to_string(),
            location: "raw/research/source.md".to_string(),
            canonical_location: "raw/research/source.md".to_string(),
            kind: crate::sources::SourceKind::Url,
            fetched_at: "2026-05-29T15:00:00Z".to_string(),
            last_verified_at: "2026-05-29T15:00:00Z".to_string(),
            fetch_provenance: crate::sources::FetchProvenance::Stub,
            content_hash: "hash".to_string(),
            title: None,
            citation: Some("Already punctuated.".to_string()),
            license: None,
            ingestion_method: crate::sources::IngestionMethod::Manual,
            compile_status: crate::sources::CompileStatus::Pending,
            replay: None,
        };

        let rendered = render_source_citation(&entry);

        assert!(rendered.starts_with("Already punctuated. Source:"));
        assert!(!rendered.contains(".. Source"));
        assert!(!rendered.ends_with('.'));
    }

    #[test]
    fn citation_renderer_does_not_duplicate_location() {
        let entry = SourceRecord {
            id: "src".to_string(),
            location: "raw/research/source.md".to_string(),
            canonical_location: "raw/research/source.md".to_string(),
            kind: crate::sources::SourceKind::Url,
            fetched_at: "2026-05-29T15:00:00Z".to_string(),
            last_verified_at: "2026-05-29T15:00:00Z".to_string(),
            fetch_provenance: crate::sources::FetchProvenance::Stub,
            content_hash: "hash".to_string(),
            title: None,
            citation: Some("raw/research/source.md".to_string()),
            license: None,
            ingestion_method: crate::sources::IngestionMethod::Manual,
            compile_status: crate::sources::CompileStatus::Pending,
            replay: None,
        };

        let rendered = render_source_citation(&entry);

        assert_eq!(rendered.matches("raw/research/source.md").count(), 1);
    }
}
