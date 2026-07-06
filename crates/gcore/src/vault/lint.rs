//! Vault-generic lint core shared by both wiki engines.
//!
//! Hosts the shared finding types and pure checks over vault pages: wikilink
//! resolution with broken-link/orphan/backlink/duplicate-alias detection,
//! Mermaid diagram validity and grounding (via [`super::mermaid`]), markdown
//! hygiene against the shared normalizer, frontmatter presence, and index
//! consistency. Engine-specific citation grammars plug in through the
//! [`CitationValidator`] trait (gcode validates `[file:line]` citations
//! against its symbol index; gwiki validates source citations against `raw/`
//! captures and provenance).
//!
//! The checks are pure over [`LintPage`] values: consumers walk the
//! filesystem, parse frontmatter, and extract links with their own domain
//! types, then hand borrowed page views to [`run_checks`]. gwiki's `lint`
//! report verb is the primary consumer; the write-time prevention path in
//! gcode consumes the shared mermaid gate directly.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use serde::Serialize;

use super::links::{LinkKind, WikiLink, canonical_target_key, normalize_wiki_path};
use super::mermaid::{
    grounding_text, is_valid_mermaid, label_is_grounded, mermaid_blocks, node_labels,
};

/// Vault index page name at the vault root.
pub const INDEX_FILE: &str = "_index.md";

/// A borrowed view of one vault page, prepared by the consumer from its own
/// page representation. `display_title` is the human-facing title used in
/// missing-backlink findings; `title` and `aliases` are the frontmatter
/// identity fields that make the page addressable by wikilink.
#[derive(Debug, Clone)]
pub struct LintPage<'a> {
    pub relative_path: &'a Path,
    pub markdown: &'a str,
    pub title: Option<&'a str>,
    pub display_title: String,
    pub aliases: &'a [String],
    pub links: &'a [WikiLink],
    pub has_frontmatter: bool,
}

/// A link-shaped defect: a broken link, a missing backlink, or an index entry
/// that no longer resolves. `kind` names the specific defect.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct LinkIssue {
    pub path: PathBuf,
    pub line: usize,
    pub target: String,
    pub kind: String,
}

/// One alias claimed by more than one page, making wikilink resolution
/// ambiguous.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DuplicateAlias {
    pub alias: String,
    pub paths: Vec<PathBuf>,
}

/// A curated-diagram defect found while linting the generated vault: either a
/// Mermaid block that is not well-formed, or one whose node labels are not
/// grounded in the rest of the page. The generator omits such diagrams (honest
/// no-diagram beats a wrong diagram), so on a freshly refreshed vault this list
/// is empty; a non-empty entry means a wrong diagram leaked into the vault.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DiagramIssue {
    pub path: PathBuf,
    pub line: usize,
    pub reason: String,
}

/// One citation an engine-specific [`CitationValidator`] rejected.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CitationIssue {
    pub path: PathBuf,
    pub line: usize,
    pub citation: String,
    pub reason: String,
}

/// Engine-specific citation validation plugged into [`run_checks`].
///
/// The citation grammar and its ground truth differ per engine — gcode
/// validates `[file:line-line]` citations against the current symbol index,
/// gwiki validates source citations against `raw/` captures and the
/// provenance graph — so the core stays agnostic and delegates per page.
pub trait CitationValidator {
    /// Citation defects found on one page; empty when every citation checks
    /// out.
    fn validate_page(&self, page: &LintPage<'_>) -> Vec<CitationIssue>;
}

/// Aggregated results of the vault-generic checks over one page set.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct VaultLintOutcome {
    pub broken_links: Vec<LinkIssue>,
    pub orphan_pages: Vec<PathBuf>,
    pub missing_frontmatter: Vec<PathBuf>,
    pub duplicate_aliases: Vec<DuplicateAlias>,
    pub missing_backlinks: Vec<LinkIssue>,
    pub invalid_diagrams: Vec<DiagramIssue>,
    pub dirty_markdown: Vec<PathBuf>,
    pub citation_issues: Vec<CitationIssue>,
}

/// Run every vault-generic check over `pages`, delegating citation validation
/// to `citations` when an engine supplies one.
pub fn run_checks(
    pages: &[LintPage<'_>],
    citations: Option<&dyn CitationValidator>,
) -> VaultLintOutcome {
    let target_map = target_map(pages);
    let mut broken_links = Vec::new();
    let mut inbound: BTreeMap<PathBuf, usize> = pages
        .iter()
        .map(|page| (page.relative_path.to_path_buf(), 0))
        .collect();
    let mut outgoing: BTreeMap<PathBuf, BTreeSet<PathBuf>> = pages
        .iter()
        .map(|page| (page.relative_path.to_path_buf(), BTreeSet::new()))
        .collect();

    for page in pages {
        for link in page.links {
            if ignored_target(&link.target) {
                continue;
            }
            if let Some(target_path) = link_lookup_targets(page.relative_path, link)
                .iter()
                .find_map(|lookup_target| target_map.get(lookup_target))
            {
                if target_path != page.relative_path {
                    *inbound.entry(target_path.clone()).or_default() += 1;
                    outgoing
                        .entry(page.relative_path.to_path_buf())
                        .or_default()
                        .insert(target_path.clone());
                }
            } else {
                broken_links.push(LinkIssue {
                    path: page.relative_path.to_path_buf(),
                    line: line_number(page.markdown, link.byte_start),
                    target: link.target.clone(),
                    kind: link_kind(link.kind).to_string(),
                });
            }
        }
    }

    let mut orphan_pages: Vec<PathBuf> = inbound
        .into_iter()
        .filter_map(|(path, count)| (count == 0 && !is_orphan_exempt(&path)).then_some(path))
        .collect();
    orphan_pages.sort();

    let mut missing_frontmatter: Vec<PathBuf> = pages
        .iter()
        .filter(|page| !page.has_frontmatter)
        .map(|page| page.relative_path.to_path_buf())
        .collect();
    missing_frontmatter.sort();

    let citation_issues = citations
        .map(|validator| {
            pages
                .iter()
                .flat_map(|page| validator.validate_page(page))
                .collect()
        })
        .unwrap_or_default();

    VaultLintOutcome {
        broken_links,
        orphan_pages,
        missing_frontmatter,
        duplicate_aliases: duplicate_aliases(pages),
        missing_backlinks: missing_backlinks(pages, &outgoing),
        invalid_diagrams: invalid_diagrams(pages),
        dirty_markdown: markdown_hygiene(pages),
        citation_issues,
    }
}

/// Collect curated-diagram defects across every page: a Mermaid block that is not
/// well-formed, or one whose node labels are not grounded in the surrounding page
/// prose. The generator already omits such diagrams (honest no-diagram beats a
/// wrong diagram), so this is a backstop - a real entry means a wrong diagram
/// leaked into the vault and should be omitted at the source instead.
pub fn invalid_diagrams(pages: &[LintPage<'_>]) -> Vec<DiagramIssue> {
    let mut issues = Vec::new();
    for page in pages {
        let grounding = grounding_text(page.markdown);
        for block in mermaid_blocks(page.markdown) {
            if !is_valid_mermaid(&block.text) {
                issues.push(DiagramIssue {
                    path: page.relative_path.to_path_buf(),
                    line: block.line,
                    reason: "invalid-mermaid".to_string(),
                });
                continue;
            }
            let ungrounded: Vec<String> = node_labels(&block.text)
                .into_iter()
                .filter(|label| !label_is_grounded(label, &grounding))
                .collect();
            if !ungrounded.is_empty() {
                issues.push(DiagramIssue {
                    path: page.relative_path.to_path_buf(),
                    line: block.line,
                    reason: format!("ungrounded: {}", ungrounded.join(", ")),
                });
            }
        }
    }
    issues
}

/// Pages whose markdown is not a fixed point of the shared normalizer — the
/// vault-generic markdown-hygiene check. A dirty page is one `normalize` (or
/// codewiki regeneration) would rewrite.
pub fn markdown_hygiene(pages: &[LintPage<'_>]) -> Vec<PathBuf> {
    let mut dirty: Vec<PathBuf> = pages
        .iter()
        .filter(|page| crate::markdown::normalize_markdown(page.markdown) != page.markdown)
        .map(|page| page.relative_path.to_path_buf())
        .collect();
    dirty.sort();
    dirty
}

/// True when the document opens with a properly closed YAML/TOML frontmatter
/// block — the pure-text frontmatter presence/shape check for consumers that
/// have not parsed frontmatter into a domain type.
pub fn has_frontmatter_block(markdown: &str) -> bool {
    crate::markdown::frontmatter_body_start(markdown).is_some()
}

/// Check the vault index page against the collected pages: a missing index is
/// itself a finding, and every non-external link in the index must resolve to
/// a collected page. Findings use kind `missing_index` or `index_link`.
pub fn index_consistency(index_markdown: Option<&str>, pages: &[LintPage<'_>]) -> Vec<LinkIssue> {
    let Some(index_markdown) = index_markdown else {
        return vec![LinkIssue {
            path: PathBuf::from(INDEX_FILE),
            line: 1,
            target: INDEX_FILE.to_string(),
            kind: "missing_index".to_string(),
        }];
    };
    let target_map = target_map(pages);
    let index_path = Path::new(INDEX_FILE);
    let links = super::links::extract_links(index_markdown, std::iter::empty::<&str>());
    let mut issues = Vec::new();
    for link in &links {
        if ignored_target(&link.target) {
            continue;
        }
        let resolves = link_lookup_targets(index_path, link)
            .iter()
            .any(|lookup_target| target_map.contains_key(lookup_target));
        if !resolves {
            issues.push(LinkIssue {
                path: index_path.to_path_buf(),
                line: line_number(index_markdown, link.byte_start),
                target: link.target.clone(),
                kind: "index_link".to_string(),
            });
        }
    }
    issues
}

/// 1-based line number of `byte_start` within `markdown`.
pub fn line_number(markdown: &str, byte_start: usize) -> usize {
    markdown[..byte_start.min(markdown.len())]
        .bytes()
        .filter(|byte| *byte == b'\n')
        .count()
        + 1
}

/// Lookup keys a page can be addressed by. Keys are case-folded through
/// [`canonical_target_key`] so link resolution is case-insensitive.
pub fn page_targets(relative_path: &Path, title: Option<&str>, aliases: &[String]) -> Vec<String> {
    let mut targets = Vec::new();
    let relative = relative_path.to_string_lossy().replace('\\', "/");
    targets.push(canonical_target_key(&normalize_wiki_path(&relative)));
    if let Some(file_stem) = relative_path.file_stem().and_then(|stem| stem.to_str()) {
        targets.push(canonical_target_key(&normalize_wiki_path(file_stem)));
    }
    if let Some(title) = title {
        targets.push(canonical_target_key(&normalize_wiki_path(title)));
    }
    for alias in aliases {
        targets.push(canonical_target_key(&normalize_wiki_path(alias)));
    }
    targets
}

fn target_map(pages: &[LintPage<'_>]) -> BTreeMap<String, PathBuf> {
    let mut targets = BTreeMap::new();
    for page in pages {
        for target in page_targets(page.relative_path, page.title, page.aliases) {
            targets
                .entry(target)
                .or_insert_with(|| page.relative_path.to_path_buf());
        }
    }
    targets
}

fn ignored_target(target: &str) -> bool {
    let trimmed = target.trim();
    trimmed.starts_with('#')
        || trimmed.starts_with("//")
        || trimmed.starts_with("\\\\")
        || trimmed.starts_with("mailto:")
        || trimmed.contains("://")
        || trimmed.starts_with("tel:")
}

/// Candidate [`canonical_target_key`] lookup keys for a link, matching the
/// case-folded keys produced by [`page_targets`].
fn link_lookup_targets(relative_path: &Path, link: &WikiLink) -> Vec<String> {
    link_lookup_keys(relative_path, link.kind, &link.normalized_target)
}

/// Candidate [`canonical_target_key`] lookup keys for a normalized target, in
/// resolution order: the vault-root-relative key first, then — unless the
/// target names a reserved top-level vault directory or is absolute — keys
/// joined onto each ancestor directory of the linking page (immediate parent
/// only for markdown links). This is the single vault link-resolution rule;
/// gwiki's graph builders share it so `gwiki link-suggest` cannot drift from
/// `gwiki lint` (#17638).
pub fn link_lookup_keys(
    relative_path: &Path,
    kind: LinkKind,
    normalized_target: &str,
) -> Vec<String> {
    let folded_target = canonical_target_key(normalized_target);
    let mut targets = vec![folded_target.clone()];
    if (kind != LinkKind::Markdown && kind != LinkKind::Wikilink)
        || folded_target.starts_with("knowledge/")
        || folded_target.starts_with("code/")
        || folded_target.starts_with("raw/")
        || folded_target.starts_with("meta/")
        || Path::new(normalized_target).is_absolute()
    {
        return targets;
    }

    let parents: Box<dyn Iterator<Item = &Path> + '_> = if kind == LinkKind::Markdown {
        Box::new(relative_path.parent().into_iter())
    } else {
        Box::new(relative_path.ancestors().skip(1))
    };

    for parent in parents {
        let parent = parent.to_string_lossy().replace('\\', "/");
        if parent.is_empty() {
            continue;
        }
        let candidate = canonical_target_key(&normalize_path_components(&parent, &folded_target));
        if !targets.contains(&candidate) {
            targets.push(candidate);
        }
    }
    targets
}

fn normalize_path_components(parent: &str, target: &str) -> String {
    let mut parts = Vec::new();
    for part in parent
        .split('/')
        .chain(target.split('/'))
        .filter(|part| !part.is_empty() && *part != ".")
    {
        if part == ".." {
            if !parts.is_empty() {
                parts.pop();
            }
        } else {
            parts.push(part);
        }
    }
    parts.join("/")
}

fn is_orphan_exempt(path: &Path) -> bool {
    path.file_stem()
        .and_then(|stem| stem.to_str())
        .is_some_and(|stem| {
            matches!(
                stem.to_ascii_lowercase().as_str(),
                "_index" | "index" | "home" | "readme"
            )
        })
}

/// Pure navigation/index/aggregate roots (the repo front page, the concept
/// index, and the ownership/hotspots/onboarding dashboards) link out to many
/// pages by design, so links *originating* from them never count as missing
/// backlinks. Matched by relative path only (any `.md` extension stripped),
/// never by display title — a content page that merely happens to be named
/// `index` is not exempt (#853D).
fn is_backlink_source_exempt(path: &Path) -> bool {
    let relative = path.to_string_lossy().replace('\\', "/");
    let relative = relative.strip_suffix(".md").unwrap_or(&relative);
    matches!(
        relative,
        "code/repo"
            | "code/concepts/index"
            | "code/_ownership"
            | "code/_hotspots"
            | "code/_onboarding"
    )
}

fn duplicate_aliases(pages: &[LintPage<'_>]) -> Vec<DuplicateAlias> {
    let mut aliases: BTreeMap<String, (String, BTreeSet<PathBuf>)> = BTreeMap::new();
    for page in pages {
        for alias in page.aliases {
            let display_alias = alias.trim().to_string();
            aliases
                .entry(display_alias.to_ascii_lowercase())
                .or_insert_with(|| (display_alias, BTreeSet::new()))
                .1
                .insert(page.relative_path.to_path_buf());
        }
    }
    aliases
        .into_iter()
        .filter_map(|(_, (alias, paths))| {
            // An alias is ambiguous only when distinct pages claim it. Multiple
            // case variants on one page all fold to that single page, so they
            // never make the page ambiguous with itself (#17642).
            (paths.len() > 1).then(|| DuplicateAlias {
                alias,
                paths: paths.into_iter().collect(),
            })
        })
        .collect()
}

fn missing_backlinks(
    pages: &[LintPage<'_>],
    outgoing: &BTreeMap<PathBuf, BTreeSet<PathBuf>>,
) -> Vec<LinkIssue> {
    let titles: BTreeMap<PathBuf, &str> = pages
        .iter()
        .map(|page| {
            (
                page.relative_path.to_path_buf(),
                page.display_title.as_str(),
            )
        })
        .collect();
    let mut issues = Vec::new();
    for (source, targets) in outgoing {
        // Navigation/index/aggregate roots fan out by design; do not require
        // every page they link to link back (#853D).
        if is_backlink_source_exempt(source) {
            continue;
        }
        for target in targets {
            if outgoing
                .get(target)
                .is_some_and(|target_outgoing| target_outgoing.contains(source))
            {
                continue;
            }
            issues.push(LinkIssue {
                path: target.clone(),
                line: 1,
                target: titles
                    .get(source)
                    .map(|title| (*title).to_string())
                    .unwrap_or_else(|| source.display().to_string()),
                kind: "missing_backlink".to_string(),
            });
        }
    }
    issues.sort_by(|left, right| {
        left.path
            .cmp(&right.path)
            .then(left.target.cmp(&right.target))
    });
    issues
}

fn link_kind(kind: LinkKind) -> &'static str {
    match kind {
        LinkKind::Wikilink => "wikilink",
        LinkKind::Markdown => "markdown",
    }
}

#[cfg(test)]
mod tests {
    use super::super::links::extract_links;
    use super::*;

    /// Owned backing storage for a [`LintPage`], since the check API borrows.
    struct OwnedPage {
        relative_path: PathBuf,
        markdown: String,
        title: Option<String>,
        aliases: Vec<String>,
        links: Vec<WikiLink>,
    }

    fn owned_page(relative: &str, markdown: &str, known_targets: &[&str]) -> OwnedPage {
        let title = markdown
            .lines()
            .find_map(|line| line.strip_prefix("title: ").map(str::to_string));
        OwnedPage {
            relative_path: PathBuf::from(relative),
            markdown: markdown.to_string(),
            title,
            aliases: Vec::new(),
            links: extract_links(markdown, known_targets.iter().copied()),
        }
    }

    fn lint_pages(pages: &[OwnedPage]) -> Vec<LintPage<'_>> {
        pages
            .iter()
            .map(|page| LintPage {
                relative_path: &page.relative_path,
                markdown: &page.markdown,
                title: page.title.as_deref(),
                display_title: page
                    .title
                    .clone()
                    .unwrap_or_else(|| page.relative_path.display().to_string()),
                aliases: &page.aliases,
                links: &page.links,
                has_frontmatter: page.markdown.starts_with("---\n"),
            })
            .collect()
    }

    #[test]
    fn detects_broken_links_and_orphans() {
        let pages = vec![
            owned_page(
                "knowledge/topics/home.md",
                "---\ntitle: Home\n---\n# Home\nSee [[Linked]], [linked](linked.md), [[Missing]], and [gone](missing.md).\n",
                &[],
            ),
            owned_page(
                "knowledge/topics/linked.md",
                "---\ntitle: Linked\n---\n# Linked\nBack to [[Home]].\n",
                &[],
            ),
            owned_page(
                "knowledge/topics/orphan.md",
                "---\ntitle: Orphan\n---\n# Orphan\nNo inbound links.\n",
                &[],
            ),
        ];

        let outcome = run_checks(&lint_pages(&pages), None);

        assert_eq!(outcome.broken_links.len(), 2);
        assert_eq!(
            outcome.broken_links[0].path,
            PathBuf::from("knowledge/topics/home.md")
        );
        assert_eq!(outcome.broken_links[0].target, "Missing");
        assert_eq!(outcome.broken_links[1].target, "missing.md");
        assert_eq!(
            outcome.orphan_pages,
            vec![PathBuf::from("knowledge/topics/orphan.md")]
        );
    }

    #[test]
    fn link_targets_resolve_case_insensitively() {
        let pages = vec![
            owned_page(
                "knowledge/topics/Gcode.md",
                "---\ntitle: Gcode\n---\n# Gcode\nSee [[home]].\n",
                &[],
            ),
            owned_page(
                "knowledge/topics/home.md",
                "---\ntitle: Home\n---\n# Home\nSee [[gcode]], [[GCODE]], [[Knowledge/Topics/GCode]], and [gcode](Gcode.md).\n",
                &[],
            ),
        ];

        let outcome = run_checks(&lint_pages(&pages), None);

        assert!(
            outcome.broken_links.is_empty(),
            "{:?}",
            outcome.broken_links
        );
    }

    #[test]
    fn nested_wikilinks_resolve_relative_to_current_subtree() {
        let pages = vec![
            owned_page(
                "code/repo.md",
                "---\ntitle: Repository Overview\n---\n# Repository Overview\n[[code/modules/crates|crates]]\n",
                &[],
            ),
            owned_page(
                "code/modules/crates.md",
                "---\ntitle: crates\n---\n# crates\n[[../repo|Repository Overview]]\n",
                &[],
            ),
        ];

        let outcome = run_checks(&lint_pages(&pages), None);

        assert!(
            outcome.broken_links.is_empty(),
            "{:?}",
            outcome.broken_links
        );
    }

    #[test]
    fn relative_markdown_links_clamp_traversal_at_vault_root() {
        assert_eq!(
            normalize_path_components("knowledge/topics", "../../../outside.md"),
            "outside.md"
        );
    }

    #[test]
    fn link_lookup_keys_short_circuit_reserved_vault_prefixes() {
        // A vault-root-relative target under a reserved directory must never
        // pick up the linking page's directory as a join candidate (#17638).
        assert_eq!(
            link_lookup_keys(
                Path::new("knowledge/concepts/gcode.md"),
                LinkKind::Wikilink,
                "knowledge/sources/src-5966419ee2f6bb38",
            ),
            vec!["knowledge/sources/src-5966419ee2f6bb38".to_string()]
        );
    }

    #[test]
    fn link_lookup_keys_try_vault_root_before_ancestor_directories() {
        assert_eq!(
            link_lookup_keys(
                Path::new("code/modules/crates.md"),
                LinkKind::Wikilink,
                "sub/page",
            ),
            vec![
                "sub/page".to_string(),
                "code/modules/sub/page".to_string(),
                "code/sub/page".to_string(),
            ]
        );
        // Markdown links only consider the immediate parent directory.
        assert_eq!(
            link_lookup_keys(
                Path::new("code/modules/crates.md"),
                LinkKind::Markdown,
                "sub/page",
            ),
            vec!["sub/page".to_string(), "code/modules/sub/page".to_string()]
        );
    }

    #[test]
    fn ignored_target_skips_external_network_references() {
        assert!(ignored_target("//cdn.example.test/asset.png"));
        assert!(ignored_target(r"\\server\share\page.md"));
        assert!(ignored_target("https://example.test/page"));
        assert!(!ignored_target("knowledge/topics/page.md"));
    }

    #[test]
    fn missing_frontmatter_reports_pages_without_a_block() {
        let pages = vec![
            owned_page(
                "knowledge/topics/with.md",
                "---\ntitle: With\n---\n# With\nSee [[Bare]].\n",
                &[],
            ),
            owned_page("knowledge/topics/bare.md", "# Bare\nLinks [[With]].\n", &[]),
        ];

        let outcome = run_checks(&lint_pages(&pages), None);

        assert_eq!(
            outcome.missing_frontmatter,
            vec![PathBuf::from("knowledge/topics/bare.md")]
        );
    }

    #[test]
    fn duplicate_aliases_are_ambiguous() {
        let mut left = owned_page(
            "knowledge/concepts/a.md",
            "---\ntitle: A\n---\n# A\nSee [[B]].\n",
            &[],
        );
        left.aliases = vec!["shared".to_string()];
        let mut right = owned_page(
            "knowledge/concepts/b.md",
            "---\ntitle: B\n---\n# B\nSee [[A]].\n",
            &[],
        );
        right.aliases = vec!["Shared".to_string()];
        let pages = vec![left, right];

        let outcome = run_checks(&lint_pages(&pages), None);

        assert_eq!(outcome.duplicate_aliases.len(), 1);
        assert_eq!(outcome.duplicate_aliases[0].alias, "shared");
        assert_eq!(
            outcome.duplicate_aliases[0].paths,
            vec![
                PathBuf::from("knowledge/concepts/a.md"),
                PathBuf::from("knowledge/concepts/b.md"),
            ]
        );
    }

    #[test]
    fn same_page_alias_variants_are_not_ambiguous() {
        // Entity pages carry observed case variants as aliases, one of which
        // can equal the title. Every variant folds to the same page, so the
        // page is never ambiguous with itself (#17642).
        let mut page = owned_page(
            "knowledge/concepts/gcode.md",
            "---\ntitle: gcode\n---\n# gcode\nSee [[Other]].\n",
            &[],
        );
        page.aliases = vec!["gcode".to_string(), "Gcode".to_string()];
        let other = owned_page(
            "knowledge/concepts/other.md",
            "---\ntitle: Other\n---\n# Other\nSee [[gcode]].\n",
            &[],
        );
        let pages = vec![page, other];

        let outcome = run_checks(&lint_pages(&pages), None);

        assert_eq!(outcome.duplicate_aliases, Vec::new());
    }

    #[test]
    fn navigation_root_links_are_exempt_from_missing_backlinks() {
        let pages = vec![
            owned_page(
                "code/repo.md",
                "---\ntitle: Repository Overview\n---\n# Repository Overview\nStart with [[Introduction]].\n",
                &[],
            ),
            owned_page(
                "code/narrative/introduction.md",
                "---\ntitle: Introduction\n---\n# Introduction\nNo link back.\n",
                &[],
            ),
        ];

        let outcome = run_checks(&lint_pages(&pages), None);

        assert!(
            outcome.missing_backlinks.is_empty(),
            "{:?}",
            outcome.missing_backlinks
        );
    }

    #[test]
    fn content_page_links_still_require_reciprocity() {
        let pages = vec![
            owned_page(
                "code/narrative/introduction.md",
                "---\ntitle: Introduction\n---\n# Introduction\nContinue to [[Architecture]].\n",
                &[],
            ),
            owned_page(
                "code/narrative/architecture.md",
                "---\ntitle: Architecture\n---\n# Architecture\nNo link back.\n",
                &[],
            ),
        ];

        let outcome = run_checks(&lint_pages(&pages), None);

        assert_eq!(
            outcome.missing_backlinks.len(),
            1,
            "{:?}",
            outcome.missing_backlinks
        );
        let issue = &outcome.missing_backlinks[0];
        assert_eq!(issue.path, PathBuf::from("code/narrative/architecture.md"));
        assert_eq!(issue.kind, "missing_backlink");
        assert_eq!(issue.target, "Introduction");
    }

    #[test]
    fn invalid_and_ungrounded_diagrams_are_flagged() {
        let pages = vec![
            owned_page(
                "code/concepts/pipeline.md",
                "---\ntitle: Pipeline\n---\n# Pipeline\nThe parser feeds the chunker.\n\n```mermaid\nflowchart LR\n    s0[\"parser — builds AST\"]\n    s1[\"chunker — splits content\"]\n    s0 --> s1\n```\n",
                &[],
            ),
            owned_page(
                "code/concepts/flow.md",
                "---\ntitle: Flow\n---\n# Flow\nThe parser feeds downstream stages.\n\n```mermaid\nflowchart LR\n    s0[\"parser — builds AST\"]\n    s1[\"telemetry — emits metrics\"]\n    s0 --> s1\n```\n",
                &[],
            ),
            owned_page(
                "code/concepts/broken.md",
                "---\ntitle: Broken\n---\n# Broken\nThe parser stage.\n\n```mermaid\nflowchart LR\n    s0[\"parser\n    s0 --> s1\n```\n",
                &[],
            ),
        ];

        let outcome = run_checks(&lint_pages(&pages), None);

        assert_eq!(outcome.invalid_diagrams.len(), 2);
        assert_eq!(outcome.invalid_diagrams[1].reason, "invalid-mermaid");
        assert_eq!(
            outcome.invalid_diagrams[1].path,
            PathBuf::from("code/concepts/broken.md")
        );
        let ungrounded = &outcome.invalid_diagrams[0];
        assert_eq!(ungrounded.path, PathBuf::from("code/concepts/flow.md"));
        assert!(
            ungrounded.reason.contains("ungrounded"),
            "{}",
            ungrounded.reason
        );
        assert!(
            ungrounded.reason.contains("telemetry"),
            "{}",
            ungrounded.reason
        );
    }

    #[test]
    fn markdown_hygiene_flags_pages_the_normalizer_would_rewrite() {
        let pages = vec![
            owned_page(
                "knowledge/topics/clean.md",
                "---\ntitle: Clean\n---\n\n# Clean\n\nBody links [[Dirty]].\n",
                &[],
            ),
            owned_page(
                "knowledge/topics/dirty.md",
                "---\ntitle: Dirty\n---\n# Dirty\nNo blank line after the heading, links [[Clean]].\n",
                &[],
            ),
        ];

        let outcome = run_checks(&lint_pages(&pages), None);

        assert_eq!(
            outcome.dirty_markdown,
            vec![PathBuf::from("knowledge/topics/dirty.md")]
        );
    }

    #[test]
    fn has_frontmatter_block_requires_a_closed_block() {
        assert!(has_frontmatter_block("---\ntitle: A\n---\nBody\n"));
        assert!(!has_frontmatter_block("# No frontmatter\n"));
        assert!(!has_frontmatter_block("---\ntitle: unclosed\nBody\n"));
    }

    struct RejectEverything;

    impl CitationValidator for RejectEverything {
        fn validate_page(&self, page: &LintPage<'_>) -> Vec<CitationIssue> {
            vec![CitationIssue {
                path: page.relative_path.to_path_buf(),
                line: 1,
                citation: "[src/lib.rs:10-20]".to_string(),
                reason: "symbol no longer at cited span".to_string(),
            }]
        }
    }

    #[test]
    fn citation_validator_findings_flow_into_the_outcome() {
        let pages = vec![owned_page(
            "knowledge/topics/cited.md",
            "---\ntitle: Cited\n---\n# Cited\nBody.\n",
            &[],
        )];
        let lint_pages = lint_pages(&pages);

        let without = run_checks(&lint_pages, None);
        assert!(without.citation_issues.is_empty());

        let with = run_checks(&lint_pages, Some(&RejectEverything));
        assert_eq!(with.citation_issues.len(), 1);
        assert_eq!(
            with.citation_issues[0].path,
            PathBuf::from("knowledge/topics/cited.md")
        );
        assert_eq!(
            with.citation_issues[0].reason,
            "symbol no longer at cited span"
        );
    }

    #[test]
    fn index_consistency_reports_missing_and_unresolved_entries() {
        let pages = vec![owned_page(
            "knowledge/concepts/gcode.md",
            "---\ntitle: gcode\n---\n# gcode\nBody.\n",
            &[],
        )];
        let lint_pages = lint_pages(&pages);

        let missing = index_consistency(None, &lint_pages);
        assert_eq!(missing.len(), 1);
        assert_eq!(missing[0].kind, "missing_index");

        let index_markdown = "# Wiki Index\n\n- [[knowledge/concepts/gcode|gcode]]\n- [[knowledge/concepts/gone|gone]]\n";
        let issues = index_consistency(Some(index_markdown), &lint_pages);
        assert_eq!(issues.len(), 1, "{issues:?}");
        assert_eq!(issues[0].kind, "index_link");
        assert_eq!(issues[0].target, "knowledge/concepts/gone");
        assert_eq!(issues[0].path, PathBuf::from(INDEX_FILE));

        let clean = index_consistency(
            Some("# Wiki Index\n\n- [[knowledge/concepts/gcode|gcode]]\n"),
            &lint_pages,
        );
        assert!(clean.is_empty(), "{clean:?}");
    }
}
