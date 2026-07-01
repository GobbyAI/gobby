use std::path::Path;

use crate::index::languages;
use crate::index::security;
use crate::index::{MAX_DATA_LANGUAGE_AST_SIZE, MAX_FILE_SIZE};

use super::generated::is_generated_js_bundle;
use super::hidden::{
    HiddenPathAllowlist, is_generated_wiki_metadata, is_hidden_metadata_content_only,
    is_hidden_path,
};
use super::types::{DiscoveryOptions, FileClassification};

/// Classify an individual file for indexing.
pub fn classify_file(
    root: &Path,
    path: &Path,
    exclude_patterns: &[impl AsRef<str>],
) -> Option<FileClassification> {
    if !is_safe_text_file(root, path, exclude_patterns) {
        return None;
    }
    if is_generated_wiki_metadata(root, path) {
        return None;
    }
    if is_generated_js_bundle(path) {
        return None;
    }

    if is_hidden_metadata_content_only(root, path) {
        return Some(FileClassification::ContentOnly);
    }

    if let Some(lang) = languages::detect_language(&path.to_string_lossy()) {
        // Oversized data files (JSON/YAML) would emit one `property` symbol per
        // key; route them content-only so they don't bloat the graph/vector/FTS
        // projections. `is_safe_text_file` already bounded len to (0, MAX_FILE_SIZE],
        // so this is one extra `stat` on the data-language branch only (gobby-cli #678).
        if languages::is_data_language(lang)
            && path
                .metadata()
                .map(|m| m.len() > MAX_DATA_LANGUAGE_AST_SIZE)
                .unwrap_or(false)
        {
            Some(FileClassification::ContentOnly)
        } else {
            Some(FileClassification::Ast)
        }
    } else {
        Some(FileClassification::ContentOnly)
    }
}

/// Classify an explicitly requested file with discovery filters applied to that
/// one path instead of walking the whole project root.
pub fn classify_explicit_file_with_options(
    root: &Path,
    path: &Path,
    exclude_patterns: &[impl AsRef<str>],
    options: DiscoveryOptions,
) -> Option<FileClassification> {
    if options.respect_gitignore && !explicit_path_visible(root, path, options) {
        return None;
    }
    classify_file(root, path, exclude_patterns)
}

/// Return true when `path` is an unsupported, safe text file suitable for chunks.
pub fn is_content_indexable(
    root: &Path,
    path: &Path,
    exclude_patterns: &[impl AsRef<str>],
) -> bool {
    matches!(
        classify_file(root, path, exclude_patterns),
        Some(FileClassification::ContentOnly)
    )
}

/// Language label for content-only files.
pub fn content_language(path: &Path) -> String {
    let extension = path
        .extension()
        .map(|e| e.to_string_lossy().to_lowercase())
        .filter(|ext| !ext.is_empty())
        .unwrap_or_else(|| "text".to_string());

    match extension.as_str() {
        "md" | "markdown" => "markdown".to_string(),
        "yml" | "yaml" => "yaml".to_string(),
        _ => extension,
    }
}

fn explicit_path_visible(root: &Path, path: &Path, options: DiscoveryOptions) -> bool {
    if is_hidden_path(root, path) && !HiddenPathAllowlist::load(root).matches(root, path) {
        return false;
    }

    let walk_root = path.parent().unwrap_or(root);
    let mut settings = gobby_core::indexing::WalkerSettings::new(walk_root);
    settings.respect_gitignore = options.respect_gitignore;
    settings.max_filesize = Some(MAX_FILE_SIZE);
    let mut builder = settings.into_walker();
    builder.hidden(false);
    builder.max_depth(Some(1));
    builder
        .build()
        .flatten()
        .any(|entry| entry.path().is_file() && same_existing_path(entry.path(), path))
}

fn same_existing_path(left: &Path, right: &Path) -> bool {
    let left = left.canonicalize().unwrap_or_else(|_| left.to_path_buf());
    let right = right.canonicalize().unwrap_or_else(|_| right.to_path_buf());
    left == right
}

fn is_safe_text_file(root: &Path, path: &Path, exclude_patterns: &[impl AsRef<str>]) -> bool {
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
