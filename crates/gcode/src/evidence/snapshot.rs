use std::collections::{BTreeMap, BTreeSet};
use std::io::{BufRead as _, BufReader, Read as _, Write as _};
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

    /// Materialize only evidence-eligible blobs without checkout filters or hooks.
    pub fn materialize(&self, target_root: &Path) -> Result<()> {
        let target_root =
            target_root
                .canonicalize()
                .map_err(|error| EvidenceError::InventoryMismatch {
                    detail: format!("canonicalize materialization root: {error}"),
                })?;
        let mut blobs = BlobBatch::new(&self.repo_root)?;
        for entry in self.eligible_entries() {
            let destination = checked_destination(&target_root, &entry.path)?;
            if destination.exists() || destination.is_symlink() {
                return Err(EvidenceError::InventoryMismatch {
                    detail: format!("materialized path already exists: {}", entry.path),
                });
            }
            let parent = destination
                .parent()
                .ok_or_else(|| EvidenceError::InventoryMismatch {
                    detail: format!("materialized path has no parent: {}", entry.path),
                })?;
            std::fs::create_dir_all(parent).map_err(|error| EvidenceError::InventoryMismatch {
                detail: format!("create materialized parent for {}: {error}", entry.path),
            })?;
            let resolved_parent =
                parent
                    .canonicalize()
                    .map_err(|error| EvidenceError::InventoryMismatch {
                        detail: format!(
                            "canonicalize materialized parent for {}: {error}",
                            entry.path
                        ),
                    })?;
            if !resolved_parent.starts_with(&target_root) {
                return Err(EvidenceError::UnsafePath {
                    path: entry.path.clone(),
                });
            }
            let mut file = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&destination)
                .map_err(|error| EvidenceError::InventoryMismatch {
                    detail: format!("create materialized file {}: {error}", entry.path),
                })?;
            file.write_all(&self.read_verified_blob(entry, &mut blobs)?)
                .and_then(|()| file.sync_all())
                .map_err(|error| EvidenceError::InventoryMismatch {
                    detail: format!("write materialized file {}: {error}", entry.path),
                })?;
            set_materialized_mode(&destination, entry.kind)?;
        }
        blobs.finish()?;
        self.verify_materialized(&target_root)
    }

    /// Verify every evidence-eligible path without invoking Git filters or hooks.
    pub fn verify_materialized(&self, target_root: &Path) -> Result<()> {
        let target_root =
            target_root
                .canonicalize()
                .map_err(|error| EvidenceError::InventoryMismatch {
                    detail: format!("canonicalize verification root: {error}"),
                })?;
        for entry in self.eligible_entries() {
            let path = checked_destination(&target_root, &entry.path)?;
            let metadata =
                path.symlink_metadata()
                    .map_err(|error| EvidenceError::InventoryMismatch {
                        detail: format!("missing materialized file {}: {error}", entry.path),
                    })?;
            if !metadata.file_type().is_file() {
                return Err(EvidenceError::InventoryMismatch {
                    detail: format!("materialized path is not a file: {}", entry.path),
                });
            }
            let content =
                std::fs::read(&path).map_err(|error| EvidenceError::InventoryMismatch {
                    detail: format!("read materialized file {}: {error}", entry.path),
                })?;
            let actual = gobby_core::indexing::content_hash(&content);
            if entry.content_hash.as_deref() != Some(actual.as_str()) {
                return Err(EvidenceError::InventoryMismatch {
                    detail: format!("materialized content changed for {}", entry.path),
                });
            }
        }
        verify_materialized_paths(
            &target_root,
            &self
                .eligible_entries()
                .map(|entry| entry.path.clone())
                .collect(),
        )?;
        Ok(())
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
        let mut blobs = BlobBatch::new(&self.repo_root)?;
        let bytes = self.read_verified_blob(entry, &mut blobs)?;
        blobs.finish()?;
        Ok(bytes)
    }

    pub(crate) fn read_eligible_blobs(
        &self,
    ) -> Result<std::collections::BTreeMap<String, Vec<u8>>> {
        let mut blobs = BlobBatch::new(&self.repo_root)?;
        let mut captured = std::collections::BTreeMap::new();
        for entry in self.eligible_entries() {
            captured.insert(
                entry.path.clone(),
                self.read_verified_blob(entry, &mut blobs)?,
            );
        }
        blobs.finish()?;
        Ok(captured)
    }

    fn read_verified_blob(&self, entry: &InventoryEntry, blobs: &mut BlobBatch) -> Result<Vec<u8>> {
        let path = &entry.path;
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
        let bytes = blobs.read(oid)?;
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
    let mut blobs = BlobBatch::new(repo_root)?;
    for record in bytes
        .split(|byte| *byte == 0)
        .filter(|record| !record.is_empty())
    {
        let (header, raw_path) = split_once(record, b'\t').ok_or_else(|| EvidenceError::Git {
            operation: "parse ls-tree".to_string(),
            message: "record lacks path delimiter".to_string(),
        })?;
        let (path, unsafe_path) = match std::str::from_utf8(raw_path) {
            Ok(path) if validate_repo_path(path).is_ok() => (path.to_string(), false),
            _ => (escaped_path(raw_path), true),
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
        if unsafe_path {
            exclusion = Some(ExclusionReason::UnsafePath);
        } else if crate::index::security::is_sensitive_evidence_path(Path::new(&path)) {
            exclusion = Some(ExclusionReason::SensitivePath);
        }
        if size_bytes.is_some_and(|size| size > MAX_EVIDENCE_FILE_BYTES) {
            exclusion = Some(ExclusionReason::Oversized);
        }
        let mut content_hash = None;
        let mut language = crate::index::languages::detect_language(&path).map(str::to_string);
        if exclusion.is_none()
            && let Some(oid) = &blob_oid
        {
            let content = blobs.read(oid)?;
            exclusion = if content.contains(&0) {
                Some(ExclusionReason::Binary)
            } else if std::str::from_utf8(&content).is_err() {
                Some(ExclusionReason::UnsupportedEncoding)
            } else if crate::index::security::contains_known_credential(&path, &content) {
                Some(ExclusionReason::SensitiveContent)
            } else {
                content_hash = Some(gobby_core::indexing::content_hash(&content));
                language = crate::index::languages::detect_language_from_content(&path, &content)
                    .map(str::to_string);
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
            language,
            exclusion,
        });
    }
    blobs.finish()?;
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
            "--no-ext-diff",
            "--no-textconv",
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
        let (first_path, first_exclusion) = canonical_diff_path(fields[index]);
        index += 1;
        let (old_path, new_path, old_exclusion, new_exclusion) = match status {
            ChangeStatus::Added => (None, Some(first_path), None, first_exclusion),
            ChangeStatus::Deleted => (Some(first_path), None, first_exclusion, None),
            ChangeStatus::Renamed | ChangeStatus::Copied => {
                if index >= fields.len() {
                    return Err(EvidenceError::Git {
                        operation: "parse diff-tree".to_string(),
                        message: "rename/copy record lacks destination".to_string(),
                    });
                }
                let (second_path, second_exclusion) = canonical_diff_path(fields[index]);
                index += 1;
                (
                    Some(first_path),
                    Some(second_path),
                    first_exclusion,
                    second_exclusion,
                )
            }
            ChangeStatus::Modified | ChangeStatus::TypeChanged => (
                Some(first_path.clone()),
                Some(first_path),
                first_exclusion,
                first_exclusion,
            ),
        };
        records.push(ChangedPath {
            status,
            similarity: status_token.get(1..).and_then(|value| value.parse().ok()),
            old_path,
            new_path,
            old_exclusion,
            new_exclusion,
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

fn canonical_diff_path(path: &[u8]) -> (String, Option<ExclusionReason>) {
    match std::str::from_utf8(path) {
        Ok(path) if validate_repo_path(path).is_ok() => (
            path.to_string(),
            crate::index::security::is_sensitive_evidence_path(Path::new(path))
                .then_some(ExclusionReason::SensitivePath),
        ),
        _ => (escaped_path(path), Some(ExclusionReason::UnsafePath)),
    }
}

fn checked_destination(root: &Path, path: &str) -> Result<PathBuf> {
    validate_repo_path(path)?;
    let destination = root.join(path);
    if !destination.starts_with(root) {
        return Err(EvidenceError::UnsafePath {
            path: path.to_string(),
        });
    }
    Ok(destination)
}

fn verify_materialized_paths(root: &Path, expected_files: &BTreeSet<String>) -> Result<()> {
    let mut expected_directories = BTreeSet::new();
    for path in expected_files {
        let mut parent = Path::new(path).parent();
        while let Some(directory) = parent {
            if directory.as_os_str().is_empty() {
                break;
            }
            expected_directories.insert(directory.to_string_lossy().replace('\\', "/"));
            parent = directory.parent();
        }
    }
    let mut pending = vec![root.to_path_buf()];
    while let Some(directory) = pending.pop() {
        for item in
            std::fs::read_dir(&directory).map_err(|error| EvidenceError::InventoryMismatch {
                detail: format!(
                    "read materialized directory {}: {error}",
                    directory.display()
                ),
            })?
        {
            let item = item.map_err(|error| EvidenceError::InventoryMismatch {
                detail: format!("read materialized directory entry: {error}"),
            })?;
            let path = item.path();
            let relative =
                path.strip_prefix(root)
                    .map_err(|error| EvidenceError::InventoryMismatch {
                        detail: format!("materialized path escaped snapshot root: {error}"),
                    })?;
            let relative = relative
                .to_str()
                .ok_or_else(|| EvidenceError::InventoryMismatch {
                    detail: "unexpected non-UTF-8 materialized path".to_string(),
                })?;
            let relative = relative.replace('\\', "/");
            let metadata =
                path.symlink_metadata()
                    .map_err(|error| EvidenceError::InventoryMismatch {
                        detail: format!("inspect materialized path {relative}: {error}"),
                    })?;
            let runtime_path = relative == ".gobby" || relative.starts_with(".gobby/");
            if runtime_path {
                if metadata.file_type().is_symlink() {
                    return Err(EvidenceError::InventoryMismatch {
                        detail: format!("unexpected materialized path: {relative}"),
                    });
                }
                if metadata.is_dir() {
                    pending.push(path);
                } else if !metadata.is_file() {
                    return Err(EvidenceError::InventoryMismatch {
                        detail: format!("unexpected materialized path: {relative}"),
                    });
                }
                continue;
            }
            if relative == ".git" && metadata.is_file() {
                continue;
            }
            if metadata.is_dir() && expected_directories.contains(&relative) {
                pending.push(path);
            } else if !metadata.is_file() || !expected_files.contains(&relative) {
                return Err(EvidenceError::InventoryMismatch {
                    detail: format!("unexpected materialized path: {relative}"),
                });
            }
        }
    }
    Ok(())
}

#[cfg(unix)]
fn set_materialized_mode(path: &Path, kind: TrackedFileKind) -> Result<()> {
    use std::os::unix::fs::PermissionsExt as _;

    let mode = if kind == TrackedFileKind::Executable {
        0o755
    } else {
        0o644
    };
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode)).map_err(|error| {
        EvidenceError::InventoryMismatch {
            detail: format!(
                "set materialized permissions for {}: {error}",
                path.display()
            ),
        }
    })
}

#[cfg(not(unix))]
fn set_materialized_mode(_path: &Path, _kind: TrackedFileKind) -> Result<()> {
    Ok(())
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

fn git_command(repo_root: &Path, args: &[&str]) -> Command {
    let mut command = Command::new("git");
    let path = std::env::var_os("PATH");
    let system_root = std::env::var_os("SYSTEMROOT");
    command.env_clear();
    if let Some(path) = path {
        command.env("PATH", path);
    }
    if let Some(system_root) = system_root {
        command.env("SYSTEMROOT", system_root);
    }
    command
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_NO_LAZY_FETCH", "1")
        .env("GIT_NO_REPLACE_OBJECTS", "1")
        .env("GIT_OPTIONAL_LOCKS", "0")
        .env("GIT_TERMINAL_PROMPT", "0")
        .args([
            "--no-replace-objects",
            "--literal-pathspecs",
            "-c",
            "core.useReplaceRefs=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "diff.external=",
        ])
        .arg("-C")
        .arg(repo_root)
        .args(args);
    command
}

fn git(repo_root: &Path, args: &[&str], input: Option<&[u8]>) -> Result<Vec<u8>> {
    let mut command = git_command(repo_root, args);
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

/// One request/response at a time bounds memory and avoids pipe deadlocks.
struct BlobBatch {
    child: std::process::Child,
    output: BufReader<std::process::ChildStdout>,
    errors: Option<std::thread::JoinHandle<std::io::Result<Vec<u8>>>>,
}

impl BlobBatch {
    fn new(repo_root: &Path) -> Result<Self> {
        let mut child = git_command(repo_root, &["cat-file", "--batch"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(batch_io_error)?;
        let Some(output) = child.stdout.take() else {
            let _ = child.kill();
            let _ = child.wait();
            return Err(batch_error("missing batch stdout"));
        };
        let mut batch = Self {
            child,
            output: BufReader::new(output),
            errors: None,
        };
        let mut errors = batch
            .child
            .stderr
            .take()
            .ok_or_else(|| batch_error("missing batch stderr"))?;
        // Drain concurrently without requiring ambient filesystem write access.
        // Retain a bounded diagnostic prefix, then discard the remainder.
        batch.errors = Some(
            std::thread::Builder::new()
                .name("git-blob-stderr".into())
                .spawn(move || {
                    let mut detail = Vec::new();
                    (&mut errors).take(4096).read_to_end(&mut detail)?;
                    std::io::copy(&mut errors, &mut std::io::sink())?;
                    Ok(detail)
                })
                .map_err(batch_io_error)?,
        );
        Ok(batch)
    }

    fn read(&mut self, oid: &str) -> Result<Vec<u8>> {
        validate_oid(oid)?;
        let input = self
            .child
            .stdin
            .as_mut()
            .ok_or_else(|| batch_error("batch is closed"))?;
        writeln!(input, "{oid}").map_err(batch_io_error)?;
        input.flush().map_err(batch_io_error)?;
        let mut header = String::new();
        (&mut self.output)
            .take(256)
            .read_line(&mut header)
            .map_err(batch_io_error)?;
        let fields = header.split_ascii_whitespace().collect::<Vec<_>>();
        let invalid = || EvidenceError::MissingGitObject {
            oid: oid.to_string(),
            detail: format!("invalid Git blob batch response: {header:?}"),
        };
        let [actual_oid, "blob", size] = fields.as_slice() else {
            return Err(invalid());
        };
        let size = size.parse::<usize>().map_err(|_| invalid())?;
        if *actual_oid != oid || !header.ends_with('\n') || size as u64 > MAX_EVIDENCE_FILE_BYTES {
            return Err(invalid());
        }
        let mut bytes = vec![0; size];
        self.output.read_exact(&mut bytes).map_err(batch_io_error)?;
        let mut delimiter = [0];
        self.output
            .read_exact(&mut delimiter)
            .map_err(batch_io_error)?;
        if delimiter != *b"\n" {
            return Err(invalid());
        }
        Ok(bytes)
    }

    fn finish(&mut self) -> Result<()> {
        drop(self.child.stdin.take());
        let mut extra = [0];
        if self.output.read(&mut extra).map_err(batch_io_error)? != 0 {
            return Err(batch_error("unexpected trailing Git blob batch response"));
        }
        let status = self.child.wait().map_err(batch_io_error)?;
        let detail = self
            .errors
            .take()
            .ok_or_else(|| batch_error("missing batch stderr reader"))?
            .join()
            .map_err(|_| batch_error("batch stderr reader panicked"))?
            .map_err(batch_io_error)?;
        if !status.success() {
            return Err(batch_error(&format!(
                "{status}: {}",
                String::from_utf8_lossy(&detail)
            )));
        }
        Ok(())
    }
}

impl Drop for BlobBatch {
    fn drop(&mut self) {
        // Reap the process on parser, integrity, and filesystem failures too.
        let _ = self.child.kill();
        let _ = self.child.wait();
        if let Some(errors) = self.errors.take() {
            let _ = errors.join();
        }
    }
}

fn batch_io_error(error: std::io::Error) -> EvidenceError {
    batch_error(&error.to_string())
}

fn batch_error(message: &str) -> EvidenceError {
    EvidenceError::Git {
        operation: "cat-file --batch".to_string(),
        message: message.to_string(),
    }
}
