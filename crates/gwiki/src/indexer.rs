use std::collections::BTreeMap;
use std::fmt;
use std::path::{Component, Path, PathBuf};

use gobby_core::indexing::{
    Chunk, IndexEvent, WalkerSettings, file_content_hash, index_events_from_hashes,
};

use crate::links as wiki_links;
use crate::store::{
    StoreError, WikiChunk, WikiDocument, WikiDocumentKind, WikiIndexStore, WikiIngestion,
    WikiIngestionEvent, WikiLink, WikiSource, configured_memory_index_limit_bytes,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IndexOptions {
    pub respect_gitignore: bool,
}

impl Default for IndexOptions {
    fn default() -> Self {
        Self {
            respect_gitignore: true,
        }
    }
}

#[derive(Debug)]
pub enum IndexError {
    Io(std::io::Error),
    Walk(String),
    Store(StoreError),
    PathOutsideVault { path: PathBuf, vault_root: PathBuf },
    MemoryIndexTooLarge { total_bytes: u64, limit_bytes: u64 },
}

impl fmt::Display for IndexError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(f, "wiki index I/O failed: {error}"),
            Self::Walk(error) => write!(f, "wiki index walk failed: {error}"),
            Self::Store(error) => write!(f, "{error}"),
            Self::PathOutsideVault { path, vault_root } => write!(
                f,
                "wiki index path {} is outside vault {}",
                path.display(),
                vault_root.display()
            ),
            Self::MemoryIndexTooLarge {
                total_bytes,
                limit_bytes,
            } => write!(
                f,
                "wiki memory index input is {total_bytes} bytes, exceeding {limit_bytes} bytes from GWIKI_MAX_MEMORY_INDEX_BYTES"
            ),
        }
    }
}

impl std::error::Error for IndexError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io(source) => Some(source),
            Self::Store(source) => Some(source),
            _ => None,
        }
    }
}

impl From<StoreError> for IndexError {
    fn from(error: StoreError) -> Self {
        Self::Store(error)
    }
}

impl From<std::io::Error> for IndexError {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error)
    }
}

pub fn index_vault(
    vault_root: impl AsRef<Path>,
    store: &mut impl WikiIndexStore,
) -> Result<(), IndexError> {
    index_vault_with_options(vault_root, store, IndexOptions::default())
}

pub fn index_vault_with_options(
    vault_root: impl AsRef<Path>,
    store: &mut impl WikiIndexStore,
    options: IndexOptions,
) -> Result<(), IndexError> {
    let vault_root = vault_root.as_ref();
    let previous_hashes = store.indexed_hashes()?;
    let current_hashes = discover_indexable_hashes(vault_root, options)?;

    for event in index_events_from_hashes(&previous_hashes, &current_hashes) {
        match event {
            IndexEvent::Added(path) => {
                index_file(
                    vault_root,
                    store,
                    path,
                    WikiIngestionEvent::Added,
                    &current_hashes,
                )?;
            }
            IndexEvent::Changed(path) => {
                index_file(
                    vault_root,
                    store,
                    path,
                    WikiIngestionEvent::Changed,
                    &current_hashes,
                )?;
            }
            IndexEvent::Deleted(path) => {
                store.delete_derived_rows(&path)?;
                store.record_ingestion(WikiIngestion {
                    path,
                    event: WikiIngestionEvent::Deleted,
                    content_hash: None,
                })?;
            }
            IndexEvent::Unchanged(path) => {
                let content_hash = current_hashes
                    .get(&path)
                    .or_else(|| previous_hashes.get(&path))
                    .cloned();
                store.record_ingestion(WikiIngestion {
                    path,
                    event: WikiIngestionEvent::Unchanged,
                    content_hash,
                })?;
            }
            IndexEvent::Skipped { path, .. } => {
                store.record_ingestion(WikiIngestion {
                    path,
                    event: WikiIngestionEvent::Skipped,
                    content_hash: None,
                })?;
            }
        }
    }

    Ok(())
}

fn discover_indexable_hashes(
    vault_root: &Path,
    options: IndexOptions,
) -> Result<BTreeMap<PathBuf, String>, IndexError> {
    discover_indexable_hashes_with_limit(vault_root, configured_memory_index_limit_bytes(), options)
}

fn discover_indexable_hashes_with_limit(
    vault_root: &Path,
    memory_limit: Option<u64>,
    options: IndexOptions,
) -> Result<BTreeMap<PathBuf, String>, IndexError> {
    let mut current_hashes = BTreeMap::new();
    let mut settings = WalkerSettings::new(vault_root);
    settings.respect_gitignore = options.respect_gitignore;
    let walker = settings.into_walker().build();
    let mut total_indexable_bytes = 0u64;

    for entry in walker {
        let entry = entry.map_err(|error| IndexError::Walk(error.to_string()))?;
        let path = entry.path();
        if !path.is_file() {
            continue;
        }

        let relative = path
            .strip_prefix(vault_root)
            .map_err(|_| IndexError::PathOutsideVault {
                path: path.to_path_buf(),
                vault_root: vault_root.to_path_buf(),
            })?;

        if is_indexable_vault_path(relative) {
            total_indexable_bytes = total_indexable_bytes.saturating_add(path.metadata()?.len());
            if let Some(limit_bytes) = memory_limit
                && total_indexable_bytes > limit_bytes
            {
                return Err(IndexError::MemoryIndexTooLarge {
                    total_bytes: total_indexable_bytes,
                    limit_bytes,
                });
            }
            current_hashes.insert(relative.to_path_buf(), file_content_hash(path)?);
        }
    }

    Ok(current_hashes)
}

fn index_file(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    path: PathBuf,
    event: WikiIngestionEvent,
    current_hashes: &BTreeMap<PathBuf, String>,
) -> Result<(), IndexError> {
    let content_hash = current_hashes
        .get(&path)
        .ok_or_else(|| {
            IndexError::Walk(format!(
                "index event path {} was missing from current hash snapshot",
                path.display()
            ))
        })?
        .clone();
    let body = std::fs::read_to_string(vault_root.join(&path))?;
    let kind = document_kind(&path).ok_or_else(|| {
        IndexError::Walk(format!(
            "indexable path {} had no document kind",
            path.display()
        ))
    })?;
    let parsed = parse_wiki_document(&path, kind, content_hash.clone(), body);

    store.upsert_document(parsed.document)?;
    store.replace_chunks(&path, parsed.chunks)?;
    store.replace_links(&path, parsed.links)?;
    store.upsert_source(parsed.source)?;
    store.record_file_hash(path.clone(), content_hash.clone())?;
    store.record_ingestion(WikiIngestion {
        path,
        event,
        content_hash: Some(content_hash),
    })?;

    Ok(())
}

fn is_indexable_vault_path(path: &Path) -> bool {
    if path == Path::new("raw").join("INDEX.md") {
        return true;
    }

    if path.extension().and_then(|extension| extension.to_str()) != Some("md") {
        return false;
    }

    let components = normal_components(path);
    matches!(components.as_slice(), ["code", ..] | ["knowledge", ..])
}

fn document_kind(path: &Path) -> Option<WikiDocumentKind> {
    if path == Path::new("raw").join("INDEX.md") {
        return Some(WikiDocumentKind::SourceCatalog);
    }

    match normal_components(path).as_slice() {
        ["code", ..] => Some(WikiDocumentKind::CodeDoc),
        ["knowledge", "sources", ..] => Some(WikiDocumentKind::SourceNote),
        ["knowledge", "concepts", ..] => Some(WikiDocumentKind::Concept),
        ["knowledge", "topics", ..] => Some(WikiDocumentKind::Topic),
        ["knowledge", ..] => Some(WikiDocumentKind::Concept),
        _ => None,
    }
}

fn normal_components(path: &Path) -> Vec<&str> {
    path.components()
        .filter_map(|component| match component {
            Component::Normal(value) => value.to_str(),
            _ => None,
        })
        .collect()
}

struct ParsedWikiDocument {
    document: WikiDocument,
    chunks: Vec<WikiChunk>,
    links: Vec<WikiLink>,
    source: WikiSource,
}

fn parse_wiki_document(
    path: &Path,
    kind: WikiDocumentKind,
    content_hash: String,
    body: String,
) -> ParsedWikiDocument {
    let title = first_heading(&body);
    let core_chunks = chunks_for_markdown(path, title.clone(), &body);
    let chunks = core_chunks
        .into_iter()
        .enumerate()
        .map(|(chunk_index, chunk)| WikiChunk {
            path: chunk.file_path,
            chunk_index,
            byte_start: chunk.byte_start,
            byte_end: chunk.byte_end,
            heading: chunk.heading,
            content: body
                .get(chunk.byte_start..chunk.byte_end)
                .unwrap_or_default()
                .to_string(),
        })
        .collect();
    let links = extract_links(path, &body);

    ParsedWikiDocument {
        document: WikiDocument {
            path: path.to_path_buf(),
            kind,
            title,
            content_hash: content_hash.clone(),
            body,
        },
        chunks,
        links,
        source: WikiSource {
            path: path.to_path_buf(),
            document_path: path.to_path_buf(),
            kind,
            content_hash,
        },
    }
}

fn chunks_for_markdown(path: &Path, fallback_heading: Option<String>, body: &str) -> Vec<Chunk> {
    let heading_spans = heading_spans(body);
    if heading_spans.is_empty() {
        return vec![Chunk {
            file_path: path.to_path_buf(),
            byte_start: 0,
            byte_end: body.len(),
            heading: fallback_heading,
            metadata: serde_json::Value::Null,
        }];
    }

    heading_spans
        .iter()
        .enumerate()
        .map(|(index, (byte_start, heading))| {
            let byte_end = heading_spans
                .get(index + 1)
                .map(|(next_start, _)| *next_start)
                .unwrap_or(body.len());
            Chunk {
                file_path: path.to_path_buf(),
                byte_start: *byte_start,
                byte_end,
                heading: Some(heading.clone()),
                metadata: serde_json::Value::Null,
            }
        })
        .collect()
}

fn heading_spans(body: &str) -> Vec<(usize, String)> {
    let mut spans = Vec::new();
    let mut byte_start = 0;

    for line in body.split_inclusive('\n') {
        if let Some(heading) = parse_heading(line) {
            spans.push((byte_start, heading));
        }
        byte_start += line.len();
    }

    spans
}

fn first_heading(body: &str) -> Option<String> {
    body.lines().find_map(parse_heading)
}

fn parse_heading(line: &str) -> Option<String> {
    crate::markdown::parse_atx_heading(line)
        .map(|(_, heading)| heading)
        .filter(|heading| !heading.is_empty())
}

fn extract_links(path: &Path, body: &str) -> Vec<WikiLink> {
    let mut links = wiki_links::extract_links(body, std::iter::empty::<&str>())
        .into_iter()
        .map(|link| WikiLink {
            path: path.to_path_buf(),
            target: link.target,
            alias: link.alias,
            byte_start: link.byte_start,
            byte_end: link.byte_end,
        })
        .collect::<Vec<_>>();
    links.sort_by_key(|link| link.byte_start);
    links
}

#[cfg(test)]
mod tests {
    use std::path::{Path, PathBuf};

    use gobby_core::indexing::content_hash;

    use super::*;
    use crate::store::{
        MemoryWikiStore, WikiDocument, WikiDocumentKind, WikiIngestionEvent, WikiLink, WikiSource,
    };

    fn write_file(root: &Path, relative: &str, contents: &str) {
        let path = root.join(relative);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).expect("create parent");
        }
        std::fs::write(path, contents).expect("write file");
    }

    fn seed_derived_rows(store: &mut MemoryWikiStore, relative: &str) {
        let path = PathBuf::from(relative);
        store.documents.insert(
            path.clone(),
            WikiDocument {
                path: path.clone(),
                kind: WikiDocumentKind::Topic,
                title: Some("Stale".to_string()),
                content_hash: "old".to_string(),
                body: "stale".to_string(),
            },
        );
        store.chunks.insert(path.clone(), Vec::new());
        store.links.insert(
            path.clone(),
            vec![WikiLink {
                path: path.clone(),
                target: "Old".to_string(),
                alias: None,
                byte_start: 0,
                byte_end: 7,
            }],
        );
        store.sources.insert(
            path.clone(),
            WikiSource {
                path: path.clone(),
                document_path: path.clone(),
                kind: WikiDocumentKind::Topic,
                content_hash: "old".to_string(),
            },
        );
        store.file_hashes.insert(path, "old".to_string());
    }

    #[test]
    fn index_writer_upserts_documents_chunks_links_sources_and_ingestions() {
        let tempdir = tempfile::tempdir().expect("tempdir");
        write_file(
            tempdir.path(),
            "knowledge/topics/rust.md",
            "# Rust\n\nSee [[Ownership|ownership]] and [Cargo](knowledge/concepts/cargo.md).\n",
        );
        let mut store = MemoryWikiStore::default();

        index_vault(tempdir.path(), &mut store).expect("index vault");

        let path = PathBuf::from("knowledge/topics/rust.md");
        assert_eq!(store.documents[&path].kind, WikiDocumentKind::Topic);
        assert!(!store.chunks[&path].is_empty());
        assert_eq!(store.links[&path].len(), 2);
        assert_eq!(store.sources[&path].kind, WikiDocumentKind::Topic);
        assert_eq!(store.ingestions[0].event, WikiIngestionEvent::Added);
    }

    #[test]
    fn index_vault_respects_gitignore_by_default_and_option() {
        let tempdir = tempfile::tempdir().expect("tempdir");
        std::fs::create_dir(tempdir.path().join(".git")).expect("git dir");
        write_file(
            tempdir.path(),
            ".gitignore",
            "knowledge/topics/ignored.md\n",
        );
        write_file(tempdir.path(), "knowledge/topics/visible.md", "# Visible\n");
        write_file(tempdir.path(), "knowledge/topics/ignored.md", "# Ignored\n");

        let mut default_store = MemoryWikiStore::default();
        index_vault(tempdir.path(), &mut default_store).expect("default index vault");
        assert!(
            default_store
                .documents
                .contains_key(&PathBuf::from("knowledge/topics/visible.md"))
        );
        assert!(
            !default_store
                .documents
                .contains_key(&PathBuf::from("knowledge/topics/ignored.md"))
        );

        let mut disabled_store = MemoryWikiStore::default();
        index_vault_with_options(
            tempdir.path(),
            &mut disabled_store,
            IndexOptions {
                respect_gitignore: false,
            },
        )
        .expect("disabled gitignore index vault");
        assert!(
            disabled_store
                .documents
                .contains_key(&PathBuf::from("knowledge/topics/ignored.md"))
        );
    }

    #[test]
    fn deleted_file_removes_derived_rows_only() {
        let tempdir = tempfile::tempdir().expect("tempdir");
        write_file(tempdir.path(), "raw/source.txt", "raw source stays\n");
        let raw_before = std::fs::read_to_string(tempdir.path().join("raw/source.txt"))
            .expect("read raw source");
        let mut store = MemoryWikiStore::default();
        seed_derived_rows(&mut store, "knowledge/topics/stale.md");

        index_vault(tempdir.path(), &mut store).expect("index vault");

        let stale = PathBuf::from("knowledge/topics/stale.md");
        assert!(!store.documents.contains_key(&stale));
        assert!(!store.chunks.contains_key(&stale));
        assert!(!store.links.contains_key(&stale));
        assert!(!store.sources.contains_key(&stale));
        assert!(!store.file_hashes.contains_key(&stale));
        assert_eq!(store.deleted_paths, vec![stale]);
        assert_eq!(
            std::fs::read_to_string(tempdir.path().join("raw/source.txt"))
                .expect("read raw source after indexing"),
            raw_before
        );
    }

    #[test]
    fn raw_sources_are_immutable() {
        let tempdir = tempfile::tempdir().expect("tempdir");
        write_file(tempdir.path(), "raw/article.txt", "original raw bytes\n");
        write_file(
            tempdir.path(),
            "raw/INDEX.md",
            "# Sources\n\n- article.txt\n",
        );
        let raw_path = tempdir.path().join("raw/article.txt");
        let raw_before = std::fs::read_to_string(&raw_path).expect("read raw source");
        let mut store = MemoryWikiStore::default();

        index_vault(tempdir.path(), &mut store).expect("index vault");

        assert_eq!(
            std::fs::read_to_string(raw_path).expect("read raw source after indexing"),
            raw_before
        );
        assert!(store.documents.contains_key(&PathBuf::from("raw/INDEX.md")));
        assert!(
            !store
                .documents
                .contains_key(&PathBuf::from("raw/article.txt"))
        );
    }

    #[test]
    fn unchanged_files_are_skipped() {
        let tempdir = tempfile::tempdir().expect("tempdir");
        let body = "# Stable\n\nNo changes.\n";
        write_file(tempdir.path(), "knowledge/concepts/stable.md", body);
        let mut store = MemoryWikiStore::default();

        index_vault(tempdir.path(), &mut store).expect("first index");
        assert_eq!(
            store.file_hashes[&PathBuf::from("knowledge/concepts/stable.md")],
            content_hash(body.as_bytes())
        );
        let document_upserts = store.document_upserts;
        let chunk_replacements = store.chunk_replacements;
        let link_replacements = store.link_replacements;
        let source_upserts = store.source_upserts;

        index_vault(tempdir.path(), &mut store).expect("second index");

        assert_eq!(store.document_upserts, document_upserts);
        assert_eq!(store.chunk_replacements, chunk_replacements);
        assert_eq!(store.link_replacements, link_replacements);
        assert_eq!(store.source_upserts, source_upserts);
        assert_eq!(
            store
                .ingestions
                .last()
                .expect("unchanged ingestion recorded")
                .event,
            WikiIngestionEvent::Unchanged
        );
    }

    #[test]
    fn indexes_codedocs_with_provenance() {
        let tempdir = tempfile::tempdir().expect("tempdir");
        write_file(
            tempdir.path(),
            "code/crates/gwiki/src/indexer.md",
            "---\nprovenance:\n  - file: crates/gwiki/src/indexer.rs\n---\n# Indexer\n\nSee [[code/crates/gwiki/src/store|store docs]].\n",
        );
        let mut store = MemoryWikiStore::default();

        index_vault(tempdir.path(), &mut store).expect("index vault");

        let path = PathBuf::from("code/crates/gwiki/src/indexer.md");
        let document = store.documents.get(&path).expect("codedoc document");
        assert_eq!(document.kind, WikiDocumentKind::CodeDoc);
        assert!(document.body.contains("provenance:"));
        assert_eq!(store.sources[&path].kind, WikiDocumentKind::CodeDoc);
        assert_eq!(store.links[&path].len(), 1);
        assert_eq!(store.links[&path][0].target, "code/crates/gwiki/src/store");
        assert_eq!(store.links[&path][0].alias.as_deref(), Some("store docs"));
        assert_eq!(store.ingestions[0].event, WikiIngestionEvent::Added);
    }

    #[test]
    fn unified_vault_indexes_code_root_wikilinks() {
        let tempdir = tempfile::tempdir().expect("tempdir");
        write_file(
            tempdir.path(),
            "code/modules/src.md",
            "---\nsource:\n  - file: src/lib.rs\n    ranges: [1-2]\nprovenance:\n  - file: src/lib.rs\n    ranges: [1-2]\ngenerated_by: gcode-codewiki\ntrust: generated\nfreshness: indexed\n---\n# src\n\nSee [[code/files/src/lib.rs|src/lib.rs]].\n",
        );
        write_file(
            tempdir.path(),
            "code/files/src/lib.rs.md",
            "# src/lib.rs\n\nModule: [[code/modules/src|src]].\n",
        );
        let mut store = MemoryWikiStore::default();

        index_vault(tempdir.path(), &mut store).expect("index unified vault");

        let module_path = PathBuf::from("code/modules/src.md");
        let file_path = PathBuf::from("code/files/src/lib.rs.md");
        assert_eq!(
            store.documents[&module_path].kind,
            WikiDocumentKind::CodeDoc
        );
        assert_eq!(store.documents[&file_path].kind, WikiDocumentKind::CodeDoc);
        assert_eq!(store.links[&module_path][0].target, "code/files/src/lib.rs");
        assert_eq!(
            store.links[&module_path][0].alias.as_deref(),
            Some("src/lib.rs")
        );
        assert_eq!(store.links[&file_path][0].target, "code/modules/src");
        assert_eq!(store.sources[&module_path].kind, WikiDocumentKind::CodeDoc);
    }

    #[test]
    fn codedoc_tree_indexes_idempotently() {
        let tempdir = tempfile::tempdir().expect("tempdir");
        write_file(tempdir.path(), "code/a.md", "# A\n\nSee [[B]].\n");
        write_file(tempdir.path(), "code/nested/b.md", "# B\n\nStable.\n");
        write_file(tempdir.path(), "code/nested/ignored.txt", "not markdown\n");
        let mut store = MemoryWikiStore::default();

        index_vault(tempdir.path(), &mut store).expect("first index");

        let a_path = PathBuf::from("code/a.md");
        let b_path = PathBuf::from("code/nested/b.md");
        assert_eq!(store.documents[&a_path].kind, WikiDocumentKind::CodeDoc);
        assert_eq!(store.documents[&b_path].kind, WikiDocumentKind::CodeDoc);
        assert!(
            !store
                .documents
                .contains_key(&PathBuf::from("code/nested/ignored.txt"))
        );
        assert_eq!(store.document_upserts, 2);
        assert_eq!(store.ingestions.len(), 2);

        write_file(tempdir.path(), "code/a.md", "# A\n\nChanged.\n");
        index_vault(tempdir.path(), &mut store).expect("second index");

        assert_eq!(store.document_upserts, 3);
        assert_eq!(store.chunk_replacements, 3);
        assert_eq!(store.link_replacements, 3);
        assert_eq!(store.source_upserts, 3);
        assert_eq!(store.ingestions[2].path, a_path);
        assert_eq!(store.ingestions[2].event, WikiIngestionEvent::Changed);
        assert_eq!(store.ingestions[3].path, b_path);
        assert_eq!(store.ingestions[3].event, WikiIngestionEvent::Unchanged);
        assert_eq!(store.links[&b_path].len(), 0);
    }

    #[test]
    fn memory_index_limit_rejects_large_vaults() {
        let temp = tempfile::tempdir().expect("tempdir");
        write_file(
            temp.path(),
            "knowledge/topics/large.md",
            "# Large\n\nabcdef\n",
        );

        let error =
            discover_indexable_hashes_with_limit(temp.path(), Some(4), IndexOptions::default())
                .expect_err("limit rejects vault");

        assert!(matches!(error, IndexError::MemoryIndexTooLarge { .. }));
    }
}
