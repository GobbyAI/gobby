//! The `gwiki lint` report verb.
//!
//! Page collection, report shaping, and rendering live here; the checks
//! themselves (wikilink resolution, orphan/backlink/duplicate-alias
//! detection, Mermaid validity and grounding) are the shared vault lint core
//! in `gobby_core::vault::lint` (#17514), so gcode's write-time prevention
//! and this report verb cannot drift.

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use gobby_core::vault::lint::{LintPage, PageAuthorship, page_targets, run_checks};
use serde::Serialize;

use crate::frontmatter::{WikiFrontmatter, parse_frontmatter};
use crate::markdown::{MarkdownDomainRecord, parse_markdown_with_canonical_targets};
use crate::{ScopeIdentity, WikiError};

pub(crate) use gobby_core::vault::lint::line_number;
pub use gobby_core::vault::lint::{DiagramIssue, DuplicateAlias, LinkIssue};

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct LintReport {
    pub command: &'static str,
    pub scope: ScopeIdentity,
    pub root: PathBuf,
    pub broken_links: Vec<LinkIssue>,
    pub orphan_pages: Vec<PathBuf>,
    pub missing_frontmatter: Vec<PathBuf>,
    pub duplicate_aliases: Vec<DuplicateAlias>,
    pub missing_backlinks: Vec<LinkIssue>,
    pub invalid_diagrams: Vec<DiagramIssue>,
}

pub fn run(vault_root: &Path, scope: ScopeIdentity) -> Result<LintReport, WikiError> {
    let pages = collect_pages(vault_root)?;
    Ok(report_from_pages(vault_root, scope, &pages))
}

pub(crate) fn report_from_pages(
    vault_root: &Path,
    scope: ScopeIdentity,
    pages: &[WikiPage],
) -> LintReport {
    let lint_pages: Vec<LintPage<'_>> = pages
        .iter()
        .map(|page| LintPage {
            relative_path: &page.relative_path,
            markdown: &page.markdown,
            title: page.parsed.frontmatter.title.as_deref(),
            display_title: title_for_page(page),
            aliases: &page.parsed.frontmatter.aliases,
            links: &page.parsed.links,
            has_frontmatter: page.has_frontmatter,
            authorship: page_authorship(page),
        })
        .collect();
    let outcome = run_checks(&lint_pages, None);

    LintReport {
        command: "lint",
        scope,
        root: vault_root.to_path_buf(),
        broken_links: outcome.broken_links,
        orphan_pages: outcome.orphan_pages,
        missing_frontmatter: outcome.missing_frontmatter,
        duplicate_aliases: outcome.duplicate_aliases,
        missing_backlinks: outcome.missing_backlinks,
        invalid_diagrams: outcome.invalid_diagrams,
    }
}

pub fn render_text(report: &LintReport) -> String {
    let mut text = format!("Wiki lint report\nScope: {}\n", report.scope);
    render_link_issues(&mut text, "Broken links", &report.broken_links);
    render_paths(&mut text, "Orphan pages", &report.orphan_pages);
    render_paths(
        &mut text,
        "Missing frontmatter",
        &report.missing_frontmatter,
    );
    if !report.duplicate_aliases.is_empty() {
        text.push_str("\nDuplicate aliases:\n");
        for alias in &report.duplicate_aliases {
            text.push_str("- ");
            text.push_str(&alias.alias);
            text.push_str(": ");
            text.push_str(&join_paths(&alias.paths));
            text.push('\n');
        }
    }
    render_link_issues(&mut text, "Missing backlinks", &report.missing_backlinks);
    render_diagram_issues(&mut text, &report.invalid_diagrams);
    text
}

#[derive(Debug, Clone)]
pub(crate) struct WikiPage {
    pub path: PathBuf,
    pub relative_path: PathBuf,
    pub markdown: String,
    pub parsed: MarkdownDomainRecord,
    pub has_frontmatter: bool,
}

/// Every canonical key a wikilink could use to reach this page: its
/// extensionless relative path, file stem, title, and aliases. Shared by
/// upkeep candidate governance and health backlink counting.
pub(crate) fn page_match_keys(page: &WikiPage) -> BTreeSet<String> {
    use crate::links::canonical_target_key;

    let mut keys = BTreeSet::new();
    if let Some(relative) = page.relative_path.with_extension("").to_str() {
        keys.insert(canonical_target_key(relative));
    }
    if let Some(stem) = page
        .relative_path
        .file_stem()
        .and_then(|stem| stem.to_str())
    {
        keys.insert(canonical_target_key(stem));
    }
    if let Some(title) = &page.parsed.frontmatter.title {
        keys.insert(canonical_target_key(title));
    }
    for alias in &page.parsed.frontmatter.aliases {
        keys.insert(canonical_target_key(alias));
    }
    keys.remove("");
    keys
}

pub(crate) fn collect_pages(vault_root: &Path) -> Result<Vec<WikiPage>, WikiError> {
    let mut raw_pages = Vec::new();
    // `recaps/` pages are first-class vault pages: their digest links must
    // count as citations in the health index and lint their hygiene like any
    // other page (#17506 wired the writer; this wires the readers).
    for root_name in ["knowledge", "code", "recaps"] {
        let page_root = vault_root.join(root_name);
        if page_root.exists() {
            collect_markdown_files(vault_root, &page_root, &mut raw_pages)?;
        }
    }
    raw_pages.sort_by(|left, right| left.relative_path.cmp(&right.relative_path));

    let known_targets = known_targets(&raw_pages);
    raw_pages
        .into_iter()
        .map(|raw| {
            let parsed = parse_markdown_with_canonical_targets(
                raw.relative_path.clone(),
                &raw.markdown,
                &known_targets,
            )
            .map_err(|error| WikiError::InvalidInput {
                field: "markdown",
                message: format!("{}: {error}", raw.relative_path.display()),
            })?;
            Ok(WikiPage {
                path: raw.path,
                relative_path: raw.relative_path,
                markdown: raw.markdown,
                parsed,
                has_frontmatter: raw.has_frontmatter,
            })
        })
        .collect()
}

pub(crate) fn relative_path(root: &Path, path: &Path) -> PathBuf {
    path.strip_prefix(root).unwrap_or(path).to_path_buf()
}

pub(crate) fn title_for_page(page: &WikiPage) -> String {
    page.parsed
        .frontmatter
        .title
        .clone()
        .or_else(|| {
            page.path
                .file_stem()
                .and_then(|stem| stem.to_str())
                .map(ToOwned::to_owned)
        })
        .unwrap_or_else(|| page.relative_path.display().to_string())
}

/// Ingested source digests live under this vault directory
/// (`paths::derived_markdown_path`).
const SOURCES_ROOT: &str = "knowledge/sources";

/// Classify a page for backlink-reciprocity semantics (#17806): machine-
/// regenerated pages neither create nor bear backlink obligations, because
/// the next regeneration erases any hand-added reciprocal link. A page is
/// generated when it is stamped `generated_by` (codewiki file/module/repo
/// pages, gwiki catalog indexes), carries a codewiki `type: code_*` marker
/// (aggregate/narrative/concept pages predate universal `generated_by`
/// stamping), or lives in a machine-written vault namespace (recap digests,
/// ingested source digests). Everything else — concepts, topics, operator
/// pages — is curated and keeps full reciprocity checking.
fn page_authorship(page: &WikiPage) -> PageAuthorship {
    let frontmatter = &page.parsed.frontmatter;
    let codewiki_typed = frontmatter
        .unknown
        .get("type")
        .and_then(serde_json::Value::as_str)
        .is_some_and(|page_type| page_type.starts_with("code_"));
    if frontmatter.generated_by.is_some()
        || codewiki_typed
        || page
            .relative_path
            .starts_with(crate::recap::RECAPS_DIRECTORY)
        || page.relative_path.starts_with(SOURCES_ROOT)
    {
        PageAuthorship::Generated
    } else {
        PageAuthorship::Curated
    }
}

fn collect_markdown_files(
    vault_root: &Path,
    directory: &Path,
    pages: &mut Vec<RawWikiPage>,
) -> Result<(), WikiError> {
    let entries = match fs::read_dir(directory) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => {
            return Err(WikiError::Io {
                action: "read wiki directory",
                path: Some(directory.to_path_buf()),
                source: error,
            });
        }
    };

    for entry in entries {
        let entry = entry.map_err(|error| WikiError::Io {
            action: "read wiki directory entry",
            path: Some(directory.to_path_buf()),
            source: error,
        })?;
        let path = entry.path();
        let file_type = entry.file_type().map_err(|error| WikiError::Io {
            action: "read wiki file type",
            path: Some(path.clone()),
            source: error,
        })?;
        if file_type.is_dir() {
            collect_markdown_files(vault_root, &path, pages)?;
        } else if file_type.is_file() && is_markdown_path(&path) {
            let markdown = fs::read_to_string(&path).map_err(|error| WikiError::Io {
                action: "read wiki markdown",
                path: Some(path.clone()),
                source: error,
            })?;
            let (frontmatter, has_frontmatter) = {
                let parsed =
                    parse_frontmatter(&markdown).map_err(|error| WikiError::InvalidInput {
                        field: "frontmatter",
                        message: format!("{}: {error}", path.display()),
                    })?;
                (parsed.metadata, parsed.format.is_some())
            };
            let relative_path = relative_path(vault_root, &path);
            pages.push(RawWikiPage {
                path,
                relative_path,
                markdown,
                frontmatter,
                has_frontmatter,
            });
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

fn known_targets(raw_pages: &[RawWikiPage]) -> BTreeSet<String> {
    let mut targets = BTreeSet::new();
    for page in raw_pages {
        targets.extend(page_targets(
            &page.relative_path,
            page.frontmatter.title.as_deref(),
            &page.frontmatter.aliases,
        ));
    }
    targets
}

fn render_link_issues(text: &mut String, heading: &str, issues: &[LinkIssue]) {
    text.push('\n');
    text.push_str(heading);
    text.push_str(":\n");
    if issues.is_empty() {
        text.push_str("- none\n");
        return;
    }
    for issue in issues {
        text.push_str("- ");
        text.push_str(&issue.path.display().to_string());
        text.push(':');
        text.push_str(&issue.line.to_string());
        text.push_str(" -> ");
        text.push_str(&issue.target);
        text.push_str(" (");
        text.push_str(&issue.kind);
        text.push_str(")\n");
    }
}

fn render_diagram_issues(text: &mut String, issues: &[DiagramIssue]) {
    text.push_str("\nInvalid diagrams:\n");
    if issues.is_empty() {
        text.push_str("- none\n");
        return;
    }
    for issue in issues {
        text.push_str("- ");
        text.push_str(&issue.path.display().to_string());
        text.push(':');
        text.push_str(&issue.line.to_string());
        text.push_str(" -> ");
        text.push_str(&issue.reason);
        text.push('\n');
    }
}

fn render_paths(text: &mut String, heading: &str, paths: &[PathBuf]) {
    text.push('\n');
    text.push_str(heading);
    text.push_str(":\n");
    if paths.is_empty() {
        text.push_str("- none\n");
        return;
    }
    for path in paths {
        text.push_str("- ");
        text.push_str(&path.display().to_string());
        text.push('\n');
    }
}

fn join_paths(paths: &[PathBuf]) -> String {
    paths
        .iter()
        .map(|path| path.display().to_string())
        .collect::<Vec<_>>()
        .join(", ")
}

#[derive(Debug, Clone)]
struct RawWikiPage {
    path: PathBuf,
    relative_path: PathBuf,
    markdown: String,
    frontmatter: WikiFrontmatter,
    has_frontmatter: bool,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_broken_links_and_orphans() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "knowledge/topics/home.md",
            "---\ntitle: Home\n---\n# Home\nSee [[Linked]], [linked](linked.md), [[Missing]], and [gone](missing.md).\n",
        );
        write_page(
            root,
            "knowledge/topics/linked.md",
            "---\ntitle: Linked\n---\n# Linked\nBack to [[Home]].\n",
        );
        write_page(
            root,
            "knowledge/topics/orphan.md",
            "---\ntitle: Orphan\n---\n# Orphan\nNo inbound links.\n",
        );

        let report = run(root, ScopeIdentity::topic("ops")).expect("lint runs");

        assert_eq!(report.broken_links.len(), 2);
        assert_eq!(
            report.broken_links[0].path,
            PathBuf::from("knowledge/topics/home.md")
        );
        assert_eq!(report.broken_links[0].target, "Missing");
        assert_eq!(report.broken_links[1].target, "missing.md");
        assert_eq!(
            report.orphan_pages,
            vec![PathBuf::from("knowledge/topics/orphan.md")]
        );
    }

    #[test]
    fn absolute_filesystem_targets_stay_reported_as_broken_links() {
        // #17649 excludes absolute filesystem targets from page-creation
        // suggestion surfaces; lint must keep reporting them as broken links
        // so the librarian's repair check still covers them.
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "knowledge/sources/src-0011223344556677-session.md",
            "---\ntitle: Session digest\n---\n# Session digest\nSee [note](/private/tmp/claude-501/scratchpad/note-orchid.md).\n",
        );

        let report = run(root, ScopeIdentity::topic("ops")).expect("lint runs");

        assert_eq!(report.broken_links.len(), 1);
        assert_eq!(
            report.broken_links[0].target,
            "/private/tmp/claude-501/scratchpad/note-orchid.md"
        );
    }

    #[test]
    fn link_targets_resolve_case_insensitively() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "knowledge/topics/Gcode.md",
            "---\ntitle: Gcode\n---\n# Gcode\nSee [[home]].\n",
        );
        write_page(
            root,
            "knowledge/topics/home.md",
            "---\ntitle: Home\n---\n# Home\nSee [[gcode]], [[GCODE]], [[Knowledge/Topics/GCode]], and [gcode](Gcode.md).\n",
        );

        let report = run(root, ScopeIdentity::topic("ops")).expect("lint runs");

        assert!(report.broken_links.is_empty(), "{:?}", report.broken_links);
    }

    #[test]
    fn nested_wikilinks_resolve_relative_to_current_subtree() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "code/repo.md",
            "---\ntitle: Repository Overview\n---\n# Repository Overview\n[[code/modules/crates|crates]]\n",
        );
        write_page(
            root,
            "code/modules/crates.md",
            "---\ntitle: crates\n---\n# crates\n[[../repo|Repository Overview]]\n",
        );

        let report = run(root, ScopeIdentity::topic("lint")).expect("lint runs");

        assert!(report.broken_links.is_empty(), "{:?}", report.broken_links);
    }

    #[test]
    fn lint_is_read_only() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let relative = "knowledge/topics/home.md";
        write_page(
            root,
            relative,
            "---\ntitle: Home\n---\n# Home\nSee [[Missing]].\n",
        );
        let page = root.join(relative);
        let before = std::fs::read_to_string(&page).expect("read before");

        let _report = run(root, ScopeIdentity::topic("ops")).expect("lint runs");

        assert_eq!(std::fs::read_to_string(&page).expect("read after"), before);
        assert!(!root.join("meta/health/latest.json").exists());
    }

    #[test]
    fn navigation_root_links_are_exempt_from_missing_backlinks() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        // The repo front page links out to a content page that never links
        // back. As a navigation root it is exempt, so this produces no
        // missing_backlink.
        write_page(
            root,
            "code/repo.md",
            "---\ntitle: Repository Overview\n---\n# Repository Overview\nStart with [[Introduction]].\n",
        );
        write_page(
            root,
            "code/narrative/introduction.md",
            "---\ntitle: Introduction\n---\n# Introduction\nNo link back.\n",
        );

        let report = run(root, ScopeIdentity::topic("ops")).expect("lint runs");

        assert!(
            report.missing_backlinks.is_empty(),
            "{:?}",
            report.missing_backlinks
        );
    }

    #[test]
    fn content_page_links_still_require_reciprocity() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        // A content page (not a navigation root) linking out without a
        // reciprocal link is still flagged, even under `code/`.
        write_page(
            root,
            "code/narrative/introduction.md",
            "---\ntitle: Introduction\n---\n# Introduction\nContinue to [[Architecture]].\n",
        );
        write_page(
            root,
            "code/narrative/architecture.md",
            "---\ntitle: Architecture\n---\n# Architecture\nNo link back.\n",
        );

        let report = run(root, ScopeIdentity::topic("ops")).expect("lint runs");

        assert_eq!(
            report.missing_backlinks.len(),
            1,
            "{:?}",
            report.missing_backlinks
        );
        let issue = &report.missing_backlinks[0];
        assert_eq!(issue.path, PathBuf::from("code/narrative/architecture.md"));
        assert_eq!(issue.kind, "missing_backlink");
        assert_eq!(issue.target, "Introduction");
    }

    #[test]
    fn generated_pages_are_exempt_from_backlink_reciprocity() {
        // The systemic #17806 noise: fan-out links from generated surfaces
        // (catalog indexes, codewiki-typed pages, recap digests, ingested
        // sources) and links pointing at machine-regenerated pages demand no
        // reciprocity. A pre-regen catalog index without frontmatter is
        // exempted by path, and its missing frontmatter stays reported
        // honestly until `catalog::regenerate` stamps it.
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(root, "code/INDEX.md", "# Code\n- [[Widget Module]]\n");
        write_page(
            root,
            "code/modules/widget.md",
            "---\ntitle: \"Widget Module\"\ntype: code_module\n---\n# Widget\nUses [[Helper Module]].\n",
        );
        write_page(
            root,
            "code/modules/helper.md",
            "---\ntitle: \"Helper Module\"\ngenerated_by: gcode-codewiki\n---\n# Helper\nNo links back.\n",
        );
        write_page(
            root,
            "recaps/2026-07-15.md",
            "---\ntitle: \"Recap: 2026-07-15\"\n---\n# Recap\nCovered [[Gwiki]].\n",
        );
        write_page(
            root,
            "knowledge/sources/src-0011223344556677-session.md",
            "---\ntitle: \"Session digest\"\n---\n# Digest\nDiscusses [[Gwiki]].\n",
        );
        write_page(
            root,
            "knowledge/concepts/gwiki.md",
            "---\ntitle: \"Gwiki\"\n---\n# Gwiki\nLinks back to nothing generated.\n",
        );

        let report = run(root, ScopeIdentity::topic("ops")).expect("lint runs");

        assert!(
            report.missing_backlinks.is_empty(),
            "{:?}",
            report.missing_backlinks
        );
        assert_eq!(
            report.missing_frontmatter,
            vec![PathBuf::from("code/INDEX.md")]
        );
    }

    #[test]
    fn curated_one_way_links_still_reported_amid_generated_noise() {
        // The genuine finding the semantic rule must preserve: a curated
        // concept linking a curated concept that never links back.
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "recaps/2026-07-15.md",
            "---\ntitle: \"Recap\"\n---\n# Recap\nCovered [[Gwiki]] and [[Qdrant]].\n",
        );
        write_page(
            root,
            "knowledge/concepts/gwiki.md",
            "---\ntitle: \"Gwiki\"\n---\n# Gwiki\nRelated to [[Qdrant]].\n",
        );
        write_page(
            root,
            "knowledge/concepts/qdrant.md",
            "---\ntitle: \"Qdrant\"\n---\n# Qdrant\nNo link back.\n",
        );

        let report = run(root, ScopeIdentity::topic("ops")).expect("lint runs");

        assert_eq!(
            report.missing_backlinks.len(),
            1,
            "{:?}",
            report.missing_backlinks
        );
        assert_eq!(
            report.missing_backlinks[0].path,
            PathBuf::from("knowledge/concepts/qdrant.md")
        );
        assert_eq!(report.missing_backlinks[0].target, "Gwiki");
    }

    #[test]
    fn grounded_valid_diagram_passes_lint() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "code/concepts/pipeline.md",
            r#"---
title: Pipeline
---
# Pipeline
The parser feeds the chunker.

```mermaid
flowchart LR
    s0["parser — builds AST"]
    s1["chunker — splits content"]
    s0 --> s1
```
"#,
        );

        let report = run(root, ScopeIdentity::topic("code")).expect("lint runs");

        assert!(
            report.invalid_diagrams.is_empty(),
            "{:?}",
            report.invalid_diagrams
        );
    }

    #[test]
    fn ungrounded_node_is_flagged_not_silently_kept() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "code/concepts/flow.md",
            r#"---
title: Flow
---
# Flow
The parser feeds downstream stages.

```mermaid
flowchart LR
    s0["parser — builds AST"]
    s1["telemetry — emits metrics"]
    s0 --> s1
```
"#,
        );

        let report = run(root, ScopeIdentity::topic("code")).expect("lint runs");

        assert_eq!(report.invalid_diagrams.len(), 1);
        let issue = &report.invalid_diagrams[0];
        assert_eq!(issue.path, PathBuf::from("code/concepts/flow.md"));
        assert!(issue.reason.contains("ungrounded"), "{}", issue.reason);
        assert!(issue.reason.contains("telemetry"), "{}", issue.reason);
    }

    #[test]
    fn malformed_mermaid_is_flagged_invalid() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "code/concepts/broken.md",
            r#"---
title: Broken
---
# Broken
The parser stage.

```mermaid
flowchart LR
    s0["parser
    s0 --> s1
```
"#,
        );

        let report = run(root, ScopeIdentity::topic("code")).expect("lint runs");

        assert_eq!(report.invalid_diagrams.len(), 1);
        assert_eq!(report.invalid_diagrams[0].reason, "invalid-mermaid");
    }

    #[test]
    fn render_text_reports_invalid_diagrams() {
        let report = LintReport {
            command: "lint",
            scope: ScopeIdentity::topic("code"),
            root: PathBuf::from("/vault"),
            broken_links: Vec::new(),
            orphan_pages: Vec::new(),
            missing_frontmatter: Vec::new(),
            duplicate_aliases: Vec::new(),
            missing_backlinks: Vec::new(),
            invalid_diagrams: vec![DiagramIssue {
                path: PathBuf::from("code/concepts/flow.md"),
                line: 7,
                reason: "ungrounded: telemetry".to_string(),
            }],
        };

        let text = render_text(&report);

        assert!(text.contains("Invalid diagrams:"));
        assert!(text.contains("code/concepts/flow.md:7 -> ungrounded: telemetry"));
    }

    fn write_page(root: &Path, relative: &str, markdown: &str) {
        let path = root.join(relative);
        std::fs::create_dir_all(path.parent().expect("page parent")).expect("create parent");
        std::fs::write(path, markdown).expect("write page");
    }
}
