//! Breadcrumb-closure stubs (#17639).
//!
//! Every generated page carries deterministic navigation links: module pages
//! emit `Parent: [[code/modules/<parent>]]` (or `[[code/repo]]` at the top),
//! and file pages emit `Module: [[code/modules/<module>]]`. A scoped write
//! pass only regenerates pages inside its scope filter, so a nested module
//! page can link ancestors — and `code/repo.md` — that no run ever produced,
//! leaving dangling breadcrumbs (existence-aware de-linking is not an option:
//! it would bake staleness into one-shot generated pages).
//!
//! After the sink finishes pruning, this module computes the breadcrumb
//! closure of the on-disk page set and synthesizes a deterministic structural
//! stub (child-module table + direct-file table) for every required module
//! page that is missing, plus a repository-overview stub when `code/repo.md`
//! itself is missing. Stubs are marked `stub: true` in frontmatter and carry a
//! `stub:`-prefixed invalidation key in the doc meta; pages recorded as stubs
//! are re-synthesized each pass so their tables track the vault, while real
//! generated pages are never touched. A later unscoped run replaces stubs
//! with full pages through the normal write path.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use super::io::{collect_generated_doc_pages, scoped_file_doc, scoped_module_doc};
use super::render::cell_summary;
use super::{
    BuiltDoc, CodewikiDocMeta, FileLink, ModuleDoc, ModuleLink, direct_child_modules,
    display_child_summary, file_doc_path, file_wikilink, frontmatter_aggregate_without_ranges,
    module_ancestors, module_depth, module_doc_path, module_for_file, module_wikilink,
    parent_module, render_module_doc, structural_module_summary, structural_repo_summary,
    write_markdown_table_header, write_markdown_table_row, write_section,
};
use crate::index::hasher;

const REPO_DOC_PATH: &str = "code/repo.md";
const STUB_KEY_PREFIX: &str = "stub:";
const STUB_FRONTMATTER_LINE: &str = "stub: true";

/// True when the recorded meta entry marks the on-disk page as a synthesized
/// stub (safe to re-synthesize) rather than a real generated page.
pub(crate) fn is_stub_meta(meta: Option<&CodewikiDocMeta>) -> bool {
    meta.and_then(|meta| meta.invalidation_key.as_deref())
        .is_some_and(|key| key.starts_with(STUB_KEY_PREFIX))
}

/// Inserts the `stub: true` marker as the last line of the page's YAML
/// frontmatter, so downstream consumers (gwiki ingest, humans, a later run's
/// vault audit) can tell a synthesized navigation stub from generated prose.
fn mark_stub_frontmatter(content: String) -> String {
    let Some(close) = content
        .strip_prefix("---\n")
        .and_then(|rest| rest.find("\n---\n"))
        // `+ 4` re-bases past the stripped opening fence; `+ 1` steps over the
        // matched newline so the marker lands on its own line before `---`.
        .map(|index| index + 4 + 1)
    else {
        return content;
    };
    let mut out = String::with_capacity(content.len() + STUB_FRONTMATTER_LINE.len() + 1);
    out.push_str(&content[..close]);
    out.push_str(STUB_FRONTMATTER_LINE);
    out.push('\n');
    out.push_str(&content[close..]);
    out
}

fn stub_doc(path: String, content: String, summary: String) -> BuiltDoc {
    let content = mark_stub_frontmatter(content);
    let invalidation_key = format!(
        "{STUB_KEY_PREFIX}{}",
        hasher::content_hash(content.as_bytes())
    );
    BuiltDoc {
        path,
        content,
        degraded: false,
        summary: Some(summary),
        neighbors: BTreeSet::new(),
        invalidation_key: Some(invalidation_key),
        invalidation_key_requires_sources: false,
    }
}

fn recorded_module_summary(
    doc_meta: &BTreeMap<String, CodewikiDocMeta>,
    stub_summaries: &BTreeMap<String, String>,
    module: &str,
) -> String {
    stub_summaries
        .get(module)
        .cloned()
        .or_else(|| {
            doc_meta
                .get(&module_doc_path(module))
                .and_then(|meta| meta.summary.clone())
        })
        .unwrap_or_default()
}

fn recorded_file_summary(doc_meta: &BTreeMap<String, CodewikiDocMeta>, file: &str) -> String {
    doc_meta
        .get(&file_doc_path(file))
        .and_then(|meta| meta.summary.clone())
        .unwrap_or_default()
}

/// Computes the breadcrumb-closure stubs for the current on-disk page set.
///
/// `doc_meta` is the sink's post-prune meta map, consulted for two things:
/// which existing pages are themselves stubs (re-synthesized so their tables
/// track the vault), and the recorded page summaries that fill the stub
/// tables' Summary column.
pub(crate) fn breadcrumb_stub_docs(
    out_dir: &Path,
    doc_meta: &BTreeMap<String, CodewikiDocMeta>,
) -> anyhow::Result<Vec<BuiltDoc>> {
    let mut module_pages = BTreeSet::new();
    let mut file_pages = BTreeSet::new();
    let mut repo_exists = false;
    for page in collect_generated_doc_pages(out_dir)? {
        if let Some(module) = scoped_module_doc(&page) {
            module_pages.insert(module.to_string());
        } else if let Some(file) = scoped_file_doc(&page) {
            file_pages.insert(file.to_string());
        } else if page == REPO_DOC_PATH {
            repo_exists = true;
        }
    }
    if module_pages.is_empty() && file_pages.is_empty() {
        return Ok(Vec::new());
    }

    // The closure: every ancestor of every module page, plus every ancestor of
    // each file page's directory module. A file's assigned cluster module is
    // always a directory prefix of the file's path, so the directory chain
    // covers the `Module:` link target even when clustering coarsened it.
    let mut required = BTreeSet::new();
    for module in &module_pages {
        required.extend(module_ancestors(module));
    }
    for file in &file_pages {
        required.extend(module_ancestors(&module_for_file(file)));
    }

    let mut targets = required
        .iter()
        .filter(|module| {
            !module_pages.contains(*module) || is_stub_meta(doc_meta.get(&module_doc_path(module)))
        })
        .cloned()
        .collect::<Vec<_>>();
    // Deepest-first, so a parent stub's child table can pick up the structural
    // summary of a child stub synthesized in the same pass.
    targets.sort_by_key(|module| std::cmp::Reverse(module_depth(module)));

    // After this pass every required module resolves; child tables link into
    // that full set, never beyond it.
    let known_modules = module_pages
        .union(&required)
        .cloned()
        .collect::<BTreeSet<_>>();
    let mut stub_summaries: BTreeMap<String, String> = BTreeMap::new();
    let mut docs = Vec::new();
    for module in targets {
        let child_modules = direct_child_modules(&module, known_modules.iter())
            .into_iter()
            .map(|child| ModuleLink {
                summary: recorded_module_summary(doc_meta, &stub_summaries, &child),
                source_spans: Vec::new(),
                module: child,
            })
            .collect::<Vec<_>>();
        let direct_files = file_pages
            .iter()
            .filter(|file| module_for_file(file) == module)
            .map(|file| FileLink {
                path: file.clone(),
                summary: recorded_file_summary(doc_meta, file),
                source_spans: Vec::new(),
            })
            .collect::<Vec<_>>();
        let summary = structural_module_summary(&module, &direct_files, &child_modules);
        stub_summaries.insert(module.clone(), summary.clone());
        let content = render_module_doc(&ModuleDoc {
            module: module.clone(),
            summary: summary.clone(),
            source_spans: Vec::new(),
            direct_files,
            child_modules,
            degraded: false,
            degraded_sources: Vec::new(),
            verify_notes: Vec::new(),
            reused_page: None,
        });
        docs.push(stub_doc(module_doc_path(&module), content, summary));
    }

    // Top-level module pages and root-module file pages point their
    // Parent/Module breadcrumb at `code/repo.md`; synthesize it when no run
    // has produced the real repository overview (scoped runs never do).
    if !repo_exists || is_stub_meta(doc_meta.get(REPO_DOC_PATH)) {
        docs.push(repo_stub_doc(
            &known_modules,
            &file_pages,
            doc_meta,
            &stub_summaries,
        ));
    }
    Ok(docs)
}

fn repo_stub_doc(
    known_modules: &BTreeSet<String>,
    file_pages: &BTreeSet<String>,
    doc_meta: &BTreeMap<String, CodewikiDocMeta>,
    stub_summaries: &BTreeMap<String, String>,
) -> BuiltDoc {
    let top_modules = known_modules
        .iter()
        .filter(|module| parent_module(module).is_none())
        .cloned()
        .collect::<Vec<_>>();
    let root_files = file_pages
        .iter()
        .filter(|file| module_for_file(file).is_empty())
        .cloned()
        .collect::<Vec<_>>();
    let summary = structural_repo_summary(file_pages.len(), known_modules.len());
    // Unlike the generated repository overview, the stub links no narrative
    // chapters or concept index — those pages only exist after a full run, and
    // a stub must never introduce dangling links of its own.
    let mut doc =
        frontmatter_aggregate_without_ranges("Repository Overview", "code_repo", &[], &[], None);
    doc.push_str("# Repository Overview\n\n");
    write_section(&mut doc, "Overview", &summary);
    if !top_modules.is_empty() {
        doc.push_str("## Modules\n\n");
        write_markdown_table_header(&mut doc, &["Module", "Summary"]);
        for module in &top_modules {
            // Leading-paragraph gist only, matching the generated repository
            // overview's reference appendix; the full brief lives on the page.
            let module_summary =
                cell_summary(&recorded_module_summary(doc_meta, stub_summaries, module));
            write_markdown_table_row(&mut doc, [module_wikilink(module), module_summary]);
        }
        doc.push('\n');
    }
    if !root_files.is_empty() {
        doc.push_str("## Files\n\n");
        write_markdown_table_header(&mut doc, &["File", "Summary"]);
        for file in &root_files {
            let file_summary = display_child_summary(&recorded_file_summary(doc_meta, file), file)
                .unwrap_or_default();
            write_markdown_table_row(&mut doc, [file_wikilink(file), file_summary]);
        }
        doc.push('\n');
    }
    stub_doc(REPO_DOC_PATH.to_string(), doc, summary)
}
