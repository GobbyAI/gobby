use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use crate::index::MAX_FILE_SIZE;

use super::classification::classify_file;
use super::hidden::HiddenPathContext;
use super::types::{DiscoveryOptions, FileClassification};

/// Discover files eligible for indexing under `root`.
/// Returns (ast_candidates, content_only_candidates) as absolute paths.
#[cfg(test)]
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

    for file in walk_files(root, options) {
        push_classified_file(
            root,
            &file.path,
            exclude_patterns,
            &mut candidates,
            &mut content_only,
            &mut seen,
        );
    }

    (candidates, content_only)
}

/// A file the discovery walk yields, before classification.
pub(crate) struct WalkedFile {
    pub(crate) path: PathBuf,
    /// The walker reached a regular file without following a link. Under a
    /// canonical root, such a path is already canonical.
    pub(crate) direct: bool,
}

/// Every file discovery classifies, in discovery order: walker entries that
/// are regular files or links to one, then hidden-allowlist matches.
pub(crate) fn walk_files(root: &Path, options: DiscoveryOptions) -> Vec<WalkedFile> {
    let hidden_context = HiddenPathContext::load(root);

    let mut settings = gobby_core::indexing::WalkerSettings::new(root);
    settings.respect_gitignore = options.respect_gitignore;
    settings.max_filesize = Some(MAX_FILE_SIZE);
    let mut builder = settings.into_walker();
    builder.hidden(true);

    let mut files = Vec::new();
    for entry in builder.build().flatten() {
        let direct = entry.file_type().is_some_and(|kind| kind.is_file());
        if direct || entry.path().is_file() {
            files.push(WalkedFile {
                path: entry.into_path(),
                direct,
            });
        }
    }
    files.extend(
        hidden_context
            .allowlist()
            .discover(root)
            .into_iter()
            .map(|path| WalkedFile {
                path,
                direct: false,
            }),
    );
    files
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
