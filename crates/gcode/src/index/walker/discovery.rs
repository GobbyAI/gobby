use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use crate::index::MAX_FILE_SIZE;

use super::classification::classify_file;
use super::hidden::HiddenPathAllowlist;
use super::types::{DiscoveryOptions, FileClassification};

/// Discover files eligible for indexing under `root`.
/// Returns (ast_candidates, content_only_candidates) as absolute paths.
pub fn discover_files<S: AsRef<str>>(
    root: &Path,
    exclude_patterns: &[S],
) -> (Vec<PathBuf>, Vec<PathBuf>) {
    discover_files_with_options(root, exclude_patterns, DiscoveryOptions::default())
}

pub fn discover_files_with_options<S: AsRef<str>>(
    root: &Path,
    exclude_patterns: &[S],
    options: DiscoveryOptions,
) -> (Vec<PathBuf>, Vec<PathBuf>) {
    let mut candidates = Vec::new();
    let mut content_only = Vec::new();
    let mut seen = BTreeSet::new();

    let mut settings = gobby_core::indexing::WalkerSettings::new(root);
    settings.respect_gitignore = options.respect_gitignore;
    settings.max_filesize = Some(MAX_FILE_SIZE);
    let mut builder = settings.into_walker();
    builder.hidden(true);
    let walker = builder.build();

    for entry in walker.flatten() {
        let path = entry.path();
        if !path.is_file() {
            continue;
        }

        push_classified_file(
            root,
            path,
            exclude_patterns,
            &mut candidates,
            &mut content_only,
            &mut seen,
        );
    }

    let hidden_allowlist = HiddenPathAllowlist::load(root);
    for path in hidden_allowlist.discover(root) {
        push_classified_file(
            root,
            &path,
            exclude_patterns,
            &mut candidates,
            &mut content_only,
            &mut seen,
        );
    }

    (candidates, content_only)
}

fn push_classified_file(
    root: &Path,
    path: &Path,
    exclude_patterns: &[impl AsRef<str>],
    candidates: &mut Vec<PathBuf>,
    content_only: &mut Vec<PathBuf>,
    seen: &mut BTreeSet<PathBuf>,
) {
    let key = path.canonicalize().unwrap_or_else(|_| path.to_path_buf());
    if !seen.insert(key) {
        return;
    }

    match classify_file(root, path, exclude_patterns) {
        Some(FileClassification::Ast) => candidates.push(path.to_path_buf()),
        Some(FileClassification::ContentOnly) => content_only.push(path.to_path_buf()),
        None => {}
    }
}
