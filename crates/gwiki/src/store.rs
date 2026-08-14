mod helpers;
mod postgres;
#[cfg(test)]
mod test_fake;
mod types;

pub use postgres::PostgresWikiStore;
#[cfg(test)]
pub(crate) use test_fake::FakeWikiStore;
pub use types::{
    StoreError, WikiChunk, WikiDocument, WikiDocumentKind, WikiIndexStore, WikiIngestion,
    WikiIngestionEvent, WikiLink, WikiSource, WikiStoreScope,
};

pub const MAX_MEMORY_INDEX_BYTES_ENV: &str = "GWIKI_MAX_MEMORY_INDEX_BYTES";

pub fn configured_memory_index_limit_bytes() -> Option<u64> {
    helpers::configured_memory_index_limit_bytes()
}

pub(crate) fn link_kind(target: &str) -> &'static str {
    helpers::link_kind(target)
}

#[cfg(test)]
use helpers::{HASH_SUFFIX_LEN, MAX_ID_LEN, cap_scoped_id_with_hash, scoped_text_id};

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::{Path, PathBuf};

    #[test]
    fn link_kind_classifies_uri_schemes_and_fragments() {
        assert_eq!(link_kind("https://example.test"), "markdown");
        assert_eq!(link_kind("mailto:hello@example.test"), "markdown");
        assert_eq!(link_kind("tel:+15551234567"), "markdown");
        assert_eq!(link_kind("//example.test/path"), "markdown");
        assert_eq!(link_kind("#local-section"), "wiki");
        assert_eq!(link_kind("Concept Page"), "wiki");
    }

    #[test]
    fn scoped_ids_are_capped_with_deterministic_hash_suffix() {
        let scope = WikiStoreScope::project("project-with-a-very-long-identifier");
        let id = scoped_text_id(
            "chunk",
            &scope,
            Path::new("knowledge/topics/a-very-long-path-name-that-keeps-going.md"),
            &["1234567890"],
        );
        let id_again = scoped_text_id(
            "chunk",
            &scope,
            Path::new("knowledge/topics/a-very-long-path-name-that-keeps-going.md"),
            &["1234567890"],
        );

        assert!(id.len() <= MAX_ID_LEN);
        assert_eq!(id, id_again);
        assert!(
            id.rsplit_once('-')
                .is_some_and(|(_, suffix)| suffix.len() == HASH_SUFFIX_LEN)
        );
    }

    #[test]
    fn scoped_id_capping_tolerates_short_hashes() {
        let id = cap_scoped_id_with_hash("x".repeat(MAX_ID_LEN + 20), "abc");

        assert!(id.len() <= MAX_ID_LEN);
        assert!(id.ends_with("-abc"));
    }

    #[test]
    fn memory_store_rejects_path_mismatches() {
        let mut store = FakeWikiStore::default();
        let err = store
            .replace_chunks(
                Path::new("knowledge/topics/page.md"),
                vec![WikiChunk {
                    path: PathBuf::from("knowledge/topics/other.md"),
                    chunk_index: 0,
                    byte_start: 0,
                    byte_end: 4,
                    heading: None,
                    content: "body".to_string(),
                }],
            )
            .expect_err("mismatched chunk path must fail");

        assert!(matches!(
            err,
            StoreError::InvalidData {
                field: "chunk.path",
                ..
            }
        ));

        let err = store
            .replace_links(
                Path::new("knowledge/topics/page.md"),
                vec![WikiLink {
                    path: PathBuf::from("knowledge/topics/other.md"),
                    target: "Target".to_string(),
                    alias: None,
                    byte_start: 0,
                    byte_end: 8,
                }],
            )
            .expect_err("mismatched link path must fail");

        assert!(matches!(
            err,
            StoreError::InvalidData {
                field: "link.path",
                ..
            }
        ));
    }

    #[test]
    fn memory_wiki_store_module_is_gone() {
        let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
        assert!(
            !manifest.join("src/store/memory.rs").exists(),
            "MemoryWikiStore production module must be deleted"
        );
        let store_root = include_str!("store.rs");
        let needle = format!("{}{}", "Memory", "WikiStore");
        let production = store_root
            .split("#[cfg(test)]")
            .next()
            .expect("production store.rs");
        assert!(
            !production.contains(&needle),
            "store.rs must not export MemoryWikiStore"
        );
        let memory_mod = format!("mod {};", "memory");
        assert!(
            !production.contains(&memory_mod),
            "store.rs must not declare the memory module"
        );
        let mut hits = Vec::new();
        let crate_root = manifest.join("src");
        let mut stack = vec![crate_root];
        while let Some(dir) = stack.pop() {
            let Ok(entries) = std::fs::read_dir(&dir) else {
                continue;
            };
            for entry in entries.flatten() {
                let path = entry.path();
                if path.is_dir() {
                    stack.push(path);
                    continue;
                }
                if path.extension().and_then(|ext| ext.to_str()) != Some("rs") {
                    continue;
                }
                if path.ends_with("store.rs") {
                    continue;
                }
                let Ok(source) = std::fs::read_to_string(&path) else {
                    continue;
                };
                if source.contains(&needle) {
                    hits.push(path.display().to_string());
                }
            }
        }
        assert!(
            hits.is_empty(),
            "workspace still names MemoryWikiStore: {hits:?}"
        );
    }

    #[test]
    fn memory_store_keys_sources_by_document_path() {
        let mut store = FakeWikiStore::default();
        let document_path = PathBuf::from("knowledge/sources/example.md");
        let source = WikiSource {
            path: PathBuf::from("raw/example.md"),
            document_path: document_path.clone(),
            kind: WikiDocumentKind::SourceNote,
            content_hash: "hash".to_string(),
        };

        store.upsert_source(source).expect("source upsert");

        assert!(store.sources.contains_key(&document_path));
        assert!(!store.sources.contains_key(Path::new("raw/example.md")));
    }
}
