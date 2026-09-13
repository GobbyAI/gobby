//! Live indexed file reads with content-hash and checkout containment checks.
use super::{EvidenceError, EvidenceLibrary, Result};
use crate::codewiki_facts::FileFact;
use std::io::Read as _;
use std::path::{Component, Path};

impl EvidenceLibrary {
    pub(super) fn entry(&self, path: &str) -> Result<&FileFact> {
        validate_repo_path(path)?;
        self.files
            .get(path)
            .ok_or_else(|| EvidenceError::InvalidSelector {
                detail: format!("path is not visible in the caller's index: {path}"),
            })
    }

    pub(super) fn verify_fact(&self, path: &str, content_hash: &str) -> Result<()> {
        let entry = self.entry(path)?;
        if entry.content_hash != content_hash {
            return Err(EvidenceError::FactMismatch {
                path: path.to_string(),
                expected: entry.content_hash.clone(),
                found: content_hash.to_string(),
            });
        }
        Ok(())
    }

    pub(super) fn read_file(&self, path: &str) -> Result<Vec<u8>> {
        let entry = self.entry(path)?;
        let stale = |error: std::io::Error| EvidenceError::StaleRange {
            path: path.to_string(),
            detail: error.to_string(),
        };
        let destination = self
            .repository_root
            .join(path)
            .canonicalize()
            .map_err(stale)?;
        if !destination.starts_with(&self.repository_root) {
            return Err(EvidenceError::UnsafePath {
                path: path.to_string(),
            });
        }
        let file = std::fs::File::open(&destination).map_err(stale)?;
        if !file.metadata().map_err(stale)?.is_file() {
            return Err(EvidenceError::InvalidSelector {
                detail: format!("not a file: {path}"),
            });
        }
        // Bound reads even if a file grows after indexing.
        let mut bytes = Vec::new();
        file.take(10 * 1024 * 1024 + 1)
            .read_to_end(&mut bytes)
            .map_err(stale)?;
        let hash = gobby_core::indexing::content_hash(&bytes);
        if bytes.len() > 10 * 1024 * 1024 || hash != entry.content_hash {
            return Err(EvidenceError::StaleRange {
                path: path.to_string(),
                detail: format!(
                    "working-tree hash {hash} differs from indexed {}",
                    entry.content_hash
                ),
            });
        }
        Ok(bytes)
    }
}

pub(crate) fn validate_repo_path(path: &str) -> Result<()> {
    let parsed = Path::new(path);
    if path.is_empty()
        || path.contains('\\')
        || parsed.is_absolute()
        || parsed.components().any(|component| {
            matches!(
                component,
                Component::ParentDir
                    | Component::CurDir
                    | Component::RootDir
                    | Component::Prefix(_)
            )
        })
    {
        return Err(EvidenceError::UnsafePath {
            path: path.to_string(),
        });
    }
    Ok(())
}

pub(crate) fn canonical_hash<T: serde::Serialize>(value: &T) -> Result<String> {
    let bytes = serde_json::to_vec(value).map_err(|error| EvidenceError::Contract {
        detail: error.to_string(),
    })?;
    Ok(gobby_core::indexing::content_hash(&bytes))
}
