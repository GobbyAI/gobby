use std::collections::BTreeSet;
use std::path::Path;

use crate::frontmatter::WikiLifecycle;
use crate::support::scope::scope_includes_page;
use crate::{CommandOutcome, ScopeSelection, WikiError, audit, lint};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    super::run_analysis_command(
        "audit",
        selection,
        "serialize audit report",
        |root, output_scope| {
            let report =
                audit::run_with_options(root, output_scope, audit::AuditOptions::from_env())?;
            promote_verified_lifecycle(root, &report)?;
            Ok(report)
        },
        audit::render_text,
    )
}

/// An audit citation pass promotes `reviewed` pages with zero unsupported
/// claims to `verified`, and clears the `candidate` quarantine flag on
/// candidate pages with zero unsupported claims (#17727) — a clean audit is
/// corroboration. Only the `gwiki audit` command mutates; trust and
/// librarian call `audit::run_with_options` directly and stay read-only.
/// Legacy pages without a lifecycle field are never touched, and `draft`
/// pages must earn `reviewed` from the librarian first.
fn promote_verified_lifecycle(
    vault_root: &Path,
    report: &audit::AuditReport,
) -> Result<(), WikiError> {
    let unsupported: BTreeSet<&Path> = report
        .unsupported_claims
        .iter()
        .map(|claim| claim.path.as_path())
        .collect();
    for page in lint::collect_pages(vault_root)? {
        if !scope_includes_page(&report.scope, &page.relative_path) {
            continue;
        }
        if unsupported.contains(page.relative_path.as_path()) {
            continue;
        }
        if page.parsed.frontmatter.candidate {
            crate::lifecycle::promote_candidate_page(
                vault_root,
                &report.scope,
                &page.relative_path,
                "audit: citation pass with no unsupported claims",
            )?;
        }
        if page.parsed.frontmatter.lifecycle != Some(WikiLifecycle::Reviewed) {
            continue;
        }
        crate::lifecycle::apply_lifecycle_transition(
            vault_root,
            &report.scope,
            &page.relative_path,
            WikiLifecycle::Verified,
            "audit: citation pass with no unsupported claims",
        )?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use super::*;
    use crate::ScopeIdentity;
    use crate::frontmatter::parse_frontmatter;

    fn write_page(root: &Path, relative: &str, markdown: &str) {
        let path = root.join(relative);
        std::fs::create_dir_all(path.parent().expect("page parent")).expect("create parent");
        std::fs::write(path, markdown).expect("write page");
    }

    fn report_with_unsupported(root: &Path, unsupported_paths: &[&str]) -> audit::AuditReport {
        let source_context = Arc::new(Vec::new());
        audit::AuditReport {
            command: "audit",
            scope: ScopeIdentity::project("proj"),
            root: root.to_path_buf(),
            claims: Vec::new(),
            unsupported_claims: unsupported_paths
                .iter()
                .map(|path| audit::UnsupportedClaim {
                    path: (*path).into(),
                    line: 1,
                    heading: None,
                    claim: "claim".to_string(),
                    reason: "no citation".to_string(),
                    source_context: Arc::clone(&source_context),
                })
                .collect(),
            source_context,
        }
    }

    #[test]
    fn citation_pass_promotes_reviewed_pages_to_verified() {
        let temp = tempfile::tempdir().expect("tempdir");
        write_page(
            temp.path(),
            "knowledge/concepts/supported.md",
            "---\ntitle: Supported\nlifecycle: reviewed\n---\n\nBody.\n",
        );
        write_page(
            temp.path(),
            "knowledge/concepts/unsupported.md",
            "---\ntitle: Unsupported\nlifecycle: reviewed\n---\n\nBody.\n",
        );
        write_page(
            temp.path(),
            "knowledge/concepts/draft.md",
            "---\ntitle: Draft\nlifecycle: draft\n---\n\nBody.\n",
        );
        let report = report_with_unsupported(temp.path(), &["knowledge/concepts/unsupported.md"]);

        promote_verified_lifecycle(temp.path(), &report).expect("promotion pass");

        let lifecycle_of = |relative: &str| {
            let markdown = std::fs::read_to_string(temp.path().join(relative)).expect("read page");
            parse_frontmatter(&markdown)
                .expect("parse")
                .metadata
                .lifecycle
        };
        assert_eq!(
            lifecycle_of("knowledge/concepts/supported.md"),
            Some(WikiLifecycle::Verified)
        );
        assert_eq!(
            lifecycle_of("knowledge/concepts/unsupported.md"),
            Some(WikiLifecycle::Reviewed)
        );
        assert_eq!(
            lifecycle_of("knowledge/concepts/draft.md"),
            Some(WikiLifecycle::Draft)
        );

        let log = std::fs::read_to_string(temp.path().join("log.md")).expect("read log");
        assert_eq!(log.matches("lifecycle_transition:").count(), 1, "{log}");
        assert!(log.contains("reviewed -> verified"), "{log}");
    }

    #[test]
    fn citation_pass_promotes_clean_candidates_out_of_quarantine() {
        let temp = tempfile::tempdir().expect("tempdir");
        write_page(
            temp.path(),
            "knowledge/concepts/clean-candidate.md",
            "---\ntitle: Clean Candidate\nlifecycle: draft\ncandidate: true\n---\n\nBody.\n",
        );
        write_page(
            temp.path(),
            "knowledge/concepts/unsupported-candidate.md",
            "---\ntitle: Unsupported Candidate\nlifecycle: draft\ncandidate: true\n---\n\nBody.\n",
        );
        let report = report_with_unsupported(
            temp.path(),
            &["knowledge/concepts/unsupported-candidate.md"],
        );

        promote_verified_lifecycle(temp.path(), &report).expect("promotion pass");

        let candidate_of = |relative: &str| {
            let markdown = std::fs::read_to_string(temp.path().join(relative)).expect("read page");
            parse_frontmatter(&markdown)
                .expect("parse")
                .metadata
                .candidate
        };
        assert!(!candidate_of("knowledge/concepts/clean-candidate.md"));
        assert!(candidate_of("knowledge/concepts/unsupported-candidate.md"));

        let log = std::fs::read_to_string(temp.path().join("log.md")).expect("read log");
        assert_eq!(log.matches("candidate_promoted:").count(), 1, "{log}");
    }
}
