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

    let excludes = effective_excludes(extra_excludes);
    let (candidates, content_only) =
        walker::discover_files_with_options(project_root, &excludes, options);
    let indexed_paths: HashSet<&str> = indexed_paths.iter().map(String::as_str).collect();
    let mut discovered_paths = HashSet::new();

    // Add: any discovered path absent from code_indexed_files. Check this before
    // mtime so previously excluded files refresh even when their mtimes are old.
    // Modify: a discovered file whose mtime is newer than the threshold.
    for path in candidates.iter().chain(content_only.iter()) {
        let Ok(rel) = relative_path(path, project_root) else {
            return true;
        };
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::File;
    use std::path::PathBuf;

    fn write_file(root: &Path, rel: &str, contents: &[u8]) -> PathBuf {
        let path = root.join(rel);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).expect("create parent");
        }
        std::fs::write(&path, contents).expect("write file");
        path
    }

    fn set_mtime(path: &Path, time: SystemTime) {
        File::options()
            .write(true)
            .open(path)
            .expect("open file to set mtime")
            .set_modified(time)
            .expect("set mtime");
    }

    /// A fixed, whole-second base instant well in the past, so the arithmetic
    /// never underflows and 1-second-granularity filesystems round-trip it.
    fn base_time() -> SystemTime {
        SystemTime::UNIX_EPOCH + Duration::from_secs(1_700_000_000)
    }

    fn default_options() -> walker::DiscoveryOptions {
        walker::DiscoveryOptions::default()
    }

    #[test]
    fn reports_no_change_when_everything_predates_last_index() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        let lib = write_file(root, "src/lib.rs", b"fn main() {}\n");
        let readme = write_file(root, "README.md", b"# Title\n");

        let base = base_time();
        set_mtime(&lib, base);
        set_mtime(&readme, base);

        // last_indexed_at is well after every file's mtime.
        let last = base + Duration::from_secs(3600);
        let indexed = vec!["src/lib.rs".to_string(), "README.md".to_string()];

        assert!(!project_changed_since(
            root,
            last,
            &indexed,
            &[],
            default_options()
        ));
    }

    #[test]
    fn reports_change_when_a_file_is_modified_after_last_index() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        let lib = write_file(root, "src/lib.rs", b"fn main() {}\n");
        set_mtime(&lib, base_time() + Duration::from_secs(7200));

        let last = base_time() + Duration::from_secs(3600);
        let indexed = vec!["src/lib.rs".to_string()];

        assert!(project_changed_since(
            root,
            last,
            &indexed,
            &[],
            default_options()
        ));
    }

    #[test]
    fn reports_change_for_unindexed_file_even_when_mtime_is_old() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        let added = write_file(root, "src/new.rs", b"fn added() {}\n");
        set_mtime(&added, base_time());

        let last = base_time() + Duration::from_secs(3600);
        let indexed: Vec<String> = Vec::new();

        assert!(project_changed_since(
            root,
            last,
            &indexed,
            &[],
            default_options()
        ));
    }

    #[test]
    fn reports_change_when_indexed_file_is_deleted() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        let lib = write_file(root, "src/lib.rs", b"fn main() {}\n");
        set_mtime(&lib, base_time());

        let last = base_time() + Duration::from_secs(3600);
        // "src/gone.rs" is recorded as indexed but no longer exists on disk.
        let indexed = vec!["src/lib.rs".to_string(), "src/gone.rs".to_string()];

        assert!(project_changed_since(
            root,
            last,
            &indexed,
            &[],
            default_options()
        ));
    }

    #[test]
    fn skew_margin_boundary_only_ever_makes_the_gate_more_eager() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        let lib = write_file(root, "src/lib.rs", b"fn main() {}\n");
        let mtime = base_time();
        set_mtime(&lib, mtime);
        let indexed = vec!["src/lib.rs".to_string()];

        // File is 1s older than last_indexed_at — inside the 2s margin, so the
        // gate refreshes (threshold = last - 2s = mtime - 1s < mtime).
        let within_margin = mtime + Duration::from_secs(1);
        assert!(project_changed_since(
            root,
            within_margin,
            &indexed,
            &[],
            default_options()
        ));

        // File sits exactly at the boundary (threshold == mtime, mtime <=
        // threshold), so it counts as unchanged.
        let at_margin = mtime + SKEW_MARGIN;
        assert!(!project_changed_since(
            root,
            at_margin,
            &indexed,
            &[],
            default_options()
        ));

        // File is 3s older than last_indexed_at — beyond the 2s margin, so the
        // gate skips (threshold = last - 2s = mtime + 1s >= mtime).
        let beyond_margin = mtime + Duration::from_secs(3);
        assert!(!project_changed_since(
            root,
            beyond_margin,
            &indexed,
            &[],
            default_options()
        ));
    }

    #[test]
    fn gitignored_new_files_follow_respect_gitignore_setting() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        std::fs::create_dir(root.join(".git")).expect("git dir");
        write_file(root, ".gitignore", b"ignored.rs\n");
        let ignored = write_file(root, "ignored.rs", b"fn ignored() {}\n");
        set_mtime(&ignored, base_time() + Duration::from_secs(7200));

        let last = base_time() + Duration::from_secs(3600);
        let indexed: Vec<String> = Vec::new();

        assert!(!project_changed_since(
            root,
            last,
            &indexed,
            &[],
            walker::DiscoveryOptions {
                respect_gitignore: true
            }
        ));
        assert!(project_changed_since(
            root,
            last,
            &indexed,
            &[],
            walker::DiscoveryOptions {
                respect_gitignore: false
            }
        ));
    }

    #[test]
    fn newly_excluded_indexed_file_triggers_pruning_refresh() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        let generated = write_file(root, "generated/output.rs", b"fn generated() {}\n");
        set_mtime(&generated, base_time());

        let last = base_time() + Duration::from_secs(3600);
        let indexed = vec!["generated/output.rs".to_string()];
        let extra_excludes = vec!["generated".to_string()];

        assert!(project_changed_since(
            root,
            last,
            &indexed,
            &extra_excludes,
            default_options()
        ));
    }
}
