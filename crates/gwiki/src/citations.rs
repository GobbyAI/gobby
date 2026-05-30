use std::path::{Path, PathBuf};

use crate::WikiError;
use crate::sources::{SourceManifest, SourceRecord};

pub fn render_source_citations(
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

    location == relative || location == absolute
}

fn render_source_citation(entry: &SourceRecord) -> String {
    let mut parts = Vec::new();
    parts.push(entry.citation.clone().unwrap_or_else(|| {
        entry
            .title
            .clone()
            .unwrap_or_else(|| entry.location.clone())
    }));
    parts.push(format!("Source: {}", entry.location));
    parts.push(format!("Kind: {}", entry.kind));
    parts.push(format!("Fetched: {}", entry.fetched_at));
    if let Some(license) = &entry.license {
        parts.push(format!("License: {license}"));
    }
    parts.push(format!("Hash: {}", entry.content_hash));

    format!("{}.", parts.join(". "))
}

fn normalize_path_text(value: &str) -> String {
    value.trim().replace('\\', "/")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sources::{SourceDraft, SourceManifest};

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
            render_source_citations(temp.path(), &[temp.path().join("raw/research/compile.md")])
                .expect("citations rendered");

        assert_eq!(citations.len(), 1);
        assert!(citations[0].contains("Example Docs, Compile Evidence"));
        assert!(citations[0].contains("raw/research/compile.md"));
        assert!(citations[0].contains("CC-BY-4.0"));
        assert!(citations[0].contains("2026-05-29T15:00:00Z"));
    }
}
