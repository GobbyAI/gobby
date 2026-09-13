pub mod api;
#[cfg(test)]
mod api_tests;
pub(crate) mod checkout_fence;
pub mod chunker;
pub mod hasher;
pub mod import_resolution;
pub mod indexer;
pub mod languages;
pub mod parser;
pub mod security;
pub mod semantic;
pub mod walker;

pub(crate) fn normalize_storage_path(path: &std::path::Path) -> String {
    normalize_storage_path_str(&path.to_string_lossy())
}

/// Separator normalization for storage keys: rewrites `\` to `/` only on
/// Windows, where it is a path separator. On Unix a backslash is a legal
/// filename character and must survive so every layer (indexer, import
/// resolution, walker matching) keys the file identically.
pub(crate) fn normalize_storage_path_str(path: &str) -> String {
    #[cfg(windows)]
    {
        path.replace('\\', "/")
    }
    #[cfg(not(windows))]
    {
        path.to_owned()
    }
}

/// Maximum file size to index (10 MB).
pub(crate) const MAX_FILE_SIZE: u64 = 10 * 1024 * 1024;

/// Maximum size for a data-language (JSON/YAML) file to be AST-parsed (1 MiB).
///
/// Data languages emit one `property` symbol per key, so a large generated
/// blob parses to tens of thousands of symbols that bloat the PostgreSQL hub,
/// FalkorDB graph, and Qdrant vectors. Files above this size are indexed
/// content-only (BM25 chunks, zero symbols) instead. Hand-authored configs
/// (`Cargo.lock`, `package.json`, CI YAML) stay well under 1 MiB and keep their
/// per-key symbols (gobby-cli #678).
pub(crate) const MAX_DATA_LANGUAGE_AST_SIZE: u64 = 1024 * 1024;

#[cfg(test)]
mod tests {
    #[test]
    #[cfg(windows)]
    fn storage_paths_normalize_windows_backslashes() {
        let path = std::path::Path::new(r"src\nested\lib.rs");

        assert_eq!(super::normalize_storage_path(path), "src/nested/lib.rs");
        assert_eq!(
            super::normalize_storage_path_str(r"src\nested\lib.rs"),
            "src/nested/lib.rs"
        );
    }

    #[test]
    #[cfg(not(windows))]
    fn storage_path_preserves_unix_filename_backslashes() {
        let path = std::path::Path::new(r"src/name\with-backslash.rs");

        assert_eq!(
            super::normalize_storage_path(path),
            r"src/name\with-backslash.rs"
        );
    }

    #[test]
    #[cfg(not(windows))]
    fn storage_path_str_preserves_unix_backslashes() {
        assert_eq!(
            super::normalize_storage_path_str(r"src/name\with-backslash.rs"),
            r"src/name\with-backslash.rs"
        );
    }
}
