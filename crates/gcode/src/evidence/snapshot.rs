use std::collections::BTreeMap;
use std::io::Write as _;
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};

use super::contracts::{
    ChangeStatus, ChangedPath, CommitBinding, ComparisonKind, EVIDENCE_SCHEMA_VERSION,
    ExclusionReason, InventoryEntry, SnapshotBinding, SnapshotInventory, TrackedFileKind,
};
use super::{EvidenceError, Result};

const MAX_EVIDENCE_FILE_BYTES: u64 = 10 * 1024 * 1024;

/// Verified access to immutable objects in one Git commit.
#[derive(Clone, Debug)]
pub struct Snapshot {
    repo_root: PathBuf,
    binding: SnapshotBinding,
    inventory: SnapshotInventory,
    entries: BTreeMap<String, InventoryEntry>,
}

impl Snapshot {
    /// Resolve an exact commit OID and prepare its complete canonical inventory.
    pub fn prepare(repo_root: &Path, project_id: &str, commit_oid: &str) -> Result<Self> {
        validate_oid(commit_oid)?;
        let repo_root = repo_root
            .canonicalize()
            .map_err(|error| EvidenceError::Git {
                operation: "canonicalize repository".to_string(),
                message: error.to_string(),
            })?;
        let commit_spec = format!("{commit_oid}^{{commit}}");
        git(&repo_root, &["cat-file", "-e", &commit_spec], None).map_err(|error| {
            EvidenceError::MissingGitObject {
                oid: commit_oid.to_string(),
                detail: error.to_string(),
            }
        })?;
        let resolved_commit_oid = git_text(&repo_root, &["rev-parse", &commit_spec])?;
        validate_oid(&resolved_commit_oid)?;
        let tree_spec = format!("{resolved_commit_oid}^{{tree}}");
        let tree_oid = git_text(&repo_root, &["rev-parse", &tree_spec])?;
        validate_oid(&tree_oid)?;

        let mut entries = load_inventory(&repo_root, &tree_oid)?;
        entries.sort();
        let inventory_digest = canonical_hash(&entries)?;
        let inventory = SnapshotInventory {
            schema_version: EVIDENCE_SCHEMA_VERSION,
            complete: true,
            digest: inventory_digest.clone(),
            entries,
        };
        let commit = load_commit_binding(&repo_root, &resolved_commit_oid)?;
        let binding = SnapshotBinding {
            project_id: project_id.to_string(),
            commit_oid: resolved_commit_oid,
            tree_oid,
            inventory_digest,
            commit,
        };
        Self::from_parts(repo_root, binding, inventory)
    }

    /// Recompute the exact Git inventory and reject drift in a supplied manifest.
    pub fn verify(
        repo_root: &Path,
        binding: SnapshotBinding,
        inventory: SnapshotInventory,
    ) -> Result<Self> {
        if !inventory.complete {
            return Err(EvidenceError::InventoryIncomplete);
        }
        if inventory.schema_version != EVIDENCE_SCHEMA_VERSION {
            return Err(EvidenceError::UnsupportedSchema {
                found: inventory.schema_version,
            });
        }
        let prepared = Self::prepare(repo_root, &binding.project_id, &binding.commit_oid)?;
        if prepared.binding != binding {
            return Err(EvidenceError::BindingMismatch {
                detail: "snapshot binding differs from exact Git objects".to_string(),
            });
        }
        if prepared.inventory != inventory {
            return Err(EvidenceError::InventoryMismatch {
                detail: "tracked-file inventory differs from exact commit tree".to_string(),
            });
        }
        Ok(prepared)
    }

    fn from_parts(
        repo_root: PathBuf,
        binding: SnapshotBinding,
        inventory: SnapshotInventory,
    ) -> Result<Self> {
        if inventory.digest != binding.inventory_digest {
            return Err(EvidenceError::InventoryMismatch {
                detail: "inventory digest does not match binding".to_string(),
            });
        }
        let entries = inventory
            .entries
            .iter()
            .cloned()
            .map(|entry| (entry.path.clone(), entry))
            .collect();
        Ok(Self {
            repo_root,
            binding,
            inventory,
            entries,
        })
    }

    pub fn binding(&self) -> &SnapshotBinding {
        &self.binding
    }

    pub fn inventory(&self) -> &SnapshotInventory {
        &self.inventory
    }

    pub fn entry(&self, path: &str) -> Result<&InventoryEntry> {
        validate_repo_path(path)?;
        self.entries
            .get(path)
            .ok_or_else(|| EvidenceError::PathNotTracked {
                path: path.to_string(),
            })
    }

    pub fn exclusions(&self) -> Vec<InventoryEntry> {
        self.inventory
            .entries
            .iter()
            .filter(|entry| entry.exclusion.is_some())
            .cloned()
            .collect()
    }

    pub fn eligible_entries(&self) -> impl Iterator<Item = &InventoryEntry> {
        self.inventory
            .entries
            .iter()
            .filter(|entry| entry.exclusion.is_none())
    }

    pub fn read_blob(&self, path: &str) -> Result<Vec<u8>> {
        let entry = self.entry(path)?;
        if let Some(reason) = entry.exclusion {
            return Err(EvidenceError::ExcludedPath {
                path: path.to_string(),
                reason,
            });
        }
        let oid = entry
            .blob_oid
            .as_deref()
            .ok_or_else(|| EvidenceError::ExcludedPath {
                path: path.to_string(),
                reason: ExclusionReason::UnsupportedObject,
            })?;
        let bytes = git(&self.repo_root, &["cat-file", "blob", oid], None).map_err(|error| {
            EvidenceError::MissingGitObject {
                oid: oid.to_string(),
                detail: error.to_string(),
            }
        })?;
        let actual_hash = gobby_core::indexing::content_hash(&bytes);
        if entry.content_hash.as_deref() != Some(actual_hash.as_str()) {
            return Err(EvidenceError::InventoryMismatch {
                detail: format!("blob content hash changed for {path}"),
            });
        }
        Ok(bytes)
    }

    pub fn verify_fact(&self, path: &str, content_hash: &str) -> Result<()> {
        let entry = self.entry(path)?;
        if entry.exclusion.is_some() || entry.content_hash.as_deref() != Some(content_hash) {
            return Err(EvidenceError::FactMismatch {
                path: path.to_string(),
                expected: entry.content_hash.clone().unwrap_or_default(),
                found: content_hash.to_string(),
            });
        }
        Ok(())
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

fn validate_oid(oid: &str) -> Result<()> {
    if !matches!(oid.len(), 40 | 64)
        || !oid
            .as_bytes()
            .iter()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
    {
        return Err(EvidenceError::InvalidObjectId {
            oid: oid.to_string(),
        });
    }
    Ok(())
}

fn load_inventory(repo_root: &Path, tree_oid: &str) -> Result<Vec<InventoryEntry>> {
    let bytes = git(
        repo_root,
        &["ls-tree", "-rlz", "--full-tree", tree_oid],
        None,
    )?;
    let mut entries = Vec::new();
    for record in bytes
        .split(|byte| *byte == 0)
        .filter(|record| !record.is_empty())
    {
        let (header, raw_path) = split_once(record, b'\t').ok_or_else(|| EvidenceError::Git {
            operation: "parse ls-tree".to_string(),
            message: "record lacks path delimiter".to_string(),
        })?;
        let path = match std::str::from_utf8(raw_path) {
            Ok(path) => path.to_string(),
            Err(_) => escaped_path(raw_path),
        };
        let header = std::str::from_utf8(header).map_err(|error| EvidenceError::Git {
            operation: "parse ls-tree".to_string(),
            message: error.to_string(),
        })?;
        let fields = header.split_ascii_whitespace().collect::<Vec<_>>();
        if fields.len() != 4 {
            return Err(EvidenceError::Git {
                operation: "parse ls-tree".to_string(),
                message: format!("unexpected header {header:?}"),
            });
        }
        let mode = fields[0].to_string();
        let object_type = fields[1];
        let object_oid = fields[2].to_string();
        let size_bytes = fields[3].parse::<u64>().ok();
        let (kind, blob_oid, mut exclusion) = match (mode.as_str(), object_type) {
            ("100644", "blob") => (TrackedFileKind::File, Some(object_oid.clone()), None),
            ("100755", "blob") => (TrackedFileKind::Executable, Some(object_oid.clone()), None),
            ("120000", "blob") => (
                TrackedFileKind::Symlink,
                Some(object_oid.clone()),
                Some(ExclusionReason::Symlink),
            ),
            ("160000", "commit") => (
                TrackedFileKind::Gitlink,
                None,
                Some(ExclusionReason::Gitlink),
            ),
            _ => (
                TrackedFileKind::Unsupported,
                None,
                Some(ExclusionReason::UnsupportedObject),
            ),
        };
        if std::str::from_utf8(raw_path).is_err() || validate_repo_path(&path).is_err() {
            exclusion = Some(ExclusionReason::UnsafePath);
        }
        if size_bytes.is_some_and(|size| size > MAX_EVIDENCE_FILE_BYTES) {
            exclusion = Some(ExclusionReason::Oversized);
        }
        let mut content_hash = None;
        if exclusion.is_none()
            && let Some(oid) = &blob_oid
        {
            let content = git(repo_root, &["cat-file", "blob", oid], None).map_err(|error| {
                EvidenceError::MissingGitObject {
                    oid: oid.clone(),
                    detail: error.to_string(),
                }
            })?;
            exclusion = if content.contains(&0) {
                Some(ExclusionReason::Binary)
            } else if std::str::from_utf8(&content).is_err() {
                Some(ExclusionReason::UnsupportedEncoding)
            } else {
                content_hash = Some(gobby_core::indexing::content_hash(&content));
                None
            };
        }
        entries.push(InventoryEntry {
            path: path.clone(),
            mode,
            kind,
            object_oid,
            blob_oid,
            size_bytes,
            content_hash,
            language: crate::index::languages::detect_language(&path).map(str::to_string),
            exclusion,
        });
    }
    Ok(entries)
}

fn load_commit_binding(repo_root: &Path, commit_oid: &str) -> Result<CommitBinding> {
    let parents = git_text(repo_root, &["show", "-s", "--format=%P", commit_oid])?;
    let parent_oids = parents
        .split_ascii_whitespace()
        .map(str::to_string)
        .collect::<Vec<_>>();
    let (comparison_parent_oid, comparison_kind) = match parent_oids.first() {
        Some(parent) => (parent.clone(), ComparisonKind::FirstParent),
        None => (
            git_text(repo_root, &["hash-object", "-t", "tree", "--stdin"])?,
            ComparisonKind::EmptyTree,
        ),
    };
    let mut changed_paths = load_changed_paths(repo_root, &comparison_parent_oid, commit_oid)?;
    changed_paths.sort();
    let changed_paths_digest = canonical_hash(&changed_paths)?;
    Ok(CommitBinding {
        parent_oids,
        comparison_parent_oid,
        comparison_kind,
        changed_paths_digest,
        changed_paths,
    })
}

fn load_changed_paths(
    repo_root: &Path,
    base_oid: &str,
    commit_oid: &str,
) -> Result<Vec<ChangedPath>> {
    let bytes = git(
        repo_root,
        &[
            "diff-tree",
            "--raw",
            "-r",
            "-z",
            "--no-abbrev",
            "--no-commit-id",
            "--find-renames",
            base_oid,
            commit_oid,
        ],
        None,
    )?;
    let fields = bytes.split(|byte| *byte == 0).collect::<Vec<_>>();
    let mut index = 0;
    let mut records = Vec::new();
    while index < fields.len() && !fields[index].is_empty() {
        let header = std::str::from_utf8(fields[index]).map_err(|error| EvidenceError::Git {
            operation: "parse diff-tree".to_string(),
            message: error.to_string(),
        })?;
        index += 1;
        let header = header.strip_prefix(':').ok_or_else(|| EvidenceError::Git {
            operation: "parse diff-tree".to_string(),
            message: format!("invalid raw header {header:?}"),
        })?;
        let parts = header.split_ascii_whitespace().collect::<Vec<_>>();
        if parts.len() != 5 || index >= fields.len() {
            return Err(EvidenceError::Git {
                operation: "parse diff-tree".to_string(),
                message: format!("invalid raw header {header:?}"),
            });
        }
        let status_token = parts[4];
        let status_code = status_token.chars().next().unwrap_or(' ');
        let status = match status_code {
            'A' => ChangeStatus::Added,
            'C' => ChangeStatus::Copied,
            'D' => ChangeStatus::Deleted,
            'M' => ChangeStatus::Modified,
            'R' => ChangeStatus::Renamed,
            'T' => ChangeStatus::TypeChanged,
            _ => {
                return Err(EvidenceError::Git {
                    operation: "parse diff-tree".to_string(),
                    message: format!("unsupported change status {status_token:?}"),
                });
            }
        };
        let first_path = utf8_diff_path(fields[index])?;
        index += 1;
        let (old_path, new_path) = match status {
            ChangeStatus::Added => (None, Some(first_path)),
            ChangeStatus::Deleted => (Some(first_path), None),
            ChangeStatus::Renamed | ChangeStatus::Copied => {
                if index >= fields.len() {
                    return Err(EvidenceError::Git {
                        operation: "parse diff-tree".to_string(),
                        message: "rename/copy record lacks destination".to_string(),
                    });
                }
                let second_path = utf8_diff_path(fields[index])?;
                index += 1;
                (Some(first_path), Some(second_path))
            }
            ChangeStatus::Modified | ChangeStatus::TypeChanged => {
                (Some(first_path.clone()), Some(first_path))
            }
        };
        records.push(ChangedPath {
            status,
            similarity: status_token.get(1..).and_then(|value| value.parse().ok()),
            old_path,
            new_path,
            old_mode: nonzero_mode(parts[0]),
            new_mode: nonzero_mode(parts[1]),
            old_blob_oid: blob_oid(parts[0], parts[2]),
            new_blob_oid: blob_oid(parts[1], parts[3]),
        });
    }
    Ok(records)
}

fn nonzero_mode(mode: &str) -> Option<String> {
    (mode != "000000").then(|| mode.to_string())
}

fn blob_oid(mode: &str, oid: &str) -> Option<String> {
    (mode.starts_with("100") || mode == "120000").then(|| oid.to_string())
}

fn utf8_diff_path(path: &[u8]) -> Result<String> {
    let path = std::str::from_utf8(path).map_err(|_| EvidenceError::UnsafePath {
        path: escaped_path(path),
    })?;
    validate_repo_path(path)?;
    Ok(path.to_string())
}

fn split_once(bytes: &[u8], delimiter: u8) -> Option<(&[u8], &[u8])> {
    let index = bytes.iter().position(|byte| *byte == delimiter)?;
    Some((&bytes[..index], &bytes[index + 1..]))
}

fn escaped_path(path: &[u8]) -> String {
    path.iter()
        .map(|byte| {
            if byte.is_ascii_graphic() && *byte != b'\\' {
                char::from(*byte).to_string()
            } else {
                format!("\\x{byte:02x}")
            }
        })
        .collect()
}

fn git_text(repo_root: &Path, args: &[&str]) -> Result<String> {
    let bytes = git(repo_root, args, None)?;
    Ok(String::from_utf8(bytes)
        .map_err(|error| EvidenceError::Git {
            operation: format!("git {}", args.join(" ")),
            message: error.to_string(),
        })?
        .trim()
        .to_string())
}

fn git(repo_root: &Path, args: &[&str], input: Option<&[u8]>) -> Result<Vec<u8>> {
    let mut command = Command::new("git");
    command.arg("-C").arg(repo_root).args(args);
    if input.is_some() || args.last() == Some(&"--stdin") {
        command.stdin(Stdio::piped());
    }
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = command.spawn().map_err(|error| EvidenceError::Git {
        operation: format!("git {}", args.join(" ")),
        message: error.to_string(),
    })?;
    if let Some(mut stdin) = child.stdin.take() {
        stdin
            .write_all(input.unwrap_or_default())
            .map_err(|error| EvidenceError::Git {
                operation: format!("git {}", args.join(" ")),
                message: error.to_string(),
            })?;
    }
    let output = child
        .wait_with_output()
        .map_err(|error| EvidenceError::Git {
            operation: format!("git {}", args.join(" ")),
            message: error.to_string(),
        })?;
    if !output.status.success() {
        return Err(EvidenceError::Git {
            operation: format!("git {}", args.join(" ")),
            message: String::from_utf8_lossy(&output.stderr).trim().to_string(),
        });
    }
    Ok(output.stdout)
}
