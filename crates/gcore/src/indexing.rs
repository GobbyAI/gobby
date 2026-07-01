//! Generic indexing primitives shared by indexing consumers.
//!
//! This module is available with the `indexing` feature. Domain-specific
//! parsers, symbol models, and graph facts stay in consumer crates.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;
use std::io::{self, Read};
use std::path::{Path, PathBuf};

use ignore::{WalkBuilder, overrides::OverrideBuilder};
use serde_json::Value;
use sha2::{Digest, Sha256};

/// Walker configuration that consumers can extend with domain-specific rules.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WalkerSettings {
    /// Root directory to walk.
    pub root: PathBuf,
    /// Whether to respect git ignore sources such as `.gitignore`.
    pub respect_gitignore: bool,
    /// Maximum file size to yield, in bytes.
    pub max_filesize: Option<u64>,
    /// Extra ignore patterns such as `*.pyc` or `node_modules/`.
    pub extra_ignores: Vec<String>,
}

impl WalkerSettings {
    /// Create walker settings with generic defaults consumers can extend.
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self {
            root: root.into(),
            respect_gitignore: true,
            max_filesize: None,
            extra_ignores: Vec::new(),
        }
    }

    /// Build an `ignore::WalkBuilder` from these settings.
    ///
    /// Panics when `extra_ignores` contains an invalid glob. Use
    /// [`try_into_walker`](Self::try_into_walker) to handle invalid patterns.
    pub fn into_walker(self) -> WalkBuilder {
        self.try_into_walker()
            .expect("invalid extra ignore pattern")
    }

    /// Build an `ignore::WalkBuilder`, returning invalid glob errors.
    pub fn try_into_walker(self) -> Result<WalkBuilder, ignore::Error> {
        let mut walker = WalkBuilder::new(&self.root);
        walker
            .git_ignore(self.respect_gitignore)
            .git_global(self.respect_gitignore)
            .git_exclude(self.respect_gitignore)
            .max_filesize(self.max_filesize);

        if !self.extra_ignores.is_empty() {
            let mut overrides = OverrideBuilder::new(&self.root);
            for pattern in self.extra_ignores {
                overrides.add(&format!("!{pattern}"))?;
            }
            walker.overrides(overrides.build()?);
        }

        Ok(walker)
    }
}

/// SHA-256 content hash for incremental indexing.
pub fn content_hash(data: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(data);
    hex_digest(hasher.finalize())
}

/// SHA-256 content hash for a file, read incrementally.
pub fn file_content_hash(path: impl AsRef<Path>) -> io::Result<String> {
    let mut file = std::fs::File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 65_536];

    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }

    Ok(hex_digest(hasher.finalize()))
}

fn hex_digest(digest: impl AsRef<[u8]>) -> String {
    let bytes = digest.as_ref();
    let mut hex = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        let _ = write!(&mut hex, "{byte:02x}");
    }
    hex
}

/// A content chunk with byte range and opaque domain metadata.
#[derive(Debug, Clone, PartialEq)]
pub struct Chunk {
    /// Path to the source file for the chunk.
    pub file_path: PathBuf,
    /// Inclusive byte offset where the chunk starts.
    pub byte_start: usize,
    /// Exclusive byte offset where the chunk ends.
    pub byte_end: usize,
    /// Optional human-readable heading associated with the chunk.
    pub heading: Option<String>,
    /// Opaque domain payload such as symbol refs or wiki links.
    pub metadata: Value,
}

/// Stable identity for a content chunk, independent of domain metadata.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct ChunkIdentity {
    /// Path to the source file for the chunk.
    pub file_path: PathBuf,
    /// Inclusive byte offset where the chunk starts.
    pub byte_start: usize,
    /// Exclusive byte offset where the chunk ends.
    pub byte_end: usize,
}

impl Chunk {
    /// Return the domain-independent identity for this chunk.
    pub fn identity(&self) -> ChunkIdentity {
        ChunkIdentity {
            file_path: self.file_path.clone(),
            byte_start: self.byte_start,
            byte_end: self.byte_end,
        }
    }
}

/// Index lifecycle events for incremental indexing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum IndexEvent {
    Added(PathBuf),
    Changed(PathBuf),
    Unchanged(PathBuf),
    Deleted(PathBuf),
    Skipped { path: PathBuf, reason: String },
}

/// Classify content-hash snapshots into deterministic incremental index events.
pub fn index_events_from_hashes(
    previous_hashes: &BTreeMap<PathBuf, String>,
    current_hashes: &BTreeMap<PathBuf, String>,
) -> Vec<IndexEvent> {
    let paths: BTreeSet<&PathBuf> = previous_hashes
        .keys()
        .chain(current_hashes.keys())
        .collect();

    paths
        .into_iter()
        .map(
            |path| match (previous_hashes.get(path), current_hashes.get(path)) {
                (None, Some(_)) => IndexEvent::Added(path.clone()),
                (Some(previous), Some(current)) if previous != current => {
                    IndexEvent::Changed(path.clone())
                }
                (Some(_), Some(_)) => IndexEvent::Unchanged(path.clone()),
                (Some(_), None) => IndexEvent::Deleted(path.clone()),
                (None, None) => unreachable!("path came from at least one snapshot"),
            },
        )
        .collect()
}

#[cfg(test)]
mod tests {
    use std::path::{Path, PathBuf};

    use serde_json::json;

    use super::*;

    fn write_file(root: &Path, rel: &str, contents: &[u8]) {
        let path = root.join(rel);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).expect("create parent");
        }
        std::fs::write(path, contents).expect("write file");
    }

    fn rels(root: &Path, settings: WalkerSettings) -> Vec<String> {
        let mut files: Vec<String> = settings
            .into_walker()
            .build()
            .flatten()
            .filter(|entry| entry.path().is_file())
            .map(|entry| {
                entry
                    .path()
                    .strip_prefix(root)
                    .expect("path under root")
                    .to_string_lossy()
                    .to_string()
            })
            .collect();
        files.sort();
        files
    }

    #[test]
    fn walker_settings_new_has_consumer_extendable_defaults() {
        let root = PathBuf::from("workspace");

        let settings = WalkerSettings::new(&root);

        assert_eq!(settings.root, root);
        assert!(settings.respect_gitignore);
        assert_eq!(settings.max_filesize, None);
        assert!(settings.extra_ignores.is_empty());
    }

    #[test]
    fn walker_settings_apply_generic_discovery_rules() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        write_file(root, ".gitignore", b"ignored.txt\n");
        write_file(root, "keep.txt", b"ok\n");
        write_file(root, "ignored.txt", b"ignored\n");
        write_file(root, "cache.pyc", b"pyc\n");
        write_file(root, "node_modules/pkg.js", b"pkg\n");
        write_file(root, "large.log", b"long\n");

        let settings = WalkerSettings {
            root: root.to_path_buf(),
            respect_gitignore: true,
            max_filesize: Some(3),
            extra_ignores: vec!["*.pyc".to_string(), "node_modules/".to_string()],
        };

        assert_eq!(rels(root, settings), vec!["keep.txt"]);
    }

    #[test]
    fn content_hash_returns_sha256_hex() {
        assert_eq!(
            content_hash(b"hello"),
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        );
    }

    #[test]
    fn file_content_hash_returns_sha256_hex() -> std::io::Result<()> {
        let tmp = tempfile::tempdir()?;
        let path = tmp.path().join("content.txt");
        std::fs::write(&path, b"hello")?;

        assert_eq!(
            file_content_hash(&path)?,
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        );
        Ok(())
    }

    #[test]
    fn chunk_metadata_is_opaque() {
        let metadata = json!({
            "symbols": ["module::Item"],
            "wiki_links": ["Indexing"],
            "consumer": {
                "domain": "docs",
                "score": 7
            }
        });

        let chunk = Chunk {
            file_path: PathBuf::from("docs/indexing.md"),
            byte_start: 12,
            byte_end: 48,
            heading: Some("Indexing".to_string()),
            metadata: metadata.clone(),
        };

        assert_eq!(chunk.metadata, metadata);
    }

    #[test]
    fn chunk_identity_uses_path_and_byte_range_only() {
        let base = Chunk {
            file_path: PathBuf::from("docs/indexing.md"),
            byte_start: 12,
            byte_end: 48,
            heading: Some("Indexing".to_string()),
            metadata: json!({ "consumer": "docs" }),
        };
        let same_identity = Chunk {
            file_path: PathBuf::from("docs/indexing.md"),
            byte_start: 12,
            byte_end: 48,
            heading: Some("Different heading".to_string()),
            metadata: json!({ "consumer": "wiki" }),
        };
        let different_range = Chunk {
            byte_end: 49,
            ..base.clone()
        };

        assert_eq!(base.identity(), same_identity.identity());
        assert_ne!(base.identity(), different_range.identity());
    }

    #[test]
    fn index_events_cover_incremental_cases() {
        let events = [
            IndexEvent::Added(PathBuf::from("added.md")),
            IndexEvent::Changed(PathBuf::from("changed.md")),
            IndexEvent::Unchanged(PathBuf::from("same.md")),
            IndexEvent::Deleted(PathBuf::from("deleted.md")),
            IndexEvent::Skipped {
                path: PathBuf::from("large.bin"),
                reason: "too large".to_string(),
            },
        ];

        assert!(matches!(events[0], IndexEvent::Added(_)));
        assert!(matches!(events[1], IndexEvent::Changed(_)));
        assert!(matches!(events[2], IndexEvent::Unchanged(_)));
        assert!(matches!(events[3], IndexEvent::Deleted(_)));
        assert!(matches!(
            &events[4],
            IndexEvent::Skipped { path, reason }
                if path == &PathBuf::from("large.bin") && reason == "too large"
        ));
    }

    #[test]
    fn index_events_from_hashes_classify_incremental_flow() {
        let previous = std::collections::BTreeMap::from([
            (PathBuf::from("changed.md"), "old".to_string()),
            (PathBuf::from("deleted.md"), "gone".to_string()),
            (PathBuf::from("same.md"), "same".to_string()),
        ]);
        let current = std::collections::BTreeMap::from([
            (PathBuf::from("added.md"), "new".to_string()),
            (PathBuf::from("changed.md"), "new".to_string()),
            (PathBuf::from("same.md"), "same".to_string()),
        ]);

        assert_eq!(
            index_events_from_hashes(&previous, &current),
            vec![
                IndexEvent::Added(PathBuf::from("added.md")),
                IndexEvent::Changed(PathBuf::from("changed.md")),
                IndexEvent::Deleted(PathBuf::from("deleted.md")),
                IndexEvent::Unchanged(PathBuf::from("same.md")),
            ]
        );
    }

    #[test]
    fn no_domain_parser_dependency() {
        let manifest = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/Cargo.toml"))
            .expect("read manifest");

        assert!(!manifest.contains("tree-sitter"));
    }

    #[test]
    fn manifest_keeps_indexing_feature_generic() {
        let manifest = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/Cargo.toml"))
            .expect("read manifest");

        let feature_sections = manifest
            .lines()
            .filter(|line| line.trim() == "[features]")
            .count();
        assert_eq!(
            feature_sections, 1,
            "gcore manifest should have exactly one [features] section"
        );

        let indexing_feature = manifest
            .lines()
            .find(|line| line.trim_start().starts_with("indexing = ["))
            .expect("indexing feature");
        for dependency in ["dep:ignore", "dep:sha2"] {
            assert!(
                indexing_feature.contains(&format!("\"{dependency}\"")),
                "indexing feature should include {dependency}"
            );
        }
        for forbidden in ["tree-sitter", "markdown", "wiki"] {
            assert!(
                !indexing_feature.contains(forbidden),
                "indexing feature should not include domain dependency {forbidden:?}"
            );
        }
    }
}
