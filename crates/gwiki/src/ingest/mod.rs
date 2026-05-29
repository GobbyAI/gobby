//! Ingestion helpers for immutable raw wiki sources.

pub mod file;
pub mod pdf;
pub mod url;

use std::path::{Path, PathBuf};

use crate::WikiError;
use crate::indexer;
use crate::sources::SourceRecord;
use crate::store::WikiIndexStore;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IngestResult {
    pub record: SourceRecord,
    pub raw_path: PathBuf,
    pub asset_path: Option<PathBuf>,
}

pub(crate) fn write_raw_markdown(
    vault_root: &Path,
    record: &SourceRecord,
    markdown: &str,
) -> Result<PathBuf, WikiError> {
    let raw_path = PathBuf::from("raw").join(format!("{}.md", record.id));
    write_immutable(vault_root, &raw_path, markdown.as_bytes())?;
    Ok(raw_path)
}

pub(crate) fn write_asset(
    vault_root: &Path,
    record: &SourceRecord,
    file_name: &str,
    bytes: &[u8],
) -> Result<PathBuf, WikiError> {
    let extension = Path::new(file_name)
        .extension()
        .and_then(|value| value.to_str())
        .map(sanitize_extension)
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "bin".to_string());
    let asset_path = PathBuf::from("raw")
        .join("assets")
        .join(format!("{}.{}", record.id, extension));
    write_immutable(vault_root, &asset_path, bytes)?;
    Ok(asset_path)
}

pub(crate) fn index_after_ingest(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
) -> Result<(), WikiError> {
    indexer::index_vault(vault_root, store).map_err(|error| WikiError::InvalidInput {
        field: "vault_root",
        message: error.to_string(),
    })
}

pub(crate) fn markdown_metadata(fields: &[(&str, String)]) -> String {
    let mut metadata = String::from("---\n");
    for (key, value) in fields {
        metadata.push_str(key);
        metadata.push_str(": ");
        metadata.push_str(&single_line(value));
        metadata.push('\n');
    }
    metadata.push_str("---\n\n");
    metadata
}

pub(crate) fn single_line(value: &str) -> String {
    value.split_whitespace().collect::<Vec<_>>().join(" ")
}

pub(crate) fn markdown_title(value: &str) -> String {
    single_line(value).trim_matches('#').trim().to_string()
}

pub(crate) fn text_from_utf8_lossy(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).replace("\r\n", "\n")
}

fn write_immutable(vault_root: &Path, relative: &Path, bytes: &[u8]) -> Result<(), WikiError> {
    let path = vault_root.join(relative);
    if path.exists() {
        let existing = std::fs::read(&path).map_err(|error| WikiError::Io {
            action: "read existing raw source",
            path: Some(path.clone()),
            source: error.to_string(),
        })?;
        if existing == bytes {
            return Ok(());
        }
        return Err(WikiError::InvalidInput {
            field: "raw_path",
            message: format!(
                "immutable raw source already exists at {}",
                relative.display()
            ),
        });
    }

    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create raw source directory",
            path: Some(parent.to_path_buf()),
            source: error.to_string(),
        })?;
    }
    std::fs::write(&path, bytes).map_err(|error| WikiError::Io {
        action: "write raw source",
        path: Some(path),
        source: error.to_string(),
    })
}

fn sanitize_extension(extension: &str) -> String {
    extension
        .chars()
        .filter(|ch| ch.is_ascii_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect()
}

#[cfg(test)]
mod tests {
    use std::path::{Path, PathBuf};

    use gobby_core::indexing::content_hash;

    use crate::ingest::file;
    use crate::store::{MemoryWikiStore, WikiIngestionEvent};

    fn write_file(root: &std::path::Path, relative: &str, contents: &str) {
        let path = root.join(relative);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).expect("create parent");
        }
        std::fs::write(path, contents).expect("write file");
    }

    #[test]
    fn ingest_indexes_raw_without_wiki_rewrite() {
        let temp = tempfile::tempdir().expect("tempdir");
        let wiki_body = "# Existing Topic\n\nUser-authored notes stay intact.\n";
        write_file(temp.path(), "wiki/topics/existing.md", wiki_body);
        let source_path = temp.path().join("capture.txt");
        std::fs::write(&source_path, "captured source text\n").expect("write source");

        let mut store = MemoryWikiStore::default();
        store.file_hashes.insert(
            PathBuf::from("wiki/topics/existing.md"),
            content_hash(wiki_body.as_bytes()),
        );
        let before = std::fs::read_to_string(temp.path().join("wiki/topics/existing.md"))
            .expect("read existing before");

        let result = file::ingest_path(
            temp.path(),
            &mut store,
            &source_path,
            "2026-05-29T17:00:00Z",
        )
        .expect("ingest path");

        let after = std::fs::read_to_string(temp.path().join("wiki/topics/existing.md"))
            .expect("read existing after");
        assert_eq!(after, before);
        assert!(temp.path().join(&result.raw_path).is_file());
        assert!(store.documents.contains_key(&PathBuf::from("raw/INDEX.md")));
        assert!(
            !store
                .documents
                .contains_key(&PathBuf::from("wiki/topics/existing.md"))
        );
        assert!(store.ingestions.iter().any(|ingestion| {
            ingestion.path == Path::new("raw/INDEX.md")
                && ingestion.event == WikiIngestionEvent::Added
        }));
    }
}
