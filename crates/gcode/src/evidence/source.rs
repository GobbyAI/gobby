//! Commit-bound blobs and indexed working-tree reads.
use super::contracts::{EvidenceItem, EvidenceWarning};
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

    /// Blobs at the binding commit, one result per path in order.
    fn bound_blobs(&self, paths: &[&str]) -> Vec<Result<Vec<u8>>> {
        if paths.is_empty() {
            return Vec::new();
        }
        let verified = self.bound_commit.get_or_init(|| {
            super::provenance::verify_bound_commit(&self.repository_root, &self.binding)
        });
        match verified {
            Ok(()) => {
                super::provenance::read_bound_blobs(&self.repository_root, &self.binding, paths)
            }
            Err(detail) => paths
                .iter()
                .map(|path| {
                    Err(super::provenance::bound_blob_unavailable(
                        &self.binding,
                        path,
                        detail.clone(),
                    ))
                })
                .collect(),
        }
    }

    pub(super) fn read_file(&self, path: &str) -> Result<Vec<u8>> {
        validate_repo_path(path)?;
        if !self.working_tree {
            let mut blobs = self.bound_blobs(&[path]);
            return blobs.pop().expect("one result per requested path");
        }
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

    pub(super) fn verify_source_hash(
        &self,
        path: &str,
        expected: &str,
        bytes: &[u8],
    ) -> Result<()> {
        let found = gobby_core::indexing::content_hash(bytes);
        if found != expected {
            return Err(EvidenceError::StaleRange {
                path: path.to_string(),
                detail: format!(
                    "source hash {found} at binding {} differs from indexed {expected}",
                    self.binding.commit_oid
                ),
            });
        }
        Ok(())
    }

    /// Marks each cited path whose working-tree bytes are not the bytes at the
    /// binding commit, which an unbound request records as HEAD.
    pub(super) fn dirty_warnings(&self, items: &[EvidenceItem]) -> Vec<EvidenceWarning> {
        if !self.working_tree {
            return Vec::new();
        }
        let cited = items
            .iter()
            .filter_map(|item| match item {
                EvidenceItem::Source(source) => Some((&*source.path, &*source.content_hash)),
                EvidenceItem::Graph(graph) => {
                    Some((&*graph.source.path, &*graph.source.content_hash))
                }
                _ => None,
            })
            .collect::<std::collections::BTreeMap<_, _>>();
        let paths = cited.keys().copied().collect::<Vec<_>>();
        let bound = self.bound_blobs(&paths);
        cited
            .into_iter()
            .zip(bound)
            .filter_map(|((path, hash), bound)| {
                let state = match bound {
                    Ok(bytes) if gobby_core::indexing::content_hash(&bytes) == hash => return None,
                    Ok(_) => "differ from",
                    Err(_) => "are absent from or unreadable at",
                };
                let commit = &self.binding.commit_oid;
                Some(EvidenceWarning {
                    code: "binding_dirty".to_string(),
                    message: format!(
                        "{path} excerpt bytes come from the working tree and {state} \
                         binding commit {commit}"
                    ),
                    path: Some(path.to_string()),
                })
            })
            .collect()
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
