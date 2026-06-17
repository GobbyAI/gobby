use super::super::*;
use super::model_degraded_sources;
use super::render_subsystem_dependency_mermaid;

pub(crate) fn build_repo_doc(
    files: &[FileDoc],
    modules: &[ModuleDoc],
    graph_edges: &[CodewikiGraphEdge],
    leading_chunks: &BTreeMap<String, LeadingChunk>,
    generate: &mut Option<&mut TextGenerator<'_>>,
    reuse: &mut Option<&mut ReusePlan>,
    progress: &mut CodewikiProgress,
) -> (String, bool) {
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
    // The repo overview's provenance rolls up every source file, so it is
    // reusable only when nothing changed at all; nothing downstream consumes
    // its summary, so the on-disk page is returned verbatim.
    if let Some(page) = reuse
        .as_deref_mut()
        .and_then(|plan| plan.reusable_page("code/repo.md", &span_files(&source_spans)))
    {
        progress.emit("reusing repo overview (sources unchanged)");
        return (page, false);
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

    let roots = cluster::subsystem_roots(
        &files
            .iter()
            .map(|file| file.path.clone())
            .collect::<Vec<_>>(),
    );
    let module_map = render_subsystem_dependency_mermaid(&roots, files, graph_edges);
    let doc = render_repo_doc(
        &summary,
        &top_modules,
        &root_files,
        module_map.as_deref(),
        &source_spans,
        degraded,
    );
    (doc, degraded)
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
    module_map: Option<&str>,
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
    // Lead with a curated "Start here" TOC so the guided tour and concept
    // navigation are the first thing a reader sees; the module/file tables are
    // demoted to a reference appendix below (#853 root cause: the reference
    // dump crowded out the narrative entry points).
    doc.push_str("## Start here\n\n");
    doc.push_str("- [[code/narrative/introduction|Introduction]] — start of the guided tour\n");
    doc.push_str("- [[code/concepts/index|Concept tree and narrative tours]]\n\n");
    write_section(&mut doc, "Overview", &summary);
    let has_appendix = module_map.is_some() || !modules.is_empty() || !files.is_empty();
    if has_appendix {
        doc.push_str("## Reference appendix\n\n");
    }
    if let Some(diagram) = module_map {
        doc.push_str("### Module Map\n\n");
        doc.push_str(diagram);
        doc.push('\n');
    }
    if !modules.is_empty() {
        doc.push_str("### Modules\n\n");
        write_markdown_table_header(&mut doc, &["Module", "Summary"]);
        for module in modules {
            let summary = replace_citations_with_markers(&module.summary, source_spans);
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
