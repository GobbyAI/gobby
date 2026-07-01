//! In-place whitespace repair for already-written vault markdown.
//!
//! Companion to [`crate::markdown::normalize`], which normalizes markdown at
//! write time (`#850`). That only fixes *new* writes; docs already on disk keep
//! their pre-normalization whitespace until regenerated. This module runs the
//! same normalizer over authored/derived docs in place, without regenerating.
//!
//! Scope is an allowlist of authored canonical docs: `code/**`, `knowledge/**`
//! (which includes derived `knowledge/sources/<id>.md` pages), and the
//! vault-root `_index.md` / `log.md` stubs. Immutable `raw/<id>.md` captures are
//! never touched — they go through `write_immutable`, whose
//! `validate_existing_raw_bytes` contract re-checks them byte-for-byte on
//! re-ingest, so a whitespace rewrite would break that fidelity guarantee.
//! Control state (`_gwiki/`), shared metadata (`_meta/`), and Obsidian config
//! (`.obsidian/`) are out of scope by virtue of the allowlist.

use std::fs;
use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::lint::relative_path;
use crate::sources::atomic::write_atomic;
use crate::vault::{CODE_ROOT, KNOWLEDGE_ROOT};
use crate::{ScopeIdentity, WikiError};

/// Authored subtrees whose markdown the normalizer may rewrite. Kept in sync
/// with the vault layout via the shared root constants; `raw/` is deliberately
/// absent so immutable captures are never visited.
const AUTHORED_ROOTS: &[&str] = &[CODE_ROOT, KNOWLEDGE_ROOT];

/// Vault-root stub files in scope (siblings of the authored roots).
const AUTHORED_ROOT_FILES: &[&str] = &["_index.md", "log.md"];

/// Result of a normalize pass, serialized for JSON output.
#[derive(Debug, Serialize)]
pub struct NormalizeReport {
    pub command: &'static str,
    pub scope: ScopeIdentity,
    pub root: PathBuf,
    /// Number of authored markdown files examined.
    pub scanned: usize,
    /// Relative paths whose content changed (or would change, in check mode).
    pub changed: Vec<PathBuf>,
    /// `true` when run with `--check`: report only, no writes performed.
    pub check_only: bool,
}

/// Normalize authored vault markdown in place, returning what changed.
///
/// With `check_only`, files are scanned and diffed but never written, so the
/// report previews the work a real pass would do. The normalizer is idempotent,
/// so a second pass over already-normalized docs reports zero changes.
pub fn run(
    vault_root: &Path,
    scope: ScopeIdentity,
    check_only: bool,
) -> Result<NormalizeReport, WikiError> {
    let mut files = Vec::new();
    for root_name in AUTHORED_ROOTS {
        let root = vault_root.join(root_name);
        if root.exists() {
            collect_markdown(&root, &mut files)?;
        }
    }
    for file_name in AUTHORED_ROOT_FILES {
        let path = vault_root.join(file_name);
        if path.is_file() {
            files.push(path);
        }
    }
    files.sort();

    let mut changed = Vec::new();
    for path in &files {
        let original = fs::read_to_string(path).map_err(|source| WikiError::Io {
            action: "read vault markdown for normalize",
            path: Some(path.clone()),
            source,
        })?;
        let normalized = crate::markdown::normalize(&original);
        if normalized != original {
            if !check_only {
                write_atomic(
                    path,
                    normalized.as_bytes(),
                    "write normalized vault markdown",
                )?;
            }
            changed.push(relative_path(vault_root, path));
        }
    }
    // `changed` is built from the pre-sorted `files`, so it is already ordered.

    Ok(NormalizeReport {
        command: "normalize",
        scope,
        root: vault_root.to_path_buf(),
        scanned: files.len(),
        changed,
        check_only,
    })
}

/// Render a human-readable summary of a normalize pass.
pub fn render_text(report: &NormalizeReport) -> String {
    let verb = if report.check_only {
        "need normalization"
    } else {
        "normalized"
    };
    let mut text = format!(
        "Wiki markdown normalize\nScope: {}\nScanned {} authored file(s); {} {}.\n",
        report.scope,
        report.scanned,
        report.changed.len(),
        verb,
    );
    for path in &report.changed {
        text.push_str("- ");
        text.push_str(&path.display().to_string());
        text.push('\n');
    }
    text
}

/// Exit code for the command layer. `--check` is a gate: it returns a non-zero
/// code when authored docs still need normalization, so CI can fail on
/// un-normalized markdown (like `cargo fmt --check`). A write pass always
/// succeeds — it fixed the files in place.
pub(crate) fn check_exit_code(report: &NormalizeReport) -> u8 {
    if report.check_only && !report.changed.is_empty() {
        1
    } else {
        0
    }
}

/// Recursively collect markdown files under `directory`. Skips nothing — the
/// allowlist in [`run`] keeps immutable/non-authored subtrees off the walk.
fn collect_markdown(directory: &Path, files: &mut Vec<PathBuf>) -> Result<(), WikiError> {
    let entries = match fs::read_dir(directory) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(source) => {
            return Err(WikiError::Io {
                action: "read vault directory for normalize",
                path: Some(directory.to_path_buf()),
                source,
            });
        }
    };
    for entry in entries {
        let entry = entry.map_err(|source| WikiError::Io {
            action: "read vault entry for normalize",
            path: Some(directory.to_path_buf()),
            source,
        })?;
        let path = entry.path();
        let file_type = entry.file_type().map_err(|source| WikiError::Io {
            action: "read vault file type for normalize",
            path: Some(path.clone()),
            source,
        })?;
        if file_type.is_dir() {
            collect_markdown(&path, files)?;
        } else if file_type.is_file() && is_markdown_path(&path) {
            files.push(path);
        }
    }
    Ok(())
}

fn is_markdown_path(path: &Path) -> bool {
    path.extension()
        .and_then(|extension| extension.to_str())
        .is_some_and(|extension| {
            matches!(extension.to_ascii_lowercase().as_str(), "md" | "markdown")
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(root: &Path, relative: &str, contents: &str) {
        let path = root.join(relative);
        std::fs::create_dir_all(path.parent().expect("parent")).expect("create parent");
        std::fs::write(path, contents).expect("write fixture");
    }

    const DIRTY: &str = "# Title  \n\n\nBody text  \n## Section\nMore text.\n\n\n";

    #[test]
    fn normalizes_authored_docs_and_leaves_raw_captures_untouched() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();

        write(root, "knowledge/concepts/dirty.md", DIRTY);
        write(root, "code/INDEX.md", "# Code  \n\n\n");
        // An immutable raw capture with deliberately non-normalized whitespace.
        let raw_bytes = b"# Raw capture  \n\n\nverbatim  \n";
        let raw_path = root.join("raw/src-abc.md");
        std::fs::create_dir_all(raw_path.parent().expect("raw parent")).expect("raw dir");
        std::fs::write(&raw_path, raw_bytes).expect("write raw");

        let report = run(root, ScopeIdentity::topic("ops"), false).expect("normalize runs");

        assert!(
            report
                .changed
                .contains(&PathBuf::from("knowledge/concepts/dirty.md")),
            "authored doc should be normalized: {:?}",
            report.changed
        );
        let fixed =
            std::fs::read_to_string(root.join("knowledge/concepts/dirty.md")).expect("read fixed");
        assert_eq!(fixed, crate::markdown::normalize(DIRTY));

        // code/** is in scope: the dirty code/INDEX.md was normalized too.
        assert!(
            report.changed.contains(&PathBuf::from("code/INDEX.md")),
            "code/** docs are in scope: {:?}",
            report.changed
        );
        // Exactly the two authored docs were scanned — the raw capture was never
        // visited (proves exclusion, not just a no-op rewrite).
        assert_eq!(report.scanned, 2, "raw/<id>.md must not be scanned");

        // The raw capture must be byte-for-byte identical (excluded from scope).
        assert_eq!(
            std::fs::read(&raw_path).expect("read raw"),
            raw_bytes,
            "raw/<id>.md capture must not be rewritten"
        );
        assert!(
            !report.changed.iter().any(|p| p.starts_with("raw")),
            "raw captures must never appear in the changed set"
        );
    }

    #[test]
    fn is_idempotent() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write(root, "knowledge/topics/page.md", DIRTY);

        let first = run(root, ScopeIdentity::topic("ops"), false).expect("first pass");
        assert_eq!(first.changed.len(), 1);

        let second = run(root, ScopeIdentity::topic("ops"), false).expect("second pass");
        assert!(
            second.changed.is_empty(),
            "second pass over normalized docs should report no changes: {:?}",
            second.changed
        );
    }

    #[test]
    fn check_mode_reports_without_writing() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write(root, "knowledge/topics/page.md", DIRTY);

        let report = run(root, ScopeIdentity::topic("ops"), true).expect("check pass");

        assert_eq!(
            report.changed,
            vec![PathBuf::from("knowledge/topics/page.md")]
        );
        assert!(report.check_only);
        // File left dirty: check mode must not write.
        assert_eq!(
            std::fs::read_to_string(root.join("knowledge/topics/page.md")).expect("read page"),
            DIRTY
        );
    }

    #[test]
    fn normalizes_vault_root_stub_files() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write(root, "_index.md", "# Wiki Index  \n\n\n");
        write(root, "log.md", "# Log\n\n\n");

        let report = run(root, ScopeIdentity::topic("ops"), false).expect("normalize runs");

        assert!(report.changed.contains(&PathBuf::from("_index.md")));
        assert!(report.changed.contains(&PathBuf::from("log.md")));
    }

    fn report_with(check_only: bool, changed: Vec<PathBuf>) -> NormalizeReport {
        NormalizeReport {
            command: "normalize",
            scope: ScopeIdentity::topic("ops"),
            root: PathBuf::from("/tmp/vault"),
            scanned: changed.len(),
            changed,
            check_only,
        }
    }

    #[test]
    fn render_text_summarizes_changed_paths() {
        let report = report_with(
            true,
            vec![PathBuf::from("knowledge/a.md"), PathBuf::from("_index.md")],
        );

        let text = render_text(&report);

        assert!(text.contains("2 need normalization."), "{text}");
        assert!(text.contains("- knowledge/a.md"));
        assert!(text.contains("- _index.md"));
    }

    #[test]
    fn check_exit_code_gates_only_dirty_check_runs() {
        // Clean check run -> success.
        assert_eq!(check_exit_code(&report_with(true, Vec::new())), 0);
        // Dirty check run -> gate fails.
        assert_eq!(
            check_exit_code(&report_with(true, vec![PathBuf::from("knowledge/a.md")])),
            1,
        );
        // Dirty write run -> success: it fixed the files in place.
        assert_eq!(
            check_exit_code(&report_with(false, vec![PathBuf::from("knowledge/a.md")])),
            0,
        );
    }
}
