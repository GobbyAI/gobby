//! Reuse validation for render inputs that source hashes cannot see.
//!
//! Module clustering is global: a page whose own file and neighbors are
//! byte-identical can still be assigned a different module this run because
//! *other* files moved, were deleted, or shifted a synthetic cluster's name
//! tokens (#17731). A verbatim-reused page then links a module page the
//! current run does not emit, and the dangling wikilink grows
//! `curated_broken_link_count` on every heal. These helpers parse the
//! deterministic scaffolding the renderer owns and either re-stamp it (file
//! pages) or veto reuse (module pages, where child names also feed the brief
//! prompt).

use std::collections::BTreeSet;

use super::paths::module_wikilink;
use super::types::ModuleLink;

/// Invalidation key recording a file page's deterministic `Module:` link, so
/// the persist gate rewrites a hash-unchanged page whose module assignment
/// moved (the disk write counterpart of [`restamp_file_module_link`]).
pub(crate) fn file_module_link_key(module: &str) -> String {
    format!("module-link:{module}")
}

/// Invalidation key recording a module page's Child Modules link set, so the
/// persist gate rewrites a hash-unchanged parent page after a child cluster
/// rename (the disk write counterpart of
/// [`reused_module_child_links_current`]).
pub(crate) fn module_child_links_key(children: &[ModuleLink]) -> String {
    let mut names = children
        .iter()
        .map(|child| child.module.as_str())
        .collect::<Vec<_>>();
    names.sort_unstable();
    format!("child-links:{}", names.join("\n"))
}

/// Re-stamp the deterministic `Module:` link of a reused file page with the
/// module computed this run.
///
/// Returns the page unchanged when the link already matches, the re-stamped
/// page when it does not, and `None` when no recognizable module link exists —
/// the caller must fall back to regeneration so the invariant "every file page
/// links an emitted module page" holds either way. The body is untouched: file
/// narratives never receive the module name as a prompt input, so the link is
/// the only module-dependent content.
pub(crate) fn restamp_file_module_link(page: &str, module: &str) -> Option<String> {
    // The header line renders as `Module: [[...]]`; markdown normalization may
    // wrap the link onto the following line. Child-module list entries render
    // as `- Module: [[...]]` and never start a line with `Module:`, so the
    // first line-anchored occurrence is the header.
    let marker_start = if page.starts_with("Module:") {
        0
    } else {
        page.find("\nModule:")? + 1
    };
    let after_marker = marker_start + "Module:".len();
    let rest = &page[after_marker..];
    let skipped = rest.len() - rest.trim_start().len();
    let whitespace = &rest[..skipped];
    // At most single-line wrapping separates the marker from its link; a blank
    // line means whatever follows is a separate paragraph, not the header link.
    if whitespace.contains("\n\n") {
        return None;
    }
    let link_start = after_marker + skipped;
    if !page[link_start..].starts_with("[[") {
        return None;
    }
    let link_end = link_start + page[link_start..].find("]]")? + 2;
    let target = &page[link_start + 2..link_end - 2];
    if !(target.starts_with("code/modules/") || target.starts_with("code/repo|")) {
        return None;
    }
    let replacement = if module.is_empty() {
        "[[code/repo|Repository Overview]]".to_string()
    } else {
        module_wikilink(module)
    };
    if page[link_start..link_end] == replacement {
        return Some(page.to_string());
    }
    let mut updated = String::with_capacity(page.len() + replacement.len());
    updated.push_str(&page[..link_start]);
    updated.push_str(&replacement);
    updated.push_str(&page[link_end..]);
    Some(updated)
}

/// Whether a reused module page's `## Child Modules` links still name exactly
/// the child modules computed this run.
///
/// A child cluster rename keeps the parent's member-file span set (same files,
/// new module name), so hash-based reuse cannot see it. Child names also feed
/// the parent's brief prompt, so a mismatch must regenerate the page rather
/// than patch links.
pub(crate) fn reused_module_child_links_current(page: &str, children: &[ModuleLink]) -> bool {
    let expected = children
        .iter()
        .map(|child| format!("code/modules/{}", child.module))
        .collect::<BTreeSet<_>>();
    let Some(heading) = page.find("\n## Child Modules") else {
        return expected.is_empty();
    };
    let body_start = heading + "\n## Child Modules".len();
    let body_end = page[body_start..]
        .find("\n## ")
        .map_or(page.len(), |relative| body_start + relative);
    let mut found = BTreeSet::new();
    let mut rest = &page[body_start..body_end];
    while let Some(start) = rest.find("[[code/modules/") {
        let after = &rest[start + 2..];
        let Some(end) = after.find(['|', ']']) else {
            break;
        };
        found.insert(after[..end].to_string());
        rest = &after[end..];
    }
    found == expected
}

#[cfg(test)]
mod tests {
    use super::*;

    fn child(module: &str) -> ModuleLink {
        ModuleLink {
            module: module.to_string(),
            summary: String::new(),
            source_spans: Vec::new(),
        }
    }

    #[test]
    fn restamp_replaces_stale_module_link_on_one_line() {
        let page = "# src/db/ids.rs\n\nModule: [[code/modules/src/graph_sync|src/graph_sync]]\n\n## Overview\n\nBody.\n";
        let updated = restamp_file_module_link(page, "src/db").expect("restamp");
        assert!(updated.contains("Module: [[code/modules/src/db|src/db]]"));
        assert!(!updated.contains("graph_sync"));
        assert!(updated.contains("## Overview\n\nBody."));
    }

    #[test]
    fn restamp_replaces_stale_module_link_wrapped_by_normalization() {
        let page = "# crates/gwiki/src/vector.rs\n\nModule:\n[[code/modules/crates/gwiki/src/execute_source|crates/gwiki/src/execute_source]]\n\n## Overview\n\nBody.\n";
        let updated =
            restamp_file_module_link(page, "crates/gwiki/src/source_execute").expect("restamp");
        assert!(updated.contains(
            "[[code/modules/crates/gwiki/src/source_execute|crates/gwiki/src/source_execute]]"
        ));
        assert!(!updated.contains("execute_source"));
    }

    #[test]
    fn restamp_switches_between_repo_overview_and_named_module() {
        let rooted = "# build.rs\n\nModule: [[code/repo|Repository Overview]]\n\nBody.\n";
        let updated = restamp_file_module_link(rooted, "src/build").expect("restamp");
        assert!(updated.contains("Module: [[code/modules/src/build|src/build]]"));

        let named = "# build.rs\n\nModule: [[code/modules/src/build|src/build]]\n\nBody.\n";
        let updated = restamp_file_module_link(named, "").expect("restamp");
        assert!(updated.contains("Module: [[code/repo|Repository Overview]]"));
    }

    #[test]
    fn restamp_keeps_matching_link_byte_identical_and_ignores_body_links() {
        let page = "# src/a.rs\n\nModule: [[code/modules/src|src]]\n\nSee also\n[[code/modules/src/other|src/other]] in the body.\n";
        let updated = restamp_file_module_link(page, "src").expect("restamp");
        assert_eq!(updated, page);
    }

    #[test]
    fn restamp_rejects_pages_without_a_recognizable_module_link() {
        assert!(restamp_file_module_link("# src/a.rs\n\nBody only.\n", "src").is_none());
        assert!(
            restamp_file_module_link("# src/a.rs\n\nModule: plain text, no link.\n", "src")
                .is_none()
        );
        // A blank line after the marker means the next link is body content.
        assert!(
            restamp_file_module_link("# src/a.rs\n\nModule:\n\n[[code/modules/src|src]]\n", "src")
                .is_none()
        );
    }

    #[test]
    fn child_links_current_accepts_matching_set_in_any_order() {
        let page = "# src\n\nParent: [[code/repo|Repository Overview]]\n\n## Child Modules\n\n- Module: [[code/modules/src/b|src/b]]\n  Summary: b.\n- Module:\n  [[code/modules/src/a|src/a]]\n  Summary: a.\n\n## Files\n\n- [[code/files/src/lib.rs|src/lib.rs]]\n";
        assert!(reused_module_child_links_current(
            page,
            &[child("src/a"), child("src/b")]
        ));
    }

    #[test]
    fn child_links_current_rejects_renamed_or_missing_children() {
        let page = "# src\n\n## Child Modules\n\n- Module: [[code/modules/src/source_execute|src/source_execute]]\n  Summary: cluster.\n";
        // Renamed child cluster: page names the old module.
        assert!(!reused_module_child_links_current(
            page,
            &[child("src/execute_source")]
        ));
        // Dropped child: page still lists one.
        assert!(!reused_module_child_links_current(page, &[]));
        // Added child: page lists none of it.
        assert!(!reused_module_child_links_current(
            page,
            &[child("src/source_execute"), child("src/new")]
        ));
    }

    #[test]
    fn child_links_current_without_section_requires_no_children() {
        let page = "# src/leaf\n\n## Overview\n\nNo children here.\n\n## Files\n\n- [[code/files/src/leaf/a.rs|src/leaf/a.rs]]\n";
        assert!(reused_module_child_links_current(page, &[]));
        assert!(!reused_module_child_links_current(page, &[child("src/x")]));
    }
}
