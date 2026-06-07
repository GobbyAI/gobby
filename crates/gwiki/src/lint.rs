use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::frontmatter::{WikiFrontmatter, parse_frontmatter};
use crate::links::{LinkKind, WikiLink, normalize_wiki_path};
use crate::markdown::{MarkdownDomainRecord, parse_markdown};
use crate::{ScopeIdentity, WikiError};

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
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct LinkIssue {
    pub path: PathBuf,
    pub line: usize,
    pub target: String,
    pub kind: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DuplicateAlias {
    pub alias: String,
    pub paths: Vec<PathBuf>,
}

pub fn run(vault_root: &Path, scope: ScopeIdentity) -> Result<LintReport, WikiError> {
    let pages = collect_pages(vault_root)?;
    let target_map = target_map(&pages);
    let mut broken_links = Vec::new();
    let mut inbound: BTreeMap<PathBuf, usize> = pages
        .iter()
        .map(|page| (page.relative_path.clone(), 0))
        .collect();
    let mut outgoing: BTreeMap<PathBuf, BTreeSet<PathBuf>> = pages
        .iter()
        .map(|page| (page.relative_path.clone(), BTreeSet::new()))
        .collect();

    for page in &pages {
        for link in &page.parsed.links {
            if ignored_target(&link.target) {
                continue;
            }
            if let Some(target_path) = link_lookup_targets(page, link)
                .iter()
                .find_map(|lookup_target| target_map.get(lookup_target))
            {
                if target_path != &page.relative_path {
                    *inbound.entry(target_path.clone()).or_default() += 1;
                    outgoing
                        .entry(page.relative_path.clone())
                        .or_default()
                        .insert(target_path.clone());
                }
            } else {
                broken_links.push(LinkIssue {
                    path: page.relative_path.clone(),
                    line: line_number(&page.markdown, link.byte_start),
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
        .filter_map(|page| (!page.has_frontmatter).then_some(page.relative_path.clone()))
        .collect();
    missing_frontmatter.sort();

    let duplicate_aliases = duplicate_aliases(&pages);
    let missing_backlinks = missing_backlinks(&pages, &outgoing);

    Ok(LintReport {
        command: "lint",
        scope,
        root: vault_root.to_path_buf(),
        broken_links,
        orphan_pages,
        missing_frontmatter,
        duplicate_aliases,
        missing_backlinks,
    })
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

pub(crate) fn collect_pages(vault_root: &Path) -> Result<Vec<WikiPage>, WikiError> {
    let wiki_root = vault_root.join("wiki");
    if !wiki_root.exists() {
        return Ok(Vec::new());
    }

    let mut raw_pages = Vec::new();
    collect_markdown_files(vault_root, &wiki_root, &mut raw_pages)?;
    raw_pages.sort_by(|left, right| left.relative_path.cmp(&right.relative_path));

    let known_targets = known_targets(&raw_pages);
    raw_pages
        .into_iter()
        .map(|raw| {
            let parsed = parse_markdown(
                raw.relative_path.clone(),
                &raw.markdown,
                known_targets.iter(),
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

pub(crate) fn line_number(markdown: &str, byte_start: usize) -> usize {
    markdown[..byte_start.min(markdown.len())]
        .bytes()
        .filter(|byte| *byte == b'\n')
        .count()
        + 1
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
        insert_page_targets(&mut targets, &page.relative_path, &page.frontmatter);
    }
    targets
}

fn target_map(pages: &[WikiPage]) -> BTreeMap<String, PathBuf> {
    let mut targets = BTreeMap::new();
    for page in pages {
        for target in page_targets(&page.relative_path, &page.parsed.frontmatter) {
            targets
                .entry(target)
                .or_insert_with(|| page.relative_path.clone());
        }
    }
    targets
}

fn insert_page_targets(
    targets: &mut BTreeSet<String>,
    relative_path: &Path,
    frontmatter: &WikiFrontmatter,
) {
    targets.extend(page_targets(relative_path, frontmatter));
}

fn page_targets(relative_path: &Path, frontmatter: &WikiFrontmatter) -> Vec<String> {
    let mut targets = Vec::new();
    let relative = relative_path.to_string_lossy().replace('\\', "/");
    targets.push(normalize_wiki_path(&relative));
    if let Some(file_stem) = relative_path.file_stem().and_then(|stem| stem.to_str()) {
        targets.push(normalize_wiki_path(file_stem));
    }
    if let Some(title) = &frontmatter.title {
        targets.push(normalize_wiki_path(title));
    }
    for alias in &frontmatter.aliases {
        targets.push(normalize_wiki_path(alias));
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

fn link_lookup_targets(page: &WikiPage, link: &WikiLink) -> Vec<String> {
    let mut targets = vec![link.normalized_target.clone()];
    if (link.kind != LinkKind::Markdown && link.kind != LinkKind::Wikilink)
        || link.normalized_target.starts_with("wiki/")
        || link.normalized_target.starts_with("raw/")
        || link.normalized_target.starts_with("meta/")
        || Path::new(&link.normalized_target).is_absolute()
    {
        return targets;
    }

    let parents: Box<dyn Iterator<Item = &Path> + '_> = if link.kind == LinkKind::Markdown {
        Box::new(page.relative_path.parent().into_iter())
    } else {
        Box::new(page.relative_path.ancestors().skip(1))
    };

    for parent in parents {
        let parent = parent.to_string_lossy().replace('\\', "/");
        if parent.is_empty() {
            continue;
        }
        let candidate = normalize_path_components(&parent, &link.normalized_target);
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

fn duplicate_aliases(pages: &[WikiPage]) -> Vec<DuplicateAlias> {
    let mut aliases: BTreeMap<String, (String, Vec<PathBuf>)> = BTreeMap::new();
    for page in pages {
        for alias in &page.parsed.frontmatter.aliases {
            let display_alias = alias.trim().to_string();
            aliases
                .entry(display_alias.to_ascii_lowercase())
                .or_insert_with(|| (display_alias, Vec::new()))
                .1
                .push(page.relative_path.clone());
        }
    }
    aliases
        .into_iter()
        .filter_map(|(_, (alias, mut paths))| {
            paths.sort();
            (paths.len() > 1).then_some(DuplicateAlias { alias, paths })
        })
        .collect()
}

fn missing_backlinks(
    pages: &[WikiPage],
    outgoing: &BTreeMap<PathBuf, BTreeSet<PathBuf>>,
) -> Vec<LinkIssue> {
    let titles: BTreeMap<PathBuf, String> = pages
        .iter()
        .map(|page| (page.relative_path.clone(), title_for_page(page)))
        .collect();
    let mut issues = Vec::new();
    for (source, targets) in outgoing {
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
                    .cloned()
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
            "wiki/topics/home.md",
            "---\ntitle: Home\n---\n# Home\nSee [[Linked]], [linked](linked.md), [[Missing]], and [gone](missing.md).\n",
        );
        write_page(
            root,
            "wiki/topics/linked.md",
            "---\ntitle: Linked\n---\n# Linked\nBack to [[Home]].\n",
        );
        write_page(
            root,
            "wiki/topics/orphan.md",
            "---\ntitle: Orphan\n---\n# Orphan\nNo inbound links.\n",
        );

        let report = run(root, ScopeIdentity::topic("ops")).expect("lint runs");

        assert_eq!(report.broken_links.len(), 2);
        assert_eq!(
            report.broken_links[0].path,
            PathBuf::from("wiki/topics/home.md")
        );
        assert_eq!(report.broken_links[0].target, "Missing");
        assert_eq!(report.broken_links[1].target, "missing.md");
        assert_eq!(
            report.orphan_pages,
            vec![PathBuf::from("wiki/topics/orphan.md")]
        );
    }

    #[test]
    fn nested_wikilinks_resolve_relative_to_current_subtree() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "wiki/code/repo.md",
            "---\ntitle: Repository Overview\n---\n# Repository Overview\n[[modules/crates|crates]]\n",
        );
        write_page(
            root,
            "wiki/code/modules/crates.md",
            "---\ntitle: crates\n---\n# crates\n[[../repo|Repository Overview]]\n",
        );

        let report = run(root, ScopeIdentity::topic("lint")).expect("lint runs");

        assert!(report.broken_links.is_empty(), "{:?}", report.broken_links);
    }

    #[test]
    fn relative_markdown_links_clamp_traversal_at_vault_root() {
        assert_eq!(
            normalize_path_components("wiki/topics", "../../../outside.md"),
            "outside.md"
        );
    }

    #[test]
    fn ignored_target_skips_external_network_references() {
        assert!(ignored_target("//cdn.example.test/asset.png"));
        assert!(ignored_target(r"\\server\share\page.md"));
        assert!(ignored_target("https://example.test/page"));
        assert!(!ignored_target("wiki/topics/page.md"));
    }

    #[test]
    fn lint_is_read_only() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let relative = "wiki/topics/home.md";
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

    fn write_page(root: &Path, relative: &str, markdown: &str) {
        let path = root.join(relative);
        std::fs::create_dir_all(path.parent().expect("page parent")).expect("create parent");
        std::fs::write(path, markdown).expect("write page");
    }
}
