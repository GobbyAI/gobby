//! Page lifecycle transitions and the shared default-surface exclusion
//! predicate.
//!
//! Lifecycle is workflow state (`draft | reviewed | verified | stale |
//! archived`) carried in page frontmatter; `trust` stays an orthogonal
//! reporting axis. Transitions are owned by the existing maintenance loops
//! (compile/upkeep create, librarian promotes, audit verifies, health
//! demotes, upkeep archives) and every applied transition appends a `log.md`
//! entry — the audit trail lives in the append-only log, not a new table.

use std::path::{Path, PathBuf};

use crate::frontmatter::{
    WikiFrontmatter, WikiLifecycle, mark_stale_markdown_at, parse_frontmatter,
    render_markdown_with_metadata,
};
use crate::log::{ACTION_LIFECYCLE_TRANSITION, LogEntry, append_logs};
use crate::{ScopeIdentity, WikiError};

/// Shared exclusion predicate for catalog indexes, agent exports, and default
/// retrieval. Archived pages stay on disk at stable paths but disappear from
/// every default surface; quarantined candidates (#17727) are likewise hidden
/// until promotion clears the flag.
#[allow(dead_code, reason = "kept as the default-surface predicate wrapper")]
pub(crate) fn excluded_from_default_surfaces(frontmatter: &WikiFrontmatter) -> bool {
    excluded_from_surfaces(frontmatter, false)
}

/// [`excluded_from_default_surfaces`] with an opt-in for candidate pages, so
/// retrieval callers serving the librarian/upkeep loops can still see
/// quarantined pages. Archived pages are excluded unconditionally.
pub(crate) fn excluded_from_surfaces(
    frontmatter: &WikiFrontmatter,
    include_candidates: bool,
) -> bool {
    matches!(frontmatter.lifecycle, Some(WikiLifecycle::Archived))
        || (!include_candidates && frontmatter.candidate)
}

/// File-reading variant of [`excluded_from_default_surfaces`] for surfaces
/// that only hold a page path. Missing or unparseable pages are NOT excluded:
/// exclusion must never hide results because of an I/O or parse hiccup.
pub(crate) fn page_excluded_from_default_surfaces(vault_root: &Path, relative: &Path) -> bool {
    page_excluded_from_surfaces(vault_root, relative, false)
}

/// File-reading variant of [`excluded_from_surfaces`].
pub(crate) fn page_excluded_from_surfaces(
    vault_root: &Path,
    relative: &Path,
    include_candidates: bool,
) -> bool {
    let path = vault_root.join(relative);
    let Ok(markdown) = std::fs::read_to_string(&path) else {
        return false;
    };
    let Ok(parsed) = parse_frontmatter(&markdown) else {
        return false;
    };
    excluded_from_surfaces(&parsed.metadata, include_candidates)
}

/// True when the page at `relative` carries the `candidate: true` quarantine
/// flag. Missing or unparseable pages read as non-candidates.
pub(crate) fn page_is_candidate(vault_root: &Path, relative: &Path) -> bool {
    let path = vault_root.join(relative);
    let Ok(markdown) = std::fs::read_to_string(&path) else {
        return false;
    };
    let Ok(parsed) = parse_frontmatter(&markdown) else {
        return false;
    };
    parsed.metadata.candidate
}

/// Clear a page's `candidate` quarantine flag (promotion, #17727) and append
/// a `candidate_promoted` log entry. Returns false when the page is not a
/// candidate (no rewrite, no log entry).
pub(crate) fn promote_candidate_page(
    vault_root: &Path,
    scope: &ScopeIdentity,
    relative: &Path,
    reason: &str,
) -> Result<bool, WikiError> {
    let path = vault_root.join(relative);
    let markdown = std::fs::read_to_string(&path).map_err(|error| WikiError::Io {
        action: "read wiki page for candidate promotion",
        path: Some(path.clone()),
        source: error,
    })?;
    let parsed = parse_frontmatter(&markdown).map_err(|error| WikiError::InvalidInput {
        field: "frontmatter",
        message: format!("{}: {error}", relative.display()),
    })?;
    if !parsed.metadata.candidate {
        return Ok(false);
    }

    let mut metadata = parsed.metadata.clone();
    metadata.candidate = false;
    let updated =
        render_markdown_with_metadata(parsed.format, &metadata, parsed.body).map_err(|error| {
            WikiError::InvalidInput {
                field: "frontmatter",
                message: format!("{}: {error}", relative.display()),
            }
        })?;
    std::fs::write(&path, updated).map_err(|error| WikiError::Io {
        action: "write wiki page candidate promotion",
        path: Some(path.clone()),
        source: error,
    })?;

    append_logs(
        vault_root,
        None,
        &LogEntry {
            timestamp: crate::support::time::collect_timestamp()?,
            scope: scope.clone(),
            action: crate::log::ACTION_CANDIDATE_PROMOTED.to_string(),
            summary: format!("{}: {reason}", relative.display()),
            artifacts: vec![relative.to_path_buf()],
        },
    )?;
    Ok(true)
}

/// One applied lifecycle transition, for reports and tests.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct AppliedTransition {
    pub page: PathBuf,
    pub from: Option<WikiLifecycle>,
    pub to: WikiLifecycle,
}

/// Rewrite one page's lifecycle frontmatter and append a `log.md` entry.
///
/// Returns `Ok(None)` without touching the file when the page is already in
/// the target state. `reason` lands in the log summary; for `stale` it is
/// also written through [`mark_stale_markdown_at`] so the legacy
/// `stale`/`stale_reason` keys stay coherent.
pub(crate) fn apply_lifecycle_transition(
    vault_root: &Path,
    scope: &ScopeIdentity,
    relative: &Path,
    to: WikiLifecycle,
    reason: &str,
) -> Result<Option<AppliedTransition>, WikiError> {
    let path = vault_root.join(relative);
    let markdown = std::fs::read_to_string(&path).map_err(|error| WikiError::Io {
        action: "read wiki page for lifecycle transition",
        path: Some(path.clone()),
        source: error,
    })?;
    let parsed = parse_frontmatter(&markdown).map_err(|error| WikiError::InvalidInput {
        field: "frontmatter",
        message: format!("{}: {error}", relative.display()),
    })?;
    let from = parsed.metadata.lifecycle;
    if from == Some(to) {
        return Ok(None);
    }

    let timestamp = crate::support::time::collect_timestamp()?;
    let updated = match to {
        WikiLifecycle::Stale => {
            mark_stale_markdown_at(&markdown, reason, &timestamp).map_err(|error| {
                WikiError::InvalidInput {
                    field: "frontmatter",
                    message: format!("{}: {error}", relative.display()),
                }
            })?
        }
        _ => {
            let mut metadata = parsed.metadata.clone();
            metadata.lifecycle = Some(to);
            if to == WikiLifecycle::Archived {
                metadata
                    .unknown
                    .entry("archived_at".to_string())
                    .or_insert_with(|| serde_json::Value::String(timestamp.clone()));
            }
            render_markdown_with_metadata(parsed.format, &metadata, parsed.body).map_err(
                |error| WikiError::InvalidInput {
                    field: "frontmatter",
                    message: format!("{}: {error}", relative.display()),
                },
            )?
        }
    };

    std::fs::write(&path, updated).map_err(|error| WikiError::Io {
        action: "write wiki page lifecycle transition",
        path: Some(path.clone()),
        source: error,
    })?;

    let from_label = from.map_or("none", WikiLifecycle::as_str);
    append_logs(
        vault_root,
        None,
        &LogEntry {
            timestamp,
            scope: scope.clone(),
            action: ACTION_LIFECYCLE_TRANSITION.to_string(),
            summary: format!("{}: {from_label} -> {to} ({reason})", relative.display()),
            artifacts: vec![relative.to_path_buf()],
        },
    )?;

    Ok(Some(AppliedTransition {
        page: relative.to_path_buf(),
        from,
        to,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::api::ScopeIdentity;

    fn write_page(root: &Path, relative: &str, markdown: &str) {
        let path = root.join(relative);
        std::fs::create_dir_all(path.parent().expect("parent")).expect("create dirs");
        std::fs::write(path, markdown).expect("write page");
    }

    #[test]
    fn archived_pages_are_excluded_from_default_surfaces() {
        let mut frontmatter = WikiFrontmatter::empty();
        assert!(!excluded_from_default_surfaces(&frontmatter));
        for state in [
            WikiLifecycle::Draft,
            WikiLifecycle::Reviewed,
            WikiLifecycle::Verified,
            WikiLifecycle::Stale,
        ] {
            frontmatter.lifecycle = Some(state);
            assert!(!excluded_from_default_surfaces(&frontmatter), "{state}");
        }
        frontmatter.lifecycle = Some(WikiLifecycle::Archived);
        assert!(excluded_from_default_surfaces(&frontmatter));
    }

    #[test]
    fn candidate_pages_are_excluded_unless_opted_in() {
        let mut frontmatter = WikiFrontmatter::empty();
        frontmatter.lifecycle = Some(WikiLifecycle::Draft);
        frontmatter.candidate = true;

        assert!(excluded_from_default_surfaces(&frontmatter));
        assert!(!excluded_from_surfaces(&frontmatter, true));

        // The opt-in never resurrects archived pages.
        frontmatter.lifecycle = Some(WikiLifecycle::Archived);
        assert!(excluded_from_surfaces(&frontmatter, true));
    }

    #[test]
    fn promote_candidate_page_clears_flag_and_logs() {
        let temp = tempfile::tempdir().expect("tempdir");
        write_page(
            temp.path(),
            "knowledge/concepts/entity.md",
            "---\ntitle: Entity\nlifecycle: draft\ncandidate: true\n---\n\nBody.\n",
        );
        let scope = ScopeIdentity::project("proj");
        let relative = Path::new("knowledge/concepts/entity.md");

        let promoted =
            promote_candidate_page(temp.path(), &scope, relative, "corroborated by 2 pages")
                .expect("promotion");
        assert!(promoted);

        let markdown =
            std::fs::read_to_string(temp.path().join(relative)).expect("read promoted page");
        let parsed = parse_frontmatter(&markdown).expect("parse promoted page");
        assert!(!parsed.metadata.candidate);
        // Promotion clears quarantine only; lifecycle is the librarian's job.
        assert_eq!(parsed.metadata.lifecycle, Some(WikiLifecycle::Draft));

        let log = std::fs::read_to_string(temp.path().join("log.md")).expect("read log");
        assert!(log.contains("candidate_promoted:"), "{log}");
        assert!(log.contains("corroborated by 2 pages"), "{log}");

        // Idempotent: a non-candidate page is left alone with no new entry.
        let repeat =
            promote_candidate_page(temp.path(), &scope, relative, "corroborated by 2 pages")
                .expect("repeat promotion");
        assert!(!repeat);
        let log_after = std::fs::read_to_string(temp.path().join("log.md")).expect("re-read log");
        assert_eq!(log_after.matches("candidate_promoted:").count(), 1);
    }

    #[test]
    fn page_exclusion_reads_vault_files_and_fails_open() {
        let temp = tempfile::tempdir().expect("tempdir");
        write_page(
            temp.path(),
            "knowledge/concepts/kept.md",
            "---\ntitle: Kept\nlifecycle: verified\n---\n\nBody.\n",
        );
        write_page(
            temp.path(),
            "knowledge/concepts/gone.md",
            "---\ntitle: Gone\nlifecycle: archived\n---\n\nBody.\n",
        );
        assert!(!page_excluded_from_default_surfaces(
            temp.path(),
            Path::new("knowledge/concepts/kept.md")
        ));
        assert!(page_excluded_from_default_surfaces(
            temp.path(),
            Path::new("knowledge/concepts/gone.md")
        ));
        // Missing pages fail open: never hide results on I/O trouble.
        assert!(!page_excluded_from_default_surfaces(
            temp.path(),
            Path::new("knowledge/concepts/missing.md")
        ));
    }

    #[test]
    fn transition_rewrites_frontmatter_and_appends_log_entry() {
        let temp = tempfile::tempdir().expect("tempdir");
        let relative = Path::new("knowledge/concepts/gcode.md");
        write_page(
            temp.path(),
            "knowledge/concepts/gcode.md",
            "---\ntitle: Gcode\nlifecycle: draft\n---\n\nBody.\n",
        );

        let applied = apply_lifecycle_transition(
            temp.path(),
            &ScopeIdentity::project("proj"),
            relative,
            WikiLifecycle::Reviewed,
            "librarian clean pass",
        )
        .expect("transition")
        .expect("applied");
        assert_eq!(applied.from, Some(WikiLifecycle::Draft));
        assert_eq!(applied.to, WikiLifecycle::Reviewed);

        let markdown = std::fs::read_to_string(temp.path().join(relative)).expect("read page back");
        let parsed = parse_frontmatter(&markdown).expect("parse");
        assert_eq!(parsed.metadata.lifecycle, Some(WikiLifecycle::Reviewed));
        assert_eq!(parsed.metadata.title.as_deref(), Some("Gcode"));

        let log = std::fs::read_to_string(temp.path().join("log.md")).expect("read log");
        assert!(log.contains("lifecycle_transition:"), "{log}");
        assert!(log.contains("draft -> reviewed"), "{log}");
        assert!(log.contains("librarian clean pass"), "{log}");
    }

    #[test]
    fn transition_to_current_state_is_a_no_op() {
        let temp = tempfile::tempdir().expect("tempdir");
        let relative = Path::new("knowledge/concepts/idle.md");
        let original = "---\ntitle: Idle\nlifecycle: verified\n---\n\nBody.\n";
        write_page(temp.path(), "knowledge/concepts/idle.md", original);

        let applied = apply_lifecycle_transition(
            temp.path(),
            &ScopeIdentity::project("proj"),
            relative,
            WikiLifecycle::Verified,
            "audit pass",
        )
        .expect("transition");
        assert!(applied.is_none());
        let markdown = std::fs::read_to_string(temp.path().join(relative)).expect("read page back");
        assert_eq!(markdown, original);
        assert!(!temp.path().join("log.md").exists());
    }

    #[test]
    fn stale_transition_keeps_reason_and_records_stale_at() {
        let temp = tempfile::tempdir().expect("tempdir");
        let relative = Path::new("knowledge/concepts/aging.md");
        write_page(
            temp.path(),
            "knowledge/concepts/aging.md",
            "---\ntitle: Aging\nlifecycle: verified\n---\n\nBody.\n",
        );

        let applied = apply_lifecycle_transition(
            temp.path(),
            &ScopeIdentity::project("proj"),
            relative,
            WikiLifecycle::Stale,
            "stale_after due",
        )
        .expect("transition")
        .expect("applied");
        assert_eq!(applied.from, Some(WikiLifecycle::Verified));

        let markdown = std::fs::read_to_string(temp.path().join(relative)).expect("read page back");
        let parsed = parse_frontmatter(&markdown).expect("parse");
        assert_eq!(parsed.metadata.lifecycle, Some(WikiLifecycle::Stale));
        assert_eq!(
            parsed.metadata.unknown.get("stale"),
            Some(&serde_json::Value::Bool(true))
        );
        assert_eq!(
            parsed
                .metadata
                .unknown
                .get("stale_reason")
                .and_then(serde_json::Value::as_str),
            Some("stale_after due")
        );
        let stale_at = parsed
            .metadata
            .unknown
            .get("stale_at")
            .and_then(serde_json::Value::as_str)
            .expect("stale_at");
        let log = std::fs::read_to_string(temp.path().join("log.md")).expect("read log");
        assert!(log.contains(stale_at), "{log}");
    }
}
