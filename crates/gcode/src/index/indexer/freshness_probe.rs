//! Lock-free, hash-free freshness pre-gate.
//!
//! `project_changed_since` answers one question without taking the per-project
//! advisory lock and without hashing any file: has anything under the project
//! root changed since the recorded `last_indexed_at`? Read-time freshness calls
//! this *before* the lock so the common no-change case is cheap and never prints
//! "refresh already running". When it reports a change, the caller falls through
//! to the existing lock + incremental reconcile, which is exactly as correct as
//! before.

use std::collections::HashSet;
use std::path::Path;
use std::time::{Duration, SystemTime};

use crate::index::walker;

use super::util::{effective_excludes, relative_path};

/// Clock-skew / mtime-granularity margin. Subtracted from `last_indexed_at`
/// before comparing file mtimes, so the gate only ever errs toward refreshing
/// and can never miss a real change. Absorbs host-vs-PostgreSQL (docker) clock
/// skew and same-second mtime granularity. It is not a distributed-filesystem
/// correctness guarantee; larger NFS or multi-host clock drift is reconciled by
/// the periodic maintenance full re-hash sweep.
const SKEW_MARGIN: Duration = Duration::from_secs(2);

/// Returns `true` if a discovered file is absent from the recorded index, newer
/// than `last_indexed_at`, or if any previously indexed path is no longer
/// discoverable. A `false` result lets the caller skip the advisory lock and
/// the full re-hash entirely.
///
/// Discovery mirrors the indexer's combined built-in and configured exclusions,
/// so the `.gobby/plans/**/*.md` allowlist and every other exclusion stay in
/// lockstep with what actually gets indexed — including the internal
/// `.gobby/plans/*.md` edits the daemon trigger never forwards.
/// Short-circuits on the first sign of change.
///
/// An indexed regular file whose mtime and ctime both predate the threshold
/// is checked from its metadata alone: its bytes are the ones the last index
/// classified, so the content checks (binary detection, generated-bundle
/// detection) are skipped. A classifier upgrade therefore does not re-prune
/// unchanged files here; the periodic maintenance full re-hash sweep does.
/// Every other file goes through the indexer's full classification and the
/// mtime check.
pub fn project_changed_since(
    project_root: &Path,
    last_indexed_at: SystemTime,
    indexed_paths: &[String],
    extra_excludes: &[String],
    options: walker::DiscoveryOptions,
) -> bool {
    let threshold = last_indexed_at
        .checked_sub(SKEW_MARGIN)
        .unwrap_or(last_indexed_at);

    // Walk from the canonical root, so a regular file the walker reaches without
    // following a link has a canonical path and a lexical relative path.
    let root = match project_root.canonicalize() {
        Ok(root) => root,
        Err(error) => {
            log::debug!(
                "treating project as changed: failed to resolve {}: {error}",
                project_root.display()
            );
            return true;
        }
    };
    let excludes = effective_excludes(extra_excludes);
    let indexed_paths: HashSet<&str> = indexed_paths.iter().map(String::as_str).collect();
    let mut discovered_paths = HashSet::new();
    // Discovery's dedup, keyed the same way: first file per canonical path wins.
    let mut seen = HashSet::new();

    for file in walker::walk_files(&root, options) {
        let path = file.path.as_path();
        let key = if file.direct {
            path.to_path_buf()
        } else {
            path.canonicalize().unwrap_or_else(|_| path.to_path_buf())
        };
        if !seen.insert(key) {
            continue;
        }

        // Fast path: an indexed regular file whose inode has not changed since
        // the threshold keeps the bytes and permissions the last index
        // classified, so only the byte-free filters can have changed its
        // verdict. Nothing is opened or read.
        if file.direct {
            let rel =
                crate::index::normalize_storage_path(path.strip_prefix(&root).unwrap_or(path));
            if indexed_paths.contains(rel.as_str())
                && let Ok(meta) = path.metadata()
            {
                match meta.modified() {
                    Ok(modified) if modified > threshold => {
                        log::debug!(
                            "treating project as changed: {rel} was modified after the last index"
                        );
                        return true;
                    }
                    Ok(_) if status_unchanged_since(&meta, threshold) => {
                        if !walker::passes_path_filters(&root, path, &excludes)
                            || !walker::indexable_len(meta.len())
                        {
                            log::debug!(
                                "treating project as changed: indexed path {rel} is no longer discovered"
                            );
                            return true;
                        }
                        discovered_paths.insert(rel);
                        continue;
                    }
                    _ => {}
                }
            }
        }

        // Full classification, as the indexer runs it, for new, linked,
        // allowlisted or recently touched files.
        if walker::classify_file(&root, path, &excludes).is_none() {
            continue;
        }
        let Ok(rel) = relative_path(path, &root) else {
            return true;
        };
        // Add: a discovered path absent from code_indexed_files. Check this before
        // mtime so previously excluded files refresh even when their mtimes are old.
        discovered_paths.insert(rel.clone());
        if !indexed_paths.contains(rel.as_str()) {
            log::debug!("treating project as changed: {rel} is discovered but not indexed");
            return true;
        }

        match path.metadata() {
            Ok(meta) => match meta.modified() {
                Ok(modified) if modified <= threshold => {}
                Ok(_) => {
                    log::debug!(
                        "treating project as changed: {rel} was modified after the last index"
                    );
                    return true;
                }
                Err(error) => {
                    log::debug!(
                        "treating project as changed: failed to read mtime for {}: {error}",
                        path.display()
                    );
                    return true;
                }
            },
            Err(error) => {
                log::debug!(
                    "treating project as changed: failed to read metadata for {}: {error}",
                    path.display()
                );
                return true;
            }
        }
    }

    // Delete, rename, or newly excluded: an indexed path absent from discovery
    // needs a reconcile so stale facts and projections are pruned.
    if let Some(missing) = indexed_paths
        .iter()
        .find(|rel| !discovered_paths.contains(**rel))
    {
        log::debug!("treating project as changed: indexed path {missing} is no longer discovered");
        return true;
    }
    false
}

/// Whether the inode's last status change (ctime) is at or before `threshold`.
/// Writes, chmod, renames and a backdated `touch` all advance ctime, and
/// ordinary tools cannot set it back.
#[cfg(unix)]
fn status_unchanged_since(meta: &std::fs::Metadata, threshold: SystemTime) -> bool {
    use std::os::unix::fs::MetadataExt;

    let (Ok(secs), Ok(nanos)) = (
        u64::try_from(meta.ctime()),
        u32::try_from(meta.ctime_nsec()),
    ) else {
        return false;
    };
    SystemTime::UNIX_EPOCH + Duration::new(secs, nanos) <= threshold
}

/// Without a ctime, every indexed file takes the full classification path.
#[cfg(not(unix))]
fn status_unchanged_since(_meta: &std::fs::Metadata, _threshold: SystemTime) -> bool {
    false
}

#[cfg(test)]
#[path = "freshness_probe/tests.rs"]
mod tests;
