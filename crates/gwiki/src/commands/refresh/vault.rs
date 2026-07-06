use std::path::{Path, PathBuf};

use crate::WikiError;
use crate::paths;

pub(crate) fn raw_source_path(id: &str) -> Result<PathBuf, WikiError> {
    paths::raw_source_path(id)
}

/// Returns vault-relative raw asset paths whose file stem matches `id`.
///
/// `vault_root` is the vault root and `id` is trimmed before matching. Missing
/// `raw/assets` directories and unmatched IDs return an empty vector. Directory
/// read failures are returned as `WikiError::Io`.
pub(crate) fn source_asset_paths_for_id(
    vault_root: &Path,
    id: &str,
) -> Result<Vec<PathBuf>, WikiError> {
    paths::source_asset_paths_for_id(vault_root, id)
}

pub(crate) fn remove_relative_file(
    vault_root: &Path,
    relative_path: &Path,
) -> Result<bool, WikiError> {
    paths::remove_relative_file(vault_root, relative_path)
}

pub(crate) fn ensure_scope_root(root: &Path) -> Result<(), WikiError> {
    if root.is_dir() {
        Ok(())
    } else {
        Err(WikiError::NotFound {
            resource: "wiki scope",
            id: root.display().to_string(),
        })
    }
}
