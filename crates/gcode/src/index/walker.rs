//! Git-aware file discovery using the `ignore` crate.
//! Respects .gitignore and exclude patterns.

use std::path::{Path, PathBuf};

use crate::index::languages;
use crate::index::security;

/// Maximum file size to index (10 MB).
const MAX_FILE_SIZE: u64 = 10 * 1024 * 1024;
const SKIPPED_EXTENSIONS: &[&str] = &["mjs", "md", "markdown"];

/// How a file should be indexed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FileClassification {
    Ast,
    ContentOnly,
}

/// Discover files eligible for indexing under `root`.
/// Returns (ast_candidates, content_only_candidates) as absolute paths.
pub fn discover_files(root: &Path, exclude_patterns: &[String]) -> (Vec<PathBuf>, Vec<PathBuf>) {
    let mut candidates = Vec::new();
    let mut content_only = Vec::new();

    let mut settings = gobby_core::indexing::WalkerSettings::new(root);
    settings.max_filesize = Some(MAX_FILE_SIZE);
    let mut builder = settings.into_walker();
    builder.hidden(true);
    let walker = builder.build();

    for entry in walker.flatten() {
        let path = entry.path();
        if !path.is_file() {
            continue;
        }

        match classify_file(root, path, exclude_patterns) {
            Some(FileClassification::Ast) => candidates.push(path.to_path_buf()),
            Some(FileClassification::ContentOnly) => content_only.push(path.to_path_buf()),
            None => {}
        }
    }

    (candidates, content_only)
}

/// Classify an individual file for indexing.
pub fn classify_file(
    root: &Path,
    path: &Path,
    exclude_patterns: &[String],
) -> Option<FileClassification> {
    if has_skipped_extension(path) {
        return None;
    }

    if !is_safe_text_file(root, path, exclude_patterns) {
        return None;
    }

    if languages::detect_language(&path.to_string_lossy()).is_some() {
        Some(FileClassification::Ast)
    } else {
        Some(FileClassification::ContentOnly)
    }
}

/// Return true when `path` is an unsupported, safe text file suitable for chunks.
pub fn is_content_indexable(root: &Path, path: &Path, exclude_patterns: &[String]) -> bool {
    matches!(
        classify_file(root, path, exclude_patterns),
        Some(FileClassification::ContentOnly)
    )
}

/// Language label for content-only files.
pub fn content_language(path: &Path) -> String {
    path.extension()
        .map(|e| e.to_string_lossy().to_lowercase())
        .filter(|ext| !ext.is_empty())
        .unwrap_or_else(|| "text".to_string())
}

fn has_skipped_extension(path: &Path) -> bool {
    path.extension()
        .and_then(|ext| ext.to_str())
        .is_some_and(|ext| {
            SKIPPED_EXTENSIONS
                .iter()
                .any(|skipped| ext.eq_ignore_ascii_case(skipped))
        })
}

fn is_safe_text_file(root: &Path, path: &Path, exclude_patterns: &[String]) -> bool {
    if !path.is_file() {
        return false;
    }
    if !security::validate_path(path, root) {
        return false;
    }
    if !security::is_symlink_safe(path, root) {
        return false;
    }
    if security::should_exclude_path(root, path, exclude_patterns) {
        return false;
    }
    if security::has_secret_extension(path) {
        return false;
    }

    let Ok(meta) = path.metadata() else {
        return false;
    };
    if meta.len() == 0 || meta.len() > MAX_FILE_SIZE {
        return false;
    }

    !security::is_binary(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write_file(root: &Path, rel: &str, contents: &[u8]) {
        let path = root.join(rel);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).expect("create parent");
        }
        std::fs::write(path, contents).expect("write file");
    }

    fn rels(root: &Path, paths: Vec<PathBuf>) -> Vec<String> {
        let mut rels: Vec<String> = paths
            .into_iter()
            .map(|path| {
                path.strip_prefix(root)
                    .expect("path under root")
                    .to_string_lossy()
                    .to_string()
            })
            .collect();
        rels.sort();
        rels
    }

    #[test]
    fn discovers_ast_and_content_only_text_files() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        write_file(root, "README.md", b"# Title\n");
        write_file(root, "skills/gcode/SKILL.md", b"# gcode\n");
        write_file(root, "src/lib.rs", b"fn main() {}\n");
        write_file(root, "src/generated.mjs", b"export const value = 1;\n");
        write_file(root, "docs/reference.markdown", b"# Reference\n");
        write_file(root, "docs/guide.rst", b"Guide\n=====\n");
        write_file(root, "notes.txt", b"plain notes\n");
        write_file(root, "config/app.properties", b"mode=dev\n");
        write_file(root, "config/app.toml", b"mode = 'dev'\n");
        write_file(root, "scripts/setup.sh", b"#!/usr/bin/env bash\n");
        write_file(root, "Dockerfile", b"FROM rust:latest\n");
        write_file(root, "image.bin", b"PNG\0binary");
        write_file(root, "api_key.txt", b"secret-ish\n");
        write_file(root, "target/generated.txt", b"generated\n");

        let excludes = vec!["target".to_string()];
        let (ast, content_only) = discover_files(root, &excludes);

        // discover_files omits api_key.txt via the security module
        // (SECRET_SUBSTRINGS matches "api_key"), image.bin via binary
        // detection, and target/* via the explicit excludes vector.
        assert_eq!(rels(root, ast), vec!["src/lib.rs"]);
        assert_eq!(
            rels(root, content_only),
            vec![
                "Dockerfile",
                "config/app.properties",
                "config/app.toml",
                "docs/guide.rst",
                "notes.txt",
                "scripts/setup.sh"
            ]
        );
    }

    #[test]
    fn classifies_extensionless_text_as_content_only() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        write_file(root, "Makefile", b"test:\n\tcargo test\n");
        let excludes = Vec::new();

        assert_eq!(
            classify_file(root, &root.join("Makefile"), &excludes),
            Some(FileClassification::ContentOnly)
        );
        assert_eq!(content_language(&root.join("Makefile")), "text");
    }

    #[test]
    fn classifies_mjs_and_markdown_as_skipped() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        write_file(root, "src/generated.mjs", b"export const value = 1;\n");
        write_file(root, "README.md", b"# Title\n");
        write_file(root, "docs/guide.markdown", b"# Guide\n");
        let excludes = Vec::new();

        assert_eq!(
            classify_file(root, &root.join("src/generated.mjs"), &excludes),
            None
        );
        assert_eq!(
            classify_file(root, &root.join("README.md"), &excludes),
            None
        );
        assert_eq!(
            classify_file(root, &root.join("docs/guide.markdown"), &excludes),
            None
        );
    }

    #[test]
    fn classifies_source_build_directory_as_ast_indexable() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        write_file(
            root,
            "src/gobby/build/workspaces.py",
            b"class WorkspaceBuilder:\n    pass\n",
        );
        let excludes = vec!["build".to_string(), "dist".to_string()];

        assert_eq!(
            classify_file(root, &root.join("src/gobby/build/workspaces.py"), &excludes),
            Some(FileClassification::Ast)
        );
    }

    #[test]
    fn skips_root_build_directory() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        write_file(root, "build/generated.py", b"class Generated:\n    pass\n");
        let excludes = vec!["build".to_string(), "dist".to_string()];

        assert_eq!(
            classify_file(root, &root.join("build/generated.py"), &excludes),
            None
        );
    }

    #[test]
    fn walker_consumes_gobby_core_walker_settings() {
        let source = include_str!("walker.rs");
        let settings = ["gobby_core", "::indexing::WalkerSettings"].concat();
        let direct_builder = ["WalkBuilder", "::new(root)"].concat();

        assert!(source.contains(&settings));
        assert!(!source.contains(&direct_builder));
    }
}
