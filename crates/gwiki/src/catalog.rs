//! Deterministic catalog regeneration for the vault indexes.
//!
//! [`regenerate`] rebuilds `_index.md`, `knowledge/INDEX.md`,
//! `code/INDEX.md`, and per-folder `_context.md` files from on-disk vault
//! state with no LLM involvement: rerunning it over an unchanged vault
//! produces byte-identical files. It also restores the static `ai-readme.md`
//! when missing (#17730).

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, OpenOptions};
use std::path::{Path, PathBuf};

use crate::frontmatter::parse_frontmatter;
use crate::links::{canonical_target_key, extract_links};
use crate::sources::{SourceManifest, SourceRecord};
use crate::synthesis::{relative_path, wiki_link};
use crate::{ScopeIdentity, WikiError};

/// Ceiling for the `## Overview` hot-cache block in `_index.md`. The block is
/// injected into session-start context, so it must stay small.
pub(crate) const OVERVIEW_MAX_CHARS: usize = 2_000;
/// Ceiling for a listing one-liner (title plus first sentence).
pub(crate) const ONE_LINER_MAX_CHARS: usize = 160;
const TOP_CONCEPTS_LIMIT: usize = 5;
/// Recap pages folded into the Overview's rolling Recent work line.
const RECENT_RECAPS_LIMIT: usize = 3;

/// Sections of `code/INDEX.md`, in render order, mapped to the codewiki
/// output directories they list.
const CODE_SECTIONS: &[(&str, &str)] = &[
    ("Handbook", "code/narrative"),
    ("Concepts", "code/concepts"),
    ("Modules", "code/modules"),
    ("Files", "code/files"),
];

/// Per-folder agent navigation file (#17730).
const CONTEXT_FILE: &str = "_context.md";
/// Content roots that receive `_context.md` folder files.
const CONTEXT_ROOTS: &[&str] = &[
    crate::vault::KNOWLEDGE_ROOT,
    crate::vault::CODE_ROOT,
    crate::recap::RECAPS_DIRECTORY,
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct CatalogReport {
    pub(crate) index_path: PathBuf,
    pub(crate) knowledge_index_path: PathBuf,
    pub(crate) code_index_path: PathBuf,
    pub(crate) context_paths: Vec<PathBuf>,
}

#[derive(Debug, Clone)]
struct PageSummary {
    /// Vault-relative path including the `.md` extension.
    relative: String,
    title: String,
    /// First body sentence, already bounded together with the title.
    one_liner: Option<String>,
}

/// Rebuild the three catalog files under the shared `_gwiki/index.lock`.
pub(crate) fn regenerate(
    vault_root: &Path,
    scope: &ScopeIdentity,
) -> Result<CatalogReport, WikiError> {
    let lock_path = vault_root.join(crate::vault::STATE_ROOT).join("index.lock");
    if let Some(parent) = lock_path.parent() {
        fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create wiki catalog lock directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(&lock_path)
        .map_err(|error| WikiError::Io {
            action: "open wiki catalog lock",
            path: Some(lock_path.clone()),
            source: error,
        })?;
    crate::compile::lock_file(&lock, &lock_path, "lock wiki catalog")?;

    let concepts = scan_pages(vault_root, "knowledge/concepts")?;
    let topics = scan_pages(vault_root, "knowledge/topics")?;
    let sources = source_records_by_id(vault_root)?;
    let code_sections = scan_code_sections(vault_root)?;
    let top_concepts = top_concepts(vault_root, &concepts)?;
    let recaps = scan_pages(vault_root, crate::recap::RECAPS_DIRECTORY)?;

    let context_paths = write_folder_contexts(vault_root)?;
    ensure_ai_readme(vault_root)?;

    let report = CatalogReport {
        index_path: vault_root.join("_index.md"),
        knowledge_index_path: vault_root.join("knowledge/INDEX.md"),
        code_index_path: vault_root.join("code/INDEX.md"),
        context_paths,
    };
    let code_page_total: usize = code_sections.iter().map(|(_, pages)| pages.len()).sum();
    write_if_changed(
        &report.index_path,
        &render_wiki_index(
            vault_root,
            scope,
            &WikiIndexSections {
                concepts: &concepts,
                topics: &topics,
                source_total: sources.len(),
                code_page_total,
                top_concepts: &top_concepts,
                recaps: &recaps,
            },
        ),
    )?;
    write_if_changed(
        &report.knowledge_index_path,
        &render_knowledge_index(vault_root, &concepts, &topics, &sources),
    )?;
    write_if_changed(
        &report.code_index_path,
        &render_code_index(vault_root, &code_sections),
    )?;
    Ok(report)
}

/// Scanned vault sections feeding `_index.md`.
struct WikiIndexSections<'a> {
    concepts: &'a [PageSummary],
    topics: &'a [PageSummary],
    source_total: usize,
    code_page_total: usize,
    top_concepts: &'a [PageSummary],
    recaps: &'a [PageSummary],
}

fn render_wiki_index(
    vault_root: &Path,
    scope: &ScopeIdentity,
    sections: &WikiIndexSections<'_>,
) -> String {
    let mut top_links: Vec<String> = sections
        .top_concepts
        .iter()
        .map(|page| page_link(vault_root, page))
        .collect();
    let recent_work = render_recent_work(sections.recaps);
    let mut overview = render_overview(scope, sections, &top_links, recent_work.as_deref());
    // The Overview block doubles as the session-start hot cache; shed trailing
    // top-concept links until it fits the injection budget. The Recent work
    // line never needs shedding: three date-labelled links plus one bounded
    // one-liner stay far under the budget.
    while overview.chars().count() > OVERVIEW_MAX_CHARS && !top_links.is_empty() {
        top_links.pop();
        overview = render_overview(scope, sections, &top_links, recent_work.as_deref());
    }

    let mut markdown = String::from("# Wiki Index\n\n");
    markdown.push_str(&overview);
    render_listing_section(&mut markdown, vault_root, "Concepts", sections.concepts);
    render_listing_section(&mut markdown, vault_root, "Topics", sections.topics);
    markdown
}

fn render_overview(
    scope: &ScopeIdentity,
    sections: &WikiIndexSections<'_>,
    top_links: &[String],
    recent_work: Option<&str>,
) -> String {
    let mut overview = String::from("## Overview\n\n");
    overview.push_str(&format!("Scope: {scope}\n"));
    overview.push_str(&format!(
        "Totals: {} concepts · {} topics · {} sources · {} code pages\n",
        sections.concepts.len(),
        sections.topics.len(),
        sections.source_total,
        sections.code_page_total,
    ));
    if let Some(recent_work) = recent_work {
        overview.push_str(recent_work);
        overview.push('\n');
    }
    if !top_links.is_empty() {
        overview.push_str(&format!("Top concepts: {}\n", top_links.join(", ")));
    }
    overview.push_str(
        "Query: search with `gwiki search \"<term>\"`; full listings live in \
         [[knowledge/INDEX|knowledge/INDEX]] and [[code/INDEX|code/INDEX]].\n",
    );
    overview
}

/// Rolling Recent work line: the newest recap with its bounded one-liner,
/// then bare date links for the next most recent recaps.
fn render_recent_work(recaps: &[PageSummary]) -> Option<String> {
    let mut newest_first = recaps.iter().rev().take(RECENT_RECAPS_LIMIT);
    let latest = newest_first.next()?;
    let mut line = format!("Recent work: {}", recap_link(latest));
    if let Some(one_liner) = &latest.one_liner {
        line.push_str(" — ");
        line.push_str(one_liner);
    }
    for earlier in newest_first {
        line.push_str(", ");
        line.push_str(&recap_link(earlier));
    }
    Some(line)
}

/// Link a recap page by its date stem — the page title ("Recap: <date>")
/// would only repeat the label.
fn recap_link(page: &PageSummary) -> String {
    let stem = page_stem(&page.relative);
    format!("[[{}|{stem}]]", page.relative.trim_end_matches(".md"),)
}

fn render_listing_section(
    markdown: &mut String,
    vault_root: &Path,
    heading: &str,
    pages: &[PageSummary],
) {
    markdown.push_str(&format!("\n## {heading}\n\n"));
    if pages.is_empty() {
        markdown.push_str("(none yet)\n");
        return;
    }
    for page in pages {
        markdown.push_str("- ");
        markdown.push_str(&page_link(vault_root, page));
        if let Some(one_liner) = &page.one_liner {
            markdown.push_str(" — ");
            markdown.push_str(one_liner);
        }
        markdown.push('\n');
    }
}

/// Frontmatter stamp for catalog-owned pages: gives the generated surface a
/// valid frontmatter block (it is a first-class lint page, #17806) and marks
/// it `generated_by` so backlink reciprocity classifies it as machine-written.
fn catalog_frontmatter(title: &str) -> String {
    format!("---\ntitle: \"{title}\"\ngenerated_by: gwiki-catalog\n---\n\n")
}

fn render_knowledge_index(
    vault_root: &Path,
    concepts: &[PageSummary],
    topics: &[PageSummary],
    sources: &[SourceRecord],
) -> String {
    let mut markdown = catalog_frontmatter("Knowledge Index");
    markdown.push_str("# Knowledge\n");
    render_listing_section(&mut markdown, vault_root, "Concepts", concepts);
    render_listing_section(&mut markdown, vault_root, "Topics", topics);
    markdown.push_str("\n## Sources\n\n");
    if sources.is_empty() {
        markdown.push_str("(none yet)\n");
        return markdown;
    }
    for record in sources {
        let title = record.title.as_deref().unwrap_or(&record.location);
        // Link the digest page only when it exists on disk: manifest entries
        // whose digests were purged or compiled under another page name would
        // otherwise regenerate as guaranteed-broken wikilinks on every rebuild.
        let digest = crate::paths::derived_markdown_path(record)
            .ok()
            .filter(|relative| vault_root.join(relative).is_file());
        let label = match digest {
            Some(relative) => wiki_link(vault_root, &vault_root.join(relative), title),
            None => title.to_string(),
        };
        markdown.push_str(&format!(
            "- {label} — {} {}\n",
            record.kind, record.location
        ));
    }
    markdown
}

fn render_code_index(vault_root: &Path, sections: &[(&'static str, Vec<PageSummary>)]) -> String {
    let mut markdown = catalog_frontmatter("Code Index");
    markdown.push_str("# Code\n");
    for (heading, pages) in sections {
        render_listing_section(&mut markdown, vault_root, heading, pages);
    }
    markdown
}

fn page_link(vault_root: &Path, page: &PageSummary) -> String {
    wiki_link(vault_root, &vault_root.join(&page.relative), &page.title)
}

/// Concepts ranked by inbound wikilink mentions across `knowledge/` pages
/// (self-links excluded), falling back to alphabetical order in a vault with
/// no cross-links yet. Ties break alphabetically by path.
fn top_concepts(
    vault_root: &Path,
    concepts: &[PageSummary],
) -> Result<Vec<PageSummary>, WikiError> {
    if concepts.is_empty() {
        return Ok(Vec::new());
    }
    let mut key_to_concept: BTreeMap<String, usize> = BTreeMap::new();
    let mut known_targets: Vec<String> = Vec::with_capacity(concepts.len() * 2);
    for (index, concept) in concepts.iter().enumerate() {
        let trimmed = concept.relative.trim_end_matches(".md");
        let stem = trimmed.rsplit('/').next().unwrap_or(trimmed);
        for target in [trimmed, stem] {
            known_targets.push(target.to_string());
            key_to_concept.insert(canonical_target_key(target), index);
        }
    }

    let mut counts = vec![0_usize; concepts.len()];
    for page in walk_markdown_pages(vault_root, "knowledge")? {
        let Some(text) = read_page(&page)? else {
            continue;
        };
        let relative = relative_path(vault_root, &page);
        for link in extract_links(&text, &known_targets) {
            if let Some(&index) = key_to_concept.get(&canonical_target_key(&link.normalized_target))
                && concepts[index].relative != relative
            {
                counts[index] += 1;
            }
        }
    }

    let mut ranked: Vec<usize> = (0..concepts.len()).collect();
    ranked.sort_by(|&left, &right| {
        counts[right]
            .cmp(&counts[left])
            .then_with(|| concepts[left].relative.cmp(&concepts[right].relative))
    });
    Ok(ranked
        .into_iter()
        .take(TOP_CONCEPTS_LIMIT)
        .map(|index| concepts[index].clone())
        .collect())
}

/// Markdown pages directly under `directory`, sorted by file name.
fn scan_pages(vault_root: &Path, directory: &str) -> Result<Vec<PageSummary>, WikiError> {
    let mut pages = Vec::new();
    for path in walk_markdown_pages(vault_root, directory)? {
        let Some(text) = read_page(&path)? else {
            continue;
        };
        if let Some(summary) = summarize_page(vault_root, &path, &text, true) {
            pages.push(summary);
        }
    }
    pages.sort_by(|left, right| left.relative.cmp(&right.relative));
    Ok(pages)
}

/// Rebuild every per-folder `_context.md` under the content roots: a
/// deterministic listing of the folder's pages and context-bearing
/// subfolders for agent traversal (#17730). Unlike the INDEX surfaces,
/// `_context.md` is an agent surface, so quarantined candidates and archived
/// pages are both excluded. Stale context files whose folder no longer holds
/// pages are removed.
fn write_folder_contexts(vault_root: &Path) -> Result<Vec<PathBuf>, WikiError> {
    let mut pages_by_dir: BTreeMap<String, Vec<PageSummary>> = BTreeMap::new();
    for root in CONTEXT_ROOTS {
        for path in walk_markdown_pages(vault_root, root)? {
            let Some(text) = read_page(&path)? else {
                continue;
            };
            let Some(summary) = summarize_page(vault_root, &path, &text, false) else {
                continue;
            };
            let Some((directory, _)) = summary.relative.rsplit_once('/') else {
                continue;
            };
            pages_by_dir
                .entry(directory.to_string())
                .or_default()
                .push(summary);
        }
    }

    // Intermediate folders without direct pages still get a context file so
    // agents can descend level by level; collect each folder's
    // context-bearing children while propagating upward to the walk roots.
    let mut context_dirs: BTreeSet<String> = pages_by_dir.keys().cloned().collect();
    let mut subfolders: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    let mut pending: Vec<String> = context_dirs.iter().cloned().collect();
    while let Some(directory) = pending.pop() {
        let Some((parent, _)) = directory.rsplit_once('/') else {
            continue;
        };
        subfolders
            .entry(parent.to_string())
            .or_default()
            .insert(directory.clone());
        if context_dirs.insert(parent.to_string()) {
            pending.push(parent.to_string());
        }
    }

    let mut written = Vec::with_capacity(context_dirs.len());
    for directory in &context_dirs {
        let mut pages = pages_by_dir.remove(directory).unwrap_or_default();
        pages.sort_by(|left, right| left.relative.cmp(&right.relative));
        let children = subfolders.remove(directory).unwrap_or_default();
        let path = vault_root.join(directory).join(CONTEXT_FILE);
        write_if_changed(
            &path,
            &render_folder_context(vault_root, directory, &pages, &children),
        )?;
        written.push(path);
    }
    prune_stale_contexts(vault_root, &written)?;
    Ok(written)
}

fn render_folder_context(
    vault_root: &Path,
    directory: &str,
    pages: &[PageSummary],
    subfolders: &BTreeSet<String>,
) -> String {
    let mut markdown = catalog_frontmatter(&format!("{directory} — folder context"));
    markdown.push_str(&format!(
        "# {directory} — folder context\n\nDeterministic folder listing for AI agents; regenerated by gwiki catalog runs. Do not edit by hand.\n"
    ));
    if !pages.is_empty() {
        markdown.push_str(&format!("\n## Pages ({})\n\n", pages.len()));
        for page in pages {
            match &page.one_liner {
                Some(one_liner) => markdown.push_str(&format!(
                    "- {} — {one_liner}\n",
                    page_link(vault_root, page)
                )),
                None => markdown.push_str(&format!("- {}\n", page_link(vault_root, page))),
            }
        }
    }
    if !subfolders.is_empty() {
        markdown.push_str("\n## Subfolders\n\n");
        for subfolder in subfolders {
            let name = subfolder.rsplit('/').next().unwrap_or(subfolder);
            markdown.push_str(&format!("- [[{subfolder}/_context|{name}/]]\n"));
        }
    }
    markdown
}

/// Remove `_context.md` files whose folder no longer carries pages.
fn prune_stale_contexts(vault_root: &Path, written: &[PathBuf]) -> Result<(), WikiError> {
    let written: BTreeSet<&Path> = written.iter().map(PathBuf::as_path).collect();
    for root in CONTEXT_ROOTS {
        let mut pending = vec![vault_root.join(root)];
        while let Some(directory) = pending.pop() {
            let entries = match fs::read_dir(&directory) {
                Ok(entries) => entries,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => {
                    return Err(WikiError::Io {
                        action: "list wiki context directory",
                        path: Some(directory),
                        source: error,
                    });
                }
            };
            for entry in entries {
                let entry = entry.map_err(|error| WikiError::Io {
                    action: "list wiki context directory",
                    path: Some(directory.clone()),
                    source: error,
                })?;
                let path = entry.path();
                if path.is_dir() {
                    pending.push(path);
                } else if entry.file_name().to_string_lossy() == CONTEXT_FILE
                    && !written.contains(path.as_path())
                {
                    fs::remove_file(&path).map_err(|error| WikiError::Io {
                        action: "remove stale wiki context file",
                        path: Some(path.clone()),
                        source: error,
                    })?;
                }
            }
        }
    }
    Ok(())
}

/// Restore the static agent readme only when missing: the init-written copy
/// and any user edits are never overwritten.
fn ensure_ai_readme(vault_root: &Path) -> Result<(), WikiError> {
    crate::vault::ensure_file(
        vault_root.join(crate::vault::AI_README_FILE).as_path(),
        crate::vault::AI_README_TEMPLATE,
    )
    .map(|_| ())
}

fn scan_code_sections(
    vault_root: &Path,
) -> Result<Vec<(&'static str, Vec<PageSummary>)>, WikiError> {
    let mut sections = Vec::with_capacity(CODE_SECTIONS.len());
    for (heading, directory) in CODE_SECTIONS {
        sections.push((*heading, scan_pages(vault_root, directory)?));
    }
    Ok(sections)
}

/// All `.md` files under `directory` (recursive), excluding catalog files.
fn walk_markdown_pages(vault_root: &Path, directory: &str) -> Result<Vec<PathBuf>, WikiError> {
    let mut pages = Vec::new();
    let mut pending = vec![vault_root.join(directory)];
    while let Some(dir) = pending.pop() {
        let entries = match fs::read_dir(&dir) {
            Ok(entries) => entries,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => {
                return Err(WikiError::Io {
                    action: "list wiki catalog directory",
                    path: Some(dir),
                    source: error,
                });
            }
        };
        for entry in entries {
            let entry = entry.map_err(|error| WikiError::Io {
                action: "list wiki catalog directory",
                path: Some(dir.clone()),
                source: error,
            })?;
            let path = entry.path();
            let name = entry.file_name();
            let name = name.to_string_lossy();
            if name.starts_with('.') {
                continue;
            }
            if path.is_dir() {
                pending.push(path);
            } else if name.ends_with(".md")
                && name != "INDEX.md"
                && name != "_index.md"
                && name != CONTEXT_FILE
            {
                pages.push(path);
            }
        }
    }
    pages.sort();
    Ok(pages)
}

/// Page text, or `None` when the file vanished between listing and reading.
fn read_page(path: &Path) -> Result<Option<String>, WikiError> {
    match fs::read_to_string(path) {
        Ok(text) => Ok(Some(text)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(WikiError::Io {
            action: "read wiki page for catalog",
            path: Some(path.to_path_buf()),
            source: error,
        }),
    }
}

/// `None` when the page is excluded from the requesting surface. Catalog
/// indexes are the maintainer/review surface (`include_candidates`):
/// quarantined candidates stay listed so the librarian can find them
/// (#17727); only archived pages drop out. Agent surfaces such as
/// `_context.md` pass `false` to exclude candidates as well (#17730).
fn summarize_page(
    vault_root: &Path,
    path: &Path,
    text: &str,
    include_candidates: bool,
) -> Option<PageSummary> {
    let relative = relative_path(vault_root, path);
    let (title, body) = match parse_frontmatter(text) {
        Ok(parsed) => {
            if crate::lifecycle::excluded_from_surfaces(&parsed.metadata, include_candidates) {
                return None;
            }
            (parsed.metadata.title.clone(), parsed.body.to_string())
        }
        Err(_) => (None, text.to_string()),
    };
    let title = title
        .or_else(|| first_heading(&body))
        .unwrap_or_else(|| page_stem(&relative).to_string());
    let one_liner = first_sentence(&body).map(|sentence| {
        let budget = ONE_LINER_MAX_CHARS.saturating_sub(title.chars().count() + 3);
        truncate_chars(&sentence, budget)
    });
    Some(PageSummary {
        relative,
        title,
        one_liner: one_liner.filter(|value| !value.is_empty()),
    })
}

fn page_stem(relative: &str) -> &str {
    let trimmed = relative.trim_end_matches(".md");
    trimmed.rsplit('/').next().unwrap_or(trimmed)
}

fn first_heading(body: &str) -> Option<String> {
    body.lines().find_map(|line| {
        line.trim()
            .strip_prefix("# ")
            .map(|heading| heading.trim().to_string())
    })
}

/// First sentence of the first prose paragraph, whitespace-collapsed.
fn first_sentence(body: &str) -> Option<String> {
    let line = body.lines().map(str::trim).find(|line| {
        !line.is_empty() && !line.starts_with('#') && !line.starts_with("---") && *line != "```"
    })?;
    let collapsed = line.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut end = collapsed.len();
    for (offset, character) in collapsed.char_indices() {
        if matches!(character, '.' | '!' | '?') {
            let next = collapsed[offset + character.len_utf8()..].chars().next();
            if next.is_none_or(|next| next == ' ') {
                end = offset + character.len_utf8();
                break;
            }
        }
    }
    Some(collapsed[..end].to_string())
}

fn truncate_chars(value: &str, limit: usize) -> String {
    if value.chars().count() <= limit {
        return value.to_string();
    }
    let mut truncated: String = value
        .chars()
        .take(limit.saturating_sub(1))
        .collect::<String>()
        .trim_end()
        .to_string();
    // Never cut inside a wikilink: an unterminated `[[` swallows following
    // markdown when linted or rendered. Drop the partial link entirely and
    // land the ellipsis after the last complete link or word.
    if let Some(open) = truncated.rfind("[[")
        && !truncated[open..].contains("]]")
    {
        truncated.truncate(open);
        truncated = truncated
            .trim_end()
            .trim_end_matches([',', ';', ':'])
            .trim_end()
            .to_string();
    }
    if !truncated.is_empty() {
        truncated.push('…');
    }
    truncated
}

/// Manifest entries ordered by source id.
fn source_records_by_id(vault_root: &Path) -> Result<Vec<SourceRecord>, WikiError> {
    let mut entries = SourceManifest::read(vault_root)?.entries;
    entries.sort_by(|left, right| left.id.cmp(&right.id));
    Ok(entries)
}

fn write_if_changed(path: &Path, content: &str) -> Result<(), WikiError> {
    match fs::read_to_string(path) {
        Ok(existing) if existing == content => return Ok(()),
        Ok(_) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => {
            return Err(WikiError::Io {
                action: "read wiki catalog file",
                path: Some(path.to_path_buf()),
                source: error,
            });
        }
    }
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create wiki catalog directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }
    fs::write(path, content).map_err(|error| WikiError::Io {
        action: "write wiki catalog file",
        path: Some(path.to_path_buf()),
        source: error,
    })
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::Path;

    use super::*;
    use crate::sources::{SourceDraft, SourceKind};

    fn write_page(root: &Path, relative: &str, content: &str) {
        let path = root.join(relative);
        fs::create_dir_all(path.parent().expect("page parent")).expect("create parent");
        fs::write(path, content).expect("page written");
    }

    fn page(title: &str, body: &str) -> String {
        format!("---\ntitle: \"{title}\"\n---\n\n{body}\n")
    }

    fn catalog_files(root: &Path) -> (String, String, String) {
        (
            fs::read_to_string(root.join("_index.md")).expect("_index.md"),
            fs::read_to_string(root.join("knowledge/INDEX.md")).expect("knowledge/INDEX.md"),
            fs::read_to_string(root.join("code/INDEX.md")).expect("code/INDEX.md"),
        )
    }

    #[test]
    fn regenerate_is_byte_identical_on_rerun() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "knowledge/concepts/gcode.md",
            &page("Gcode", "Code index CLI. Second sentence."),
        );
        write_page(
            root,
            "knowledge/topics/dispatch.md",
            &page("Dispatch", "Links to [[gcode]] twice: [[gcode]]."),
        );
        write_page(root, "code/files/src/lib.rs.md", &page("lib.rs", "Entry."));
        SourceManifest::register(
            root,
            SourceDraft::new(
                "raw/research/note.md",
                SourceKind::Text,
                "unix-ms:1751500000000",
                b"note".to_vec(),
            ),
        )
        .expect("source registered");

        regenerate(root, &ScopeIdentity::project("/repo")).expect("first regenerate");
        let first = catalog_files(root);
        regenerate(root, &ScopeIdentity::project("/repo")).expect("second regenerate");
        let second = catalog_files(root);

        assert_eq!(first, second);
    }

    #[test]
    fn catalog_indexes_carry_generated_frontmatter() {
        // Generated indexes are first-class lint pages: regeneration stamps a
        // valid frontmatter block marked `generated_by`, so lint reports no
        // missing-frontmatter false positive and backlink reciprocity
        // classifies their fan-out links as machine-written (#17806).
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "knowledge/concepts/gcode.md",
            &page("Gcode", "Code index CLI."),
        );
        write_page(root, "code/files/src/lib.rs.md", &page("lib.rs", "Entry."));

        regenerate(root, &ScopeIdentity::project("/repo")).expect("regenerate");

        let (_, knowledge_index, code_index) = catalog_files(root);
        for index in [&knowledge_index, &code_index] {
            assert!(index.starts_with("---\n"), "{index}");
            assert!(index.contains("generated_by: gwiki-catalog"), "{index}");
        }
        let report = crate::lint::run(root, ScopeIdentity::topic("ops")).expect("lint runs");
        assert!(
            report.missing_frontmatter.is_empty(),
            "{:?}",
            report.missing_frontmatter
        );
        assert!(
            report.missing_backlinks.is_empty(),
            "{:?}",
            report.missing_backlinks
        );
    }

    #[test]
    fn regenerate_drops_code_module_link_after_page_removed() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        // A synthetic cross-directory cluster module page, as a codewiki heal emits.
        write_page(
            root,
            "code/modules/src/foo/cluster.md",
            &page("cluster", "Call-connected cluster module."),
        );
        regenerate(root, &ScopeIdentity::project("/repo")).expect("first regenerate");
        let before = fs::read_to_string(root.join("code/INDEX.md")).expect("code/INDEX.md");
        assert!(before.contains("code/modules/src/foo/cluster"));

        // A later incremental heal re-clusters and removes the page. Regenerating
        // the catalog must drop the now-stale link, or `code/INDEX.md` dangles and
        // grows `curated_broken_link_count` (the codewiki-nightly convergence bug:
        // the nightly flow `gcode codewiki` -> `gwiki index` now regenerates here).
        fs::remove_file(root.join("code/modules/src/foo/cluster.md")).expect("remove page");
        regenerate(root, &ScopeIdentity::project("/repo")).expect("second regenerate");
        let after = fs::read_to_string(root.join("code/INDEX.md")).expect("code/INDEX.md");
        assert!(!after.contains("code/modules/src/foo/cluster"));
    }

    #[test]
    fn regenerate_excludes_archived_pages_from_indexes() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "knowledge/concepts/kept.md",
            &page("Kept", "Still live."),
        );
        write_page(
            root,
            "knowledge/concepts/retired.md",
            "---\ntitle: \"Retired\"\nlifecycle: archived\n---\n\nSuperseded.\n",
        );
        write_page(
            root,
            "code/files/src/old.rs.md",
            "---\ntitle: \"old.rs\"\nlifecycle: archived\n---\n\nRemoved module.\n",
        );

        regenerate(root, &ScopeIdentity::project("/repo")).expect("regenerate");
        let (_, knowledge_index, code_index) = catalog_files(root);

        assert!(knowledge_index.contains("knowledge/concepts/kept"));
        assert!(!knowledge_index.contains("retired"));
        assert!(!code_index.contains("old.rs"));
        // The archived files themselves stay in place at stable paths.
        assert!(root.join("knowledge/concepts/retired.md").exists());
        assert!(root.join("code/files/src/old.rs.md").exists());
    }

    #[test]
    fn overview_recent_work_lists_recaps_newest_first() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        for (date, body) in [
            ("2026-07-01", "Old work."),
            ("2026-07-02", "Older work."),
            ("2026-07-03", "Recent work."),
            ("2026-07-04", "Shipped the recap feature. More detail."),
        ] {
            write_page(
                root,
                &format!("recaps/{date}.md"),
                &format!("---\ntitle: \"Recap: {date}\"\n---\n# Recap: {date}\n\n{body}\n"),
            );
        }

        regenerate(root, &ScopeIdentity::project("/repo")).expect("regenerate");

        let index = fs::read_to_string(root.join("_index.md")).expect("_index.md");
        let recent = index
            .lines()
            .find(|line| line.starts_with("Recent work: "))
            .expect("recent work line rendered");
        assert!(
            recent.contains("[[recaps/2026-07-04|2026-07-04]] — Shipped the recap feature."),
            "latest recap leads with its one-liner: {recent}"
        );
        let position = |needle: &str| recent.find(needle).map(|at| (needle.to_string(), at));
        let latest = position("recaps/2026-07-04").expect("latest listed");
        let middle = position("recaps/2026-07-03").expect("middle listed");
        let earliest = position("recaps/2026-07-02").expect("earliest kept listed");
        assert!(latest.1 < middle.1 && middle.1 < earliest.1, "{recent}");
        assert!(
            !recent.contains("recaps/2026-07-01"),
            "limit keeps only the newest {RECENT_RECAPS_LIMIT}: {recent}"
        );
    }

    #[test]
    fn overview_omits_recent_work_without_recaps() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();

        regenerate(root, &ScopeIdentity::project("/repo")).expect("regenerate");

        let index = fs::read_to_string(root.join("_index.md")).expect("_index.md");
        assert!(!index.contains("Recent work:"), "{index}");
    }

    #[test]
    fn wiki_index_lists_pages_with_bounded_one_liners() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let long_sentence = "word ".repeat(80);
        write_page(
            root,
            "knowledge/concepts/gcode.md",
            &page("Gcode", &format!("{long_sentence}end.")),
        );
        write_page(
            root,
            "knowledge/topics/dispatch.md",
            &page("Dispatch", "Routes manifest state. Extra detail."),
        );

        regenerate(root, &ScopeIdentity::project("/repo")).expect("regenerate");

        let index = fs::read_to_string(root.join("_index.md")).expect("_index.md");
        assert!(
            index.contains("- [[knowledge/topics/dispatch|Dispatch]] — Routes manifest state."),
            "{index}"
        );
        let concept_line = index
            .lines()
            .find(|line| line.starts_with("- ") && line.contains("knowledge/concepts/gcode"))
            .expect("concept listed");
        let one_liner = concept_line
            .split(" — ")
            .nth(1)
            .expect("one-liner rendered");
        assert!(one_liner.ends_with('…'), "{concept_line}");
        assert!(
            "Gcode".chars().count() + 3 + one_liner.chars().count() <= ONE_LINER_MAX_CHARS,
            "{concept_line}"
        );
    }

    #[test]
    fn one_liner_truncation_never_cuts_inside_a_wikilink() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        // First body line is a long link list (the concept-page `Sources:`
        // shape) whose budget cut lands mid-link.
        let sources_line = (1..=8)
            .map(|index| {
                format!("[[knowledge/sources/src-{index:02}-very-long-digest-path-{index:02}|Session {index:02}]]")
            })
            .collect::<Vec<_>>()
            .join(", ");
        write_page(
            root,
            "knowledge/concepts/gcode.md",
            &page("Gcode", &format!("Sources: {sources_line}")),
        );

        regenerate(root, &ScopeIdentity::project("/repo")).expect("regenerate");

        let index = fs::read_to_string(root.join("_index.md")).expect("_index.md");
        let concept_line = index
            .lines()
            .find(|line| line.starts_with("- ") && line.contains("knowledge/concepts/gcode"))
            .expect("concept listed");
        assert_eq!(
            concept_line.matches("[[").count(),
            concept_line.matches("]]").count(),
            "unterminated wikilink in catalog entry: {concept_line}"
        );
        assert!(concept_line.ends_with('…'), "{concept_line}");
    }

    #[test]
    fn overview_block_stays_bounded_with_adversarial_titles() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let long_title = "T".repeat(600);
        for index in 0..6 {
            write_page(
                root,
                &format!("knowledge/concepts/concept-{index}.md"),
                &page(&format!("{long_title}{index}"), "Body sentence."),
            );
        }

        regenerate(root, &ScopeIdentity::project("/repo")).expect("regenerate");

        let index = fs::read_to_string(root.join("_index.md")).expect("_index.md");
        let overview_start = index.find("## Overview").expect("overview present");
        let overview_end = index[overview_start..]
            .find("\n## ")
            .map(|offset| overview_start + offset)
            .unwrap_or(index.len());
        let overview = &index[overview_start..overview_end];
        assert!(
            overview.chars().count() <= OVERVIEW_MAX_CHARS,
            "overview is {} chars",
            overview.chars().count()
        );
        assert!(overview.contains("Totals: 6 concepts"), "{overview}");
        assert!(overview.contains("gwiki search"), "{overview}");
    }

    #[test]
    fn top_concepts_ranked_by_inbound_links() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "knowledge/concepts/alpha.md",
            &page("Alpha", "Mentions [[beta]] once."),
        );
        write_page(root, "knowledge/concepts/beta.md", &page("Beta", "Body."));
        write_page(
            root,
            "knowledge/topics/tour.md",
            &page("Tour", "See [[beta]] and [[knowledge/concepts/beta|Beta]]."),
        );

        regenerate(root, &ScopeIdentity::topic("research")).expect("regenerate");

        let index = fs::read_to_string(root.join("_index.md")).expect("_index.md");
        let top_line = index
            .lines()
            .find(|line| line.starts_with("Top concepts:"))
            .expect("top concepts line");
        let beta = top_line.find("beta|Beta").expect("beta ranked");
        let alpha = top_line.find("alpha|Alpha").expect("alpha ranked");
        assert!(beta < alpha, "{top_line}");
    }

    #[test]
    fn knowledge_index_lists_sources_id_ordered() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let zebra = SourceManifest::register(
            root,
            SourceDraft::new(
                "raw/research/zebra.md",
                SourceKind::Text,
                "unix-ms:1751500000000",
                b"zebra".to_vec(),
            )
            .with_title("Zebra"),
        )
        .expect("zebra registered");
        let apple = SourceManifest::register(
            root,
            SourceDraft::new(
                "raw/research/apple.md",
                SourceKind::Text,
                "unix-ms:1751500000001",
                b"apple".to_vec(),
            )
            .with_title("Apple"),
        )
        .expect("apple registered");
        write_page(
            root,
            &format!("knowledge/sources/{}.md", zebra.id),
            &page("Zebra", "Zebra digest."),
        );
        write_page(
            root,
            &format!("knowledge/sources/{}.md", apple.id),
            &page("Apple", "Apple digest."),
        );

        regenerate(root, &ScopeIdentity::global()).expect("regenerate");

        let knowledge = fs::read_to_string(root.join("knowledge/INDEX.md")).expect("INDEX.md");
        let mut expected = [zebra.id.clone(), apple.id.clone()];
        expected.sort();
        let first = knowledge.find(&expected[0]).expect("first source listed");
        let second = knowledge.find(&expected[1]).expect("second source listed");
        assert!(first < second, "{knowledge}");
        assert!(
            knowledge.contains(&format!("knowledge/sources/{}", apple.id)),
            "{knowledge}"
        );
    }

    #[test]
    fn knowledge_index_skips_links_for_missing_digest_pages() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let digested = SourceManifest::register(
            root,
            SourceDraft::new(
                "raw/research/digested.md",
                SourceKind::Text,
                "unix-ms:1751500000000",
                b"digested".to_vec(),
            )
            .with_title("Digested"),
        )
        .expect("digested registered");
        let purged = SourceManifest::register(
            root,
            SourceDraft::new(
                "raw/research/purged.md",
                SourceKind::Text,
                "unix-ms:1751500000001",
                b"purged".to_vec(),
            )
            .with_title("Purged"),
        )
        .expect("purged registered");
        write_page(
            root,
            &format!("knowledge/sources/{}.md", digested.id),
            &page("Digested", "Digest body."),
        );

        regenerate(root, &ScopeIdentity::global()).expect("regenerate");

        let knowledge = fs::read_to_string(root.join("knowledge/INDEX.md")).expect("INDEX.md");
        assert!(
            knowledge.contains(&format!("[[knowledge/sources/{}|Digested]]", digested.id)),
            "{knowledge}"
        );
        assert!(
            !knowledge.contains(&format!("knowledge/sources/{}", purged.id)),
            "{knowledge}"
        );
        assert!(
            knowledge.contains("- Purged — text raw/research/purged.md"),
            "{knowledge}"
        );
    }

    #[test]
    fn code_index_groups_codewiki_sections() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "code/narrative/01-overview.md",
            &page("Overview", "Chapter one."),
        );
        write_page(
            root,
            "code/modules/search.md",
            &page("Search", "Module page."),
        );
        write_page(root, "code/files/src/lib.rs.md", &page("lib.rs", "Entry."));

        regenerate(root, &ScopeIdentity::project("/repo")).expect("regenerate");

        let code = fs::read_to_string(root.join("code/INDEX.md")).expect("code/INDEX.md");
        let handbook = code.find("## Handbook").expect("handbook section");
        let modules = code.find("## Modules").expect("modules section");
        let files = code.find("## Files").expect("files section");
        assert!(handbook < modules && modules < files, "{code}");
        assert!(
            code.contains("[[code/narrative/01-overview|Overview]]"),
            "{code}"
        );
        assert!(code.contains("[[code/modules/search|Search]]"), "{code}");
        assert!(code.contains("[[code/files/src/lib.rs|lib.rs]]"), "{code}");
    }

    #[test]
    fn regenerate_writes_folder_context_files() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "knowledge/concepts/gcode.md",
            &page("Gcode", "Code index CLI. Second sentence."),
        );
        write_page(
            root,
            "code/modules/search/query.md",
            &page("Query", "Query planning."),
        );

        let report = regenerate(root, &ScopeIdentity::project("/repo")).expect("regenerate");

        let concepts_context = fs::read_to_string(root.join("knowledge/concepts/_context.md"))
            .expect("concepts context");
        assert!(
            concepts_context.contains("## Pages (1)"),
            "{concepts_context}"
        );
        assert!(
            concepts_context.contains("[[knowledge/concepts/gcode|Gcode]] — Code index CLI."),
            "{concepts_context}"
        );

        // Intermediate folders without direct pages still link downward.
        let knowledge_context =
            fs::read_to_string(root.join("knowledge/_context.md")).expect("knowledge context");
        assert!(
            knowledge_context.contains("## Subfolders"),
            "{knowledge_context}"
        );
        assert!(
            knowledge_context.contains("- [[knowledge/concepts/_context|concepts/]]"),
            "{knowledge_context}"
        );
        assert!(
            root.join("code/modules/search/_context.md").exists(),
            "nested code folder gets a context file"
        );
        assert!(
            report
                .context_paths
                .contains(&root.join("knowledge/concepts/_context.md")),
            "{:?}",
            report.context_paths
        );

        let first = fs::read_to_string(root.join("knowledge/_context.md")).expect("first");
        regenerate(root, &ScopeIdentity::project("/repo")).expect("second regenerate");
        assert_eq!(
            fs::read_to_string(root.join("knowledge/_context.md")).expect("second"),
            first,
            "context files are byte-identical on rerun"
        );
    }

    #[test]
    fn folder_context_excludes_candidates_and_archived_unlike_indexes() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "knowledge/concepts/kept.md",
            &page("Kept", "Promoted page."),
        );
        write_page(
            root,
            "knowledge/concepts/pending.md",
            "---\ntitle: \"Pending\"\ncandidate: true\n---\n\nQuarantined candidate.\n",
        );
        write_page(
            root,
            "knowledge/concepts/retired.md",
            "---\ntitle: \"Retired\"\nlifecycle: archived\n---\n\nArchived page.\n",
        );

        regenerate(root, &ScopeIdentity::project("/repo")).expect("regenerate");

        let context = fs::read_to_string(root.join("knowledge/concepts/_context.md"))
            .expect("concepts context");
        assert!(
            context.contains("[[knowledge/concepts/kept|Kept]]"),
            "{context}"
        );
        assert!(
            !context.contains("Pending") && !context.contains("Retired"),
            "agent context excludes candidates and archived pages: {context}"
        );

        // The INDEX stays the maintainer surface: candidates listed, archived out.
        let index = fs::read_to_string(root.join("knowledge/INDEX.md")).expect("knowledge index");
        assert!(index.contains("Pending"), "{index}");
        assert!(!index.contains("Retired"), "{index}");
    }

    #[test]
    fn stale_folder_context_is_removed_when_folder_empties() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "knowledge/concepts/only.md",
            &page("Only", "Sole page."),
        );
        regenerate(root, &ScopeIdentity::project("/repo")).expect("first regenerate");
        assert!(root.join("knowledge/concepts/_context.md").exists());

        fs::remove_file(root.join("knowledge/concepts/only.md")).expect("page removed");
        regenerate(root, &ScopeIdentity::project("/repo")).expect("second regenerate");

        assert!(
            !root.join("knowledge/concepts/_context.md").exists(),
            "emptied folder loses its context file"
        );
    }

    #[test]
    fn regenerate_restores_ai_readme_only_when_missing() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "knowledge/concepts/gcode.md",
            &page("Gcode", "Code index CLI."),
        );

        regenerate(root, &ScopeIdentity::project("/repo")).expect("regenerate creates");
        let readme_path = root.join(crate::vault::AI_README_FILE);
        assert_eq!(
            fs::read_to_string(&readme_path).expect("readme"),
            crate::vault::AI_README_TEMPLATE
        );

        fs::write(&readme_path, "# Customized\n").expect("customized");
        regenerate(root, &ScopeIdentity::project("/repo")).expect("regenerate preserves");
        assert_eq!(
            fs::read_to_string(&readme_path).expect("customized readme"),
            "# Customized\n",
            "user edits are never overwritten"
        );

        fs::remove_file(&readme_path).expect("readme removed");
        regenerate(root, &ScopeIdentity::project("/repo")).expect("regenerate restores");
        assert_eq!(
            fs::read_to_string(&readme_path).expect("restored readme"),
            crate::vault::AI_README_TEMPLATE
        );
    }
}
