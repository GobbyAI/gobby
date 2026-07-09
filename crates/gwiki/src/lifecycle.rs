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
/// every default surface.
pub(crate) fn excluded_from_default_surfaces(frontmatter: &WikiFrontmatter) -> bool {
    matches!(frontmatter.lifecycle, Some(WikiLifecycle::Archived))
}

/// File-reading variant of [`excluded_from_default_surfaces`] for surfaces
/// that only hold a page path. Missing or unparseable pages are NOT excluded:
/// exclusion must never hide results because of an I/O or parse hiccup.
pub(crate) fn page_excluded_from_default_surfaces(vault_root: &Path, relative: &Path) -> bool {
    let path = vault_root.join(relative);
    let Ok(markdown) = std::fs::read_to_string(&path) else {
        return false;
    };
    let Ok(parsed) = parse_frontmatter(&markdown) else {
        return false;
    };
    excluded_from_default_surfaces(&parsed.metadata)
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
            mark_stale_markdown_at(&markdown, reason, &chrono::Utc::now().to_rfc3339()).map_err(
                |error| WikiError::InvalidInput {
                    field: "frontmatter",
                    message: format!("{}: {error}", relative.display()),
                },
            )?
        }
        _ => {
            let mut metadata = parsed.metadata.clone();
            metadata.lifecycle = Some(to);
            if to == WikiLifecycle::Archived {
                metadata
                    .unknown
                    .entry("archived_at".to_string())
                    .or_insert_with(|| serde_json::Value::String(chrono::Utc::now().to_rfc3339()));
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
        assert!(parsed.metadata.unknown.contains_key("stale_at"));
    }
}
