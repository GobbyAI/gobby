//! Ingestion helpers for immutable raw wiki sources.

pub mod audio;
pub mod file;
pub mod git;
pub mod image;
pub mod mediawiki;
pub mod pdf;
pub mod url;
pub mod video;
pub mod wayback;

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

pub(crate) fn write_raw_then_index(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    record: SourceRecord,
    markdown: &str,
    asset_path: Option<PathBuf>,
) -> Result<IngestResult, WikiError> {
    let raw_path = write_raw_markdown(vault_root, &record, markdown)?;
    index_after_ingest(vault_root, store)?;

    Ok(IngestResult {
        record,
        raw_path,
        asset_path,
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

pub(crate) fn path_to_string(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

fn write_immutable(vault_root: &Path, relative: &Path, bytes: &[u8]) -> Result<(), WikiError> {
    let path = vault_root.join(relative);
    if path.exists() {
        let existing_hash =
            gobby_core::indexing::file_content_hash(&path).map_err(|error| WikiError::Io {
                action: "hash existing raw source",
                path: Some(path.clone()),
                source: error.to_string(),
            })?;
        if existing_hash == gobby_core::indexing::content_hash(bytes) {
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
    use crate::ingest::wayback::{self, WaybackCaptureSnapshot};
    use crate::sources::{CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest};
    use crate::store::{
        MemoryWikiStore, StoreError, WikiChunk, WikiDocument, WikiIndexStore, WikiIngestion,
        WikiIngestionEvent, WikiLink, WikiSource,
    };

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

    #[derive(Debug, Default)]
    struct RawFirstStore {
        vault_root: PathBuf,
        expected_raw_path: PathBuf,
        inner: MemoryWikiStore,
        observed_index_write: bool,
    }

    impl RawFirstStore {
        fn new(vault_root: &Path, expected_raw_path: impl Into<PathBuf>) -> Self {
            Self {
                vault_root: vault_root.to_path_buf(),
                expected_raw_path: expected_raw_path.into(),
                inner: MemoryWikiStore::default(),
                observed_index_write: false,
            }
        }

        fn assert_raw_exists_before_index(&mut self) {
            self.observed_index_write = true;
            assert!(
                self.vault_root.join(&self.expected_raw_path).is_file(),
                "external connector must write raw source before derived index rows"
            );
        }
    }

    impl WikiIndexStore for RawFirstStore {
        fn indexed_hashes(
            &mut self,
        ) -> Result<std::collections::BTreeMap<PathBuf, String>, StoreError> {
            self.inner.indexed_hashes()
        }

        fn upsert_document(&mut self, document: WikiDocument) -> Result<(), StoreError> {
            self.assert_raw_exists_before_index();
            self.inner.upsert_document(document)
        }

        fn replace_chunks(
            &mut self,
            path: &Path,
            chunks: Vec<WikiChunk>,
        ) -> Result<(), StoreError> {
            self.assert_raw_exists_before_index();
            self.inner.replace_chunks(path, chunks)
        }

        fn replace_links(&mut self, path: &Path, links: Vec<WikiLink>) -> Result<(), StoreError> {
            self.assert_raw_exists_before_index();
            self.inner.replace_links(path, links)
        }

        fn upsert_source(&mut self, source: WikiSource) -> Result<(), StoreError> {
            self.assert_raw_exists_before_index();
            self.inner.upsert_source(source)
        }

        fn record_ingestion(&mut self, ingestion: WikiIngestion) -> Result<(), StoreError> {
            self.assert_raw_exists_before_index();
            self.inner.record_ingestion(ingestion)
        }

        fn record_file_hash(
            &mut self,
            path: PathBuf,
            content_hash: String,
        ) -> Result<(), StoreError> {
            self.assert_raw_exists_before_index();
            self.inner.record_file_hash(path, content_hash)
        }

        fn delete_derived_rows(&mut self, path: &Path) -> Result<(), StoreError> {
            self.assert_raw_exists_before_index();
            self.inner.delete_derived_rows(path)
        }
    }

    #[test]
    fn external_connectors_write_raw_first() {
        let temp = tempfile::tempdir().expect("tempdir");
        let snapshot = WaybackCaptureSnapshot {
            original_url: "https://example.com/reference".to_string(),
            capture_url: "https://web.archive.org/web/20260529120000/https://example.com/reference"
                .to_string(),
            capture_timestamp: "20260529120000".to_string(),
            fetched_at: "2026-05-29T18:30:00Z".to_string(),
            body: b"<html><body>Archived reference.</body></html>".to_vec(),
            content_type: Some("text/html".to_string()),
        };
        let expected_record = SourceManifest::register(
            temp.path(),
            SourceDraft {
                location: snapshot.capture_url.clone(),
                kind: SourceKind::Wayback,
                fetched_at: snapshot.fetched_at.clone(),
                content: snapshot.body.clone(),
                title: Some(snapshot.original_url.clone()),
                citation: Some(snapshot.capture_url.clone()),
                license: None,
                ingestion_method: IngestionMethod::Manual,
                compile_status: CompileStatus::Pending,
            },
        )
        .expect("predict wayback record");
        let expected_raw_path = PathBuf::from("raw").join(format!("{}.md", expected_record.id));
        std::fs::remove_file(temp.path().join("raw/INDEX.md")).expect("remove predicted manifest");
        let mut store = RawFirstStore::new(temp.path(), expected_raw_path);

        wayback::ingest_capture(temp.path(), &mut store, snapshot).expect("ingest wayback");

        assert!(store.observed_index_write);
    }
}
