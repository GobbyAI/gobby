use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use crate::index::languages;
use crate::index::security;
use crate::index::{MAX_DATA_LANGUAGE_AST_SIZE, MAX_FILE_SIZE};

use super::generated::is_generated_js_bundle;
use super::hidden::{HiddenPathContext, is_hidden_metadata_content_only, is_hidden_path};
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
    let context = HiddenPathContext::load(root);
    if !explicit_path_visible(root, path, options, &context) {
        return None;
    }
    classify_file(root, path, exclude_patterns)
}

/// Classify many explicitly requested files with one visibility pass.
///
/// Each result equals `classify_explicit_file_with_options` for the same path. The
/// hidden-path context loads once and one walk covers the whole batch, descending only
/// along ancestors of the candidates. Cost follows the candidate set instead of
/// rebuilding a root-scoped walker and canonicalizing ancestors for every file.
pub fn classify_explicit_files_with_options<S: AsRef<str>>(
    root: &Path,
    paths: &[PathBuf],
    exclude_patterns: &[S],
    options: DiscoveryOptions,
) -> Vec<Option<FileClassification>> {
    let context = HiddenPathContext::load(root);
    let visible = explicit_paths_visible(root, paths, options, &context);
    paths
        .iter()
        .zip(visible)
        .map(|(path, visible)| {
            if visible {
                classify_file(root, path, exclude_patterns)
            } else {
                None
            }
        })
        .collect()
}

/// Batch form of `explicit_path_visible`, returning one flag per input path.
fn explicit_paths_visible(
    root: &Path,
    paths: &[PathBuf],
    options: DiscoveryOptions,
    context: &HiddenPathContext,
) -> Vec<bool> {
    let mut visible = vec![false; paths.len()];
    let mut pending: HashMap<PathBuf, Vec<usize>> = HashMap::new();
    for (index, path) in paths.iter().enumerate() {
        if context.allowlist().matches(root, path) {
            visible[index] = true;
        } else if !is_hidden_path(root, path) {
            pending.entry(path.clone()).or_default().push(index);
        }
    }
    if pending.is_empty() {
        return visible;
    }

    // Every pending path and each of its ancestors may be descended into. Inserting
    // stops at the first ancestor already present, whose own ancestors are present too.
    let mut descend: HashSet<PathBuf> = HashSet::new();
    for path in pending.keys() {
        for ancestor in path.ancestors() {
            if !descend.insert(ancestor.to_path_buf()) {
                break;
            }
        }
    }

    let mut settings = gobby_core::indexing::WalkerSettings::new(root);
    settings.respect_gitignore = options.respect_gitignore;
    settings.max_filesize = Some(MAX_FILE_SIZE);
    let mut builder = settings.into_walker();
    builder.hidden(false);
    builder.filter_entry(move |entry| descend.contains(entry.path()));
    for entry in builder.build().flatten() {
        if !entry.path().is_file() {
            continue;
        }
        if let Some(indices) = pending.get(entry.path()) {
            for &index in indices {
                visible[index] = true;
            }
        }
    }
    visible
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

fn explicit_path_visible(
    root: &Path,
    path: &Path,
    options: DiscoveryOptions,
    context: &HiddenPathContext,
) -> bool {
    // Explicitly allowlisted metadata bypasses hidden filtering and gitignore,
    // matching discovery.
    if context.allowlist().matches(root, path) {
        return true;
    }
    if is_hidden_path(root, path) {
        return false;
    }

    let mut settings = gobby_core::indexing::WalkerSettings::new(root);
    settings.respect_gitignore = options.respect_gitignore;
    settings.max_filesize = Some(MAX_FILE_SIZE);
    let mut builder = settings.into_walker();
    builder.hidden(false);
    // Start at the repository boundary so ignored ancestors are admitted by
    // the same rules as discovery. Only descend along the requested path.
    let target = path.to_path_buf();
    builder.filter_entry(move |entry| {
        target.starts_with(entry.path()) || same_existing_path(entry.path(), &target)
    });
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
    // The lexical filters go first: an excluded path then costs no stat or
    // canonicalization.
    if !passes_path_filters(root, path, exclude_patterns) {
        return false;
    }
    if !path.is_file() {
        return false;
    }
    if !security::validate_path(path, root) {
        return false;
    }
    if !security::is_symlink_safe(path, root) {
        return false;
    }

    let Ok(meta) = path.metadata() else {
        return false;
    };
    if !indexable_len(meta.len()) {
        return false;
    }

    !security::is_binary(path)
}

/// The lexical filters: configured excludes and secret-looking names. They
/// read neither metadata nor file bytes.
pub(crate) fn passes_path_filters(
    root: &Path,
    path: &Path,
    exclude_patterns: &[impl AsRef<str>],
) -> bool {
    !security::should_exclude_path(root, path, exclude_patterns)
        && !security::has_secret_extension(path)
}

/// Whether a file of `len` bytes is inside the indexable size range.
pub(crate) fn indexable_len(len: u64) -> bool {
    len > 0 && len <= MAX_FILE_SIZE
}
