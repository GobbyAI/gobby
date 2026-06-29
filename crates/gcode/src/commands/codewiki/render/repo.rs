use super::super::build::default_chapter_links;
use super::super::*;
use super::cell_summary;
use crate::index::hasher;

#[expect(clippy::too_many_arguments)]
pub(crate) fn build_repo_doc(
    files: &[FileDoc],
    modules: &[ModuleDoc],
    leading_chunks: &BTreeMap<String, LeadingChunk>,
    audit_links: &[(&str, &str)],
    generate: &mut Option<&mut TextGenerator<'_>>,
    tool_loop: &mut Option<&mut ToolLoopGenerator<'_>>,
    reuse: &mut Option<&mut ReusePlan>,
    progress: &mut CodewikiProgress,
    aggregate_ai_outcome: CodewikiAiOutcome,
) -> anyhow::Result<(String, bool, String)> {
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
    let repo_key = repo_audit_link_key(audit_links);
    // The repo overview's provenance rolls up every source file, so it is
    // reusable only when nothing changed at all and the deterministic audit-link
    // appendix set is the same; nothing downstream consumes its summary, so the
    // on-disk page is returned verbatim.
    if let Some(page) = reuse.as_deref_mut().and_then(|plan| {
        plan.reusable_page_keyed_with_sources_and_ai_outcome(
            "code/repo.md",
            &repo_key,
            &source_files,
            aggregate_ai_outcome,
        )
    }) {
        progress.emit("reusing repo overview (sources unchanged)");
        return Ok((page, false, repo_key));
    }
    progress.emit("generating repo overview");
    let sources = repo_source_excerpts(files, leading_chunks);
    // Aggregate-tier page: Lane B tool loop when configured, else Lane A
    // one-shot. A Lane B generation failure hard-fails the run here (no skeleton
    // fallback); a Lane A failure degrades to the structural summary.
    let aggregate = generate_aggregate(
        tool_loop,
        generate,
        &prompts::repo_prompt(&module_summaries, &file_summaries, &sources),
        prompts::REPO_SYSTEM,
        "repo overview",
    )?;
    // `data_source_degraded` (e.g. graph-unavailable) is evidence degradation:
    // listed in the page's degraded_sources but never an AI-generation failure.
    let mut degraded_sources = aggregate.data_source_degraded;
    let summary = match aggregate.content {
        GenerationContent::Generated(generated) => {
            let markers = citation_markers(&source_spans, &generated);
            let grounded = ground_text(&generated, &source_spans, Some(&markers));
            if grounded.trim().is_empty() {
                degraded_sources.push("grounding-empty".to_string());
                if aggregate.lane == LANE_TOOL_LOOP {
                    anyhow::bail!(
                        "Lane B repo overview generation grounded to empty; \
                         page not written (no skeleton, no Lane A fallback)"
                    );
                }
                ground_text(&fallback, &source_spans, None)
            } else {
                grounded
            }
        }
        GenerationContent::Failed(cause) => {
            degraded_sources.push(cause.reason_code().to_string());
            ground_text(&fallback, &source_spans, None)
        }
        GenerationContent::Skipped => ground_text(&fallback, &source_spans, None),
    };
    let degraded = degraded_sources
        .iter()
        .any(|source| source != GRAPH_UNAVAILABLE);

    let doc = render_repo_doc(
        &summary,
        &top_modules,
        &root_files,
        audit_links,
        &source_spans,
        &degraded_sources,
        aggregate.lane,
        &aggregate.observability,
    );
    Ok((doc, degraded, repo_key))
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

#[expect(clippy::too_many_arguments)]
pub(crate) fn render_repo_doc(
    summary: &str,
    modules: &[ModuleLink],
    files: &[FileLink],
    audit_links: &[(&str, &str)],
    source_spans: &[SourceSpan],
    degraded_sources: &[String],
    lane: &str,
    observability: &GenerationObservability,
) -> String {
    let lane_b = (lane == LANE_TOOL_LOOP).then_some(FrontmatterLaneB {
        lane,
        tool_call_count: observability.tool_call_count,
        turns: observability.turns,
    });
    let mut doc = frontmatter_aggregate_without_ranges(
        "Repository Overview",
        "code_repo",
        source_spans,
        degraded_sources,
        lane_b,
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

#[cfg(test)]
mod tests {
    use super::*;

    fn file_doc(path: &str) -> FileDoc {
        FileDoc {
            path: path.to_string(),
            module: String::new(),
            summary: "Root file summary.".to_string(),
            body: String::new(),
            source_spans: vec![SourceSpan {
                file: path.to_string(),
                line_start: 1,
                line_end: 3,
            }],
            symbols: Vec::new(),
            component_ids: Vec::new(),
            degraded: false,
            degraded_sources: Vec::new(),
            verify_notes: Vec::new(),
            reused_page: None,
        }
    }

    #[test]
    fn repo_lane_a_grounding_empty_falls_back_and_marks_degraded() {
        let mut files = vec![file_doc("src/lib.rs")];
        files[0].source_spans.clear();
        let mut generate = |_prompt: &str, _system: &str, _tier: PromptTier| {
            Some("[src/missing.rs:99]".to_string())
        };
        let mut generate = Some(&mut generate as &mut TextGenerator<'_>);
        let mut tool_loop = None;
        let mut reuse = None;
        let mut progress = CodewikiProgress::stderr(false);

        let (doc, degraded, _key) = build_repo_doc(
            &files,
            &[],
            &BTreeMap::new(),
            &[],
            &mut generate,
            &mut tool_loop,
            &mut reuse,
            &mut progress,
            CodewikiAiOutcome::default(),
        )
        .expect("repo falls back");

        assert!(degraded);
        assert!(doc.contains("grounding-empty"), "{doc}");
        assert!(
            doc.contains("Repository code documentation covers 1 file across 0 modules."),
            "{doc}"
        );
    }

    #[test]
    fn repo_lane_b_grounding_empty_hard_fails_without_fallback() {
        let mut files = vec![file_doc("src/lib.rs")];
        files[0].source_spans.clear();
        let mut tool_loop = |_prompt: &str, _system: &str| LaneBResult {
            outcome: GenerationOutcome::generated("[src/missing.rs:99]".to_string()),
            data_source_degraded: Vec::new(),
        };
        let mut tool_loop = Some(&mut tool_loop as &mut ToolLoopGenerator<'_>);
        let mut generate = None;
        let mut reuse = None;
        let mut progress = CodewikiProgress::stderr(false);

        let error = build_repo_doc(
            &files,
            &[],
            &BTreeMap::new(),
            &[],
            &mut generate,
            &mut tool_loop,
            &mut reuse,
            &mut progress,
            CodewikiAiOutcome::generated(gobby_core::config::AiRouting::Daemon, false),
        )
        .expect_err("Lane B grounding-empty hard-fails");

        assert!(error.to_string().contains("grounded to empty"), "{error}");
    }
}
