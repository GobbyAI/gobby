//! Git-aware file discovery using the `ignore` crate.
//! Respects .gitignore and exclude patterns.

use std::collections::HashSet;
use std::path::{Path, PathBuf};

use ignore::WalkBuilder;

use crate::index::languages;
use crate::index::security;

/// Content-only extensions: chunked for FTS but not AST-parsed.
const CONTENT_EXTENSIONS: &[&str] = &[
    ".txt", ".cfg", ".ini", ".toml", ".conf", ".xml", ".html", ".htm", ".css", ".scss", ".less",
    ".sql", ".sh", ".bash", ".zsh", ".fish", ".bat", ".ps1",
];

/// Discover files eligible for indexing under `root`.
/// Returns (ast_candidates, content_only_candidates) as absolute paths.
pub fn discover_files(root: &Path, exclude_patterns: &[String]) -> (Vec<PathBuf>, Vec<PathBuf>) {
    let supported = languages::supported_extensions();
    let content_exts: HashSet<&str> = CONTENT_EXTENSIONS.iter().copied().collect();

    let mut candidates = Vec::new();
    let mut content_only = Vec::new();

    let walker = WalkBuilder::new(root)
        .hidden(true)
        .git_ignore(true)
        .git_global(true)
        .git_exclude(true)
        .build();

    for entry in walker.flatten() {
        let path = entry.path();
        if !path.is_file() {
            continue;
        }

        let ext = path
            .extension()
            .map(|e| format!(".{}", e.to_string_lossy().to_lowercase()))
            .unwrap_or_default();

        let is_supported = supported.contains(ext.as_str());
        let is_content = content_exts.contains(ext.as_str());

        if !is_supported && !is_content {
            continue;
        }

        if security::should_exclude(path, exclude_patterns) {
            continue;
        }

        if is_supported {
            candidates.push(path.to_path_buf());
        } else {
            content_only.push(path.to_path_buf());
        }
    }

    (candidates, content_only)
}
