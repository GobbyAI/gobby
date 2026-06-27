use super::super::build::default_chapter_links;
use super::super::*;
use super::{cell_summary, model_degraded_sources};
use crate::index::hasher;

// The repo overview aggregates many independent, already-separate inputs
// (content, links, code-graph, and the shared generate/reuse/progress build
// context); threading them as a single bag would obscure more than it helps.
#[allow(clippy::too_many_arguments)]
pub(crate) fn build_repo_doc(
    files: &[FileDoc],
    modules: &[ModuleDoc],
    leading_chunks: &BTreeMap<String, LeadingChunk>,
    audit_links: &[(&str, &str)],
    graph_edges: &[CodewikiGraphEdge],
    graph_availability: CodewikiGraphAvailability,
    generate: &mut Option<&mut TextGenerator<'_>>,
    reuse: &mut Option<&mut ReusePlan>,
    progress: &mut CodewikiProgress,
) -> (String, bool, String) {
    let top_modules = modules
        .iter()
        .filter(|module| parent_module(&module.module).is_none())
        .map(|module| ModuleLink {
            module: module.module.clone(),
            summary: module.summary.clone(),
            source_spans: module.source_spans.clone(),
        })
        .collect::<Vec<_>>();
    let root_files = files
        .iter()
        .filter(|file| file.module.is_empty())
        .map(|file| FileLink {
            path: file.path.clone(),
            summary: file.summary.clone(),
            source_spans: file.source_spans.clone(),
        })
        .collect::<Vec<_>>();
    let module_summaries = top_modules
        .iter()
        .map(|module| prompts::ChildSummary {
            name: module.module.clone(),
            summary: module.summary.clone(),
        })
        .collect::<Vec<_>>();
    let file_summaries = root_files
        .iter()
        .map(|file| prompts::ChildSummary {
            name: file.path.clone(),
            summary: file.summary.clone(),
        })
        .collect::<Vec<_>>();
    let fallback = structural_repo_summary(files.len(), modules.len());
    let source_spans = collect_link_spans(&root_files, &top_modules);
    let source_files = span_files(&source_spans);
    // Subsystem-level import edges from the indexed code graph drive the
    // overview's dependency diagram. Resolved before the reuse check so the
    // cache key tracks graph changes (a new edge re-renders the page); the
    // diagram degrades to nothing — never to a degraded skeleton — when the
    // graph backend is unavailable.
    let dependency_edges = match graph_availability {
        CodewikiGraphAvailability::Unavailable => Default::default(),
        CodewikiGraphAvailability::Available | CodewikiGraphAvailability::Truncated => {
            let file_paths: Vec<String> = files.iter().map(|file| file.path.clone()).collect();
            let roots = cluster::subsystem_roots(&file_paths);
            collect_subsystem_dependency_edges(&roots, files, graph_edges)
        }
    };
    let mut repo_key = repo_audit_link_key(audit_links);
    repo_key.push_str("repo-dep-edges:v1\n");
    for (from, to) in &dependency_edges {
        repo_key.push_str(from);
        repo_key.push('\t');
        repo_key.push_str(to);
        repo_key.push('\n');
    }
    // The repo overview's provenance rolls up every source file, so it is
    // reusable only when nothing changed at all and the deterministic audit-link
    // appendix set is the same; nothing downstream consumes its summary, so the
    // on-disk page is returned verbatim.
    if let Some(page) = reuse.as_deref_mut().and_then(|plan| {
        plan.reusable_page_keyed_with_sources("code/repo.md", &repo_key, &source_files)
    }) {
        progress.emit("reusing repo overview (sources unchanged)");
        return (page, false, repo_key);
    }
    progress.emit("generating repo overview");
    let sources = repo_source_excerpts(files, leading_chunks);
    let generation = maybe_generate(
        generate,
        &prompts::repo_prompt(&module_summaries, &file_summaries, &sources),
        prompts::REPO_SYSTEM,
        PromptTier::Aggregate,
    );
    let degraded = generation.failed();
    let summary = match generation {
        Generation::Generated(generated) => {
            let markers = citation_markers(&source_spans, &generated);
            ground_text(&generated, &source_spans, Some(&markers))
        }
        Generation::Failed | Generation::Skipped => ground_text(&fallback, &source_spans, None),
    };

    // Append the bounded code-graph dependency diagram to the narrative body.
    // When the graph backend is off the edge set is empty and no diagram is
    // drawn — a benign data-source gap, distinct from an AI-generation failure,
    // so it never sets `degraded`.
    let summary = match render_dependency_diagram(&dependency_edges) {
        Some(diagram) => format!("{summary}\n\n{diagram}"),
        None => summary,
    };

    let doc = render_repo_doc(
        &summary,
        &top_modules,
        &root_files,
        audit_links,
        &source_spans,
        degraded,
    );
    (doc, degraded, repo_key)
}

fn repo_audit_link_key(audit_links: &[(&str, &str)]) -> String {
    let mut key = String::from("repo-audit-links:v1\n");
    for (label, target) in audit_links {
        key.push_str(label);
        key.push('\t');
        key.push_str(target);
        key.push('\n');
    }
    format!("repo-audit-links:{}", hasher::content_hash(key.as_bytes()))
}

/// Root-level source excerpts for the repository overview prompt; README-style
/// files rank first because they describe the system, then code roots by
/// symbol count.
fn repo_source_excerpts(
    files: &[FileDoc],
    leading_chunks: &BTreeMap<String, LeadingChunk>,
) -> Vec<prompts::SourceExcerpt> {
    let mut candidates = files
        .iter()
        .filter(|file| file.module.is_empty())
        .collect::<Vec<_>>();
    candidates.sort_by_key(|file| {
        (
            !file.path.to_ascii_lowercase().starts_with("readme"),
            std::cmp::Reverse(file.symbols.len()),
            file.path.clone(),
        )
    });
    candidates
        .into_iter()
        .filter_map(|file| source_excerpt_for_file(&file.path, leading_chunks))
        .take(prompts::MAX_PROMPT_SOURCE_EXCERPTS)
        .collect()
}

pub(crate) fn render_repo_doc(
    summary: &str,
    modules: &[ModuleLink],
    files: &[FileLink],
    audit_links: &[(&str, &str)],
    source_spans: &[SourceSpan],
    degraded: bool,
) -> String {
    let mut doc = frontmatter_with_degradation_without_ranges(
        "Repository Overview",
        "code_repo",
        source_spans,
        &model_degraded_sources(degraded),
    );
    append_relevant_source_files(&mut doc, source_spans);
    doc.push_str("# Repository Overview\n\n");
    let summary = replace_citations_with_markers(summary, source_spans);
    // Lead with the guided tour: the canonical handbook spine as a numbered
    // Chapter 1..N path, with a "new to this codebase" callout and a one-line
    // ask/search pointer. The module/file tables are demoted to a reference
    // appendix below (#853 root cause: the reference dump crowded out the
    // narrative entry points).
    doc.push_str("## Start here — guided tour\n\n");
    let chapter_links = default_chapter_links();
    if let Some((slug, title)) = chapter_links.first() {
        let _ = std::fmt::Write::write_fmt(
            &mut doc,
            format_args!("New to this codebase? Begin with [[code/narrative/{slug}|{title}]].\n\n"),
        );
    }
    for (index, (slug, title)) in chapter_links.iter().enumerate() {
        let _ = std::fmt::Write::write_fmt(
            &mut doc,
            format_args!(
                "{index}. [[code/narrative/{slug}|{title}]]\n",
                index = index + 1
            ),
        );
    }
    doc.push('\n');
    doc.push_str(
        "Browse all concepts in the [[code/concepts/index|Concept tree and narrative tours]].\n\n",
    );
    doc.push_str(
        "Ask questions across this vault with `gwiki ask \"...\"`, or find pages with `gwiki search \"...\"`.\n\n",
    );
    write_section(&mut doc, "Overview", &summary);
    // Link the deterministic analysis/catalog pages so they are reachable from
    // the front page instead of orphaned (#904). Only the pages that were
    // actually generated are passed in, so these links never dangle.
    if !audit_links.is_empty() {
        doc.push_str("## Analysis & catalogs\n\n");
        for (label, target) in audit_links {
            doc.push_str(&format!("- [[{target}|{label}]]\n"));
        }
        doc.push('\n');
    }
    let has_appendix = !modules.is_empty() || !files.is_empty();
    if has_appendix {
        doc.push_str("## Reference appendix\n\n");
    }
    if !modules.is_empty() {
        doc.push_str("### Modules\n\n");
        write_markdown_table_header(&mut doc, &["Module", "Summary"]);
        for module in modules {
            // Reference-appendix rows are navigational: keep the module's leading
            // paragraph, not its full multi-table brief (that lives on its page).
            let summary =
                replace_citations_with_markers(&cell_summary(&module.summary), source_spans);
            write_markdown_table_row(&mut doc, [module_wikilink(&module.module), summary]);
        }
        doc.push('\n');
    }
    if !files.is_empty() {
        doc.push_str("### Files\n\n");
        write_markdown_table_header(&mut doc, &["File", "Summary"]);
        for file in files {
            // Structural no-symbol filler is dropped from the front page so
            // it never reads as a wall of "has no indexed API symbols".
            match display_child_summary(&file.summary, &file.path) {
                Some(summary) => {
                    let summary = replace_citations_with_markers(&summary, source_spans);
                    write_markdown_table_row(&mut doc, [file_wikilink(&file.path), summary]);
                }
                None => {
                    write_markdown_table_row(&mut doc, [file_wikilink(&file.path), String::new()]);
                }
            }
        }
        doc.push('\n');
    }
    write_references(&mut doc, source_spans);
    doc
}
