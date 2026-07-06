use std::path::{Path, PathBuf};

use crate::WikiError;
use crate::paths;
use crate::sources::SourceRecord;

pub(crate) fn raw_source_path(id: &str) -> Result<PathBuf, WikiError> {
    paths::raw_source_path(id)
}

/// Returns every vault-relative file belonging to `record`: raw capture,
/// derived digest page, and stored assets.
pub(crate) fn source_record_paths(
    vault_root: &Path,
    record: &SourceRecord,
) -> Result<Vec<PathBuf>, WikiError> {
    paths::source_record_paths(vault_root, record)
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
