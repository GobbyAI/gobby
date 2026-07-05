//! Deterministic catalog regeneration for the vault indexes.
//!
//! [`regenerate`] rebuilds `_index.md`, `knowledge/INDEX.md`, and
//! `code/INDEX.md` from on-disk vault state with no LLM involvement:
//! rerunning it over an unchanged vault produces byte-identical files.

use std::collections::BTreeMap;
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

/// Sections of `code/INDEX.md`, in render order, mapped to the codewiki
/// output directories they list.
const CODE_SECTIONS: &[(&str, &str)] = &[
    ("Handbook", "code/narrative"),
    ("Concepts", "code/concepts"),
    ("Modules", "code/modules"),
    ("Files", "code/files"),
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct CatalogReport {
    pub(crate) index_path: PathBuf,
    pub(crate) knowledge_index_path: PathBuf,
    pub(crate) code_index_path: PathBuf,
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

    let report = CatalogReport {
        index_path: vault_root.join("_index.md"),
        knowledge_index_path: vault_root.join("knowledge/INDEX.md"),
        code_index_path: vault_root.join("code/INDEX.md"),
    };
    let code_page_total: usize = code_sections.iter().map(|(_, pages)| pages.len()).sum();
    write_if_changed(
        &report.index_path,
        &render_wiki_index(
            vault_root,
            scope,
            &concepts,
            &topics,
            sources.len(),
            code_page_total,
            &top_concepts,
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

fn render_wiki_index(
    vault_root: &Path,
    scope: &ScopeIdentity,
    concepts: &[PageSummary],
    topics: &[PageSummary],
    source_total: usize,
    code_page_total: usize,
    top_concepts: &[PageSummary],
) -> String {
    let mut top_links: Vec<String> = top_concepts
        .iter()
        .map(|page| page_link(vault_root, page))
        .collect();
    let mut overview = render_overview(
        scope,
        concepts.len(),
        topics.len(),
        source_total,
        code_page_total,
        &top_links,
    );
    // The Overview block doubles as the session-start hot cache; shed trailing
    // top-concept links until it fits the injection budget.
    while overview.chars().count() > OVERVIEW_MAX_CHARS && !top_links.is_empty() {
        top_links.pop();
        overview = render_overview(
            scope,
            concepts.len(),
            topics.len(),
            source_total,
            code_page_total,
            &top_links,
        );
    }

    let mut markdown = String::from("# Wiki Index\n\n");
    markdown.push_str(&overview);
    render_listing_section(&mut markdown, vault_root, "Concepts", concepts);
    render_listing_section(&mut markdown, vault_root, "Topics", topics);
    markdown
}

fn render_overview(
    scope: &ScopeIdentity,
    concept_total: usize,
    topic_total: usize,
    source_total: usize,
    code_page_total: usize,
    top_links: &[String],
) -> String {
    let mut overview = String::from("## Overview\n\n");
    overview.push_str(&format!("Scope: {scope}\n"));
    overview.push_str(&format!(
        "Totals: {concept_total} concepts · {topic_total} topics · {source_total} sources · \
         {code_page_total} code pages\n"
    ));
    if !top_links.is_empty() {
        overview.push_str(&format!("Top concepts: {}\n", top_links.join(", ")));
    }
    overview.push_str(
        "Query: search with `gwiki search \"<term>\"`; full listings live in \
         [[knowledge/INDEX|knowledge/INDEX]] and [[code/INDEX|code/INDEX]].\n",
    );
    overview
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

fn render_knowledge_index(
    vault_root: &Path,
    concepts: &[PageSummary],
    topics: &[PageSummary],
    sources: &[SourceRecord],
) -> String {
    let mut markdown = String::from("# Knowledge\n");
    render_listing_section(&mut markdown, vault_root, "Concepts", concepts);
    render_listing_section(&mut markdown, vault_root, "Topics", topics);
    markdown.push_str("\n## Sources\n\n");
    if sources.is_empty() {
        markdown.push_str("(none yet)\n");
        return markdown;
    }
    for record in sources {
        let title = record.title.as_deref().unwrap_or(&record.location);
        let digest = vault_root.join("knowledge/sources").join(&record.id);
        markdown.push_str(&format!(
            "- {} — {} {}\n",
            wiki_link(vault_root, &digest, title),
            record.kind,
            record.location
        ));
    }
    markdown
}

fn render_code_index(vault_root: &Path, sections: &[(&'static str, Vec<PageSummary>)]) -> String {
    let mut markdown = String::from("# Code\n");
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
        pages.push(summarize_page(vault_root, &path, &text));
    }
    pages.sort_by(|left, right| left.relative.cmp(&right.relative));
    Ok(pages)
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
            } else if name.ends_with(".md") && name != "INDEX.md" && name != "_index.md" {
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

fn summarize_page(vault_root: &Path, path: &Path, text: &str) -> PageSummary {
    let relative = relative_path(vault_root, path);
    let (title, body) = match parse_frontmatter(text) {
        Ok(parsed) => (parsed.metadata.title.clone(), parsed.body.to_string()),
        Err(_) => (None, text.to_string()),
    };
    let title = title
        .or_else(|| first_heading(&body))
        .unwrap_or_else(|| page_stem(&relative).to_string());
    let one_liner = first_sentence(&body).map(|sentence| {
        let budget = ONE_LINER_MAX_CHARS.saturating_sub(title.chars().count() + 3);
        truncate_chars(&sentence, budget)
    });
    PageSummary {
        relative,
        title,
        one_liner: one_liner.filter(|value| !value.is_empty()),
    }
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
}
