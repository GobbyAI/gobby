use super::super::super::*;
use super::super::curated_content::{self, CuratedPageKind};
use super::plan::{normalize_concepts, normalize_narrative_pages, normalize_sections};
use super::spans::{all_input_spans, item_spans, narrative_spans};
use super::support::{
    combine_degraded_sources, concept_doc_path, concept_doc_stem, narrative_doc_path,
};
use super::types::*;
use super::{MAX_CURATED_KEY_COMPONENTS, MAX_CURATED_SOURCE_FILE_LINKS};

#[allow(clippy::too_many_arguments)]
pub(super) fn render_curated_navigation_docs(
    files: &[FileDoc],
    modules: &[ModuleDoc],
    plan: CuratedNavigationPlan,
    degraded_sources: Vec<String>,
    lane: &'static str,
    plan_observability: &GenerationObservability,
    leading_chunks: &std::collections::BTreeMap<String, LeadingChunk>,
    generate: &mut Option<&mut TextGenerator<'_>>,
    tool_loop: &mut Option<&mut ToolLoopGenerator<'_>>,
    verify: &mut Option<&mut TextVerifier<'_>>,
) -> anyhow::Result<Vec<BuiltDoc>> {
    let module_lookup = modules
        .iter()
        .map(|module| (module.module.as_str(), module))
        .collect::<std::collections::BTreeMap<_, _>>();
    let file_lookup = files
        .iter()
        .map(|file| (file.path.as_str(), file))
        .collect::<std::collections::BTreeMap<_, _>>();
    let mut concepts = normalize_concepts(plan.concept_modules, modules, files);
    let sections = normalize_sections(plan.sections, &concepts);
    let mut narrative_pages =
        normalize_narrative_pages(plan.narrative_pages, modules, files, &concepts);
    let all_spans = all_input_spans(files, modules);

    // Per-page content pass: expand each curated page's one-line summary into a
    // grounded, multi-section body (structural fallback on skip/failure). Runs
    // after normalization so member lists are final, and before rendering so
    // the bodies are in place. Mutates `concepts` first, so `concept_titles`
    // (which borrows it) is built afterwards.
    for concept in &mut concepts {
        // Lane B investigates beyond this concept's own members via tools, so
        // grounding must accept citations to any indexed span in scope (#993);
        // restricting to member spans strips legitimately tool-fetched anchors
        // and the body reads as ungroundable. all_spans covers the wiki scope.
        let result = curated_content::curated_page_body(
            CuratedPageKind::Concept,
            &concept.title,
            &concept.summary,
            &concept.modules,
            &concept.files,
            &module_lookup,
            &file_lookup,
            leading_chunks,
            &all_spans,
            generate,
            tool_loop,
            verify,
        )?;
        concept.body = result.body;
        concept.body_degraded_sources = result.degraded_sources;
        concept.verify_notes = result.verify_notes;
        concept.body_observability = result.observability;
    }
    for page in &mut narrative_pages {
        let (member_modules, member_files) = narrative_members(page, &concepts);
        let result = curated_content::curated_page_body(
            CuratedPageKind::Narrative,
            &page.title,
            &page.summary,
            &member_modules,
            &member_files,
            &module_lookup,
            &file_lookup,
            leading_chunks,
            &all_spans,
            generate,
            tool_loop,
            verify,
        )?;
        page.body = result.body;
        page.body_degraded_sources = result.degraded_sources;
        page.verify_notes = result.verify_notes;
        page.body_observability = result.observability;
    }

    let concept_titles = concepts
        .iter()
        .map(|concept| (concept.slug.as_str(), concept.title.as_str()))
        .collect::<std::collections::BTreeMap<_, _>>();

    let mut docs = Vec::new();
    docs.push(BuiltDoc {
        path: "code/concepts/index.md".to_string(),
        content: render_concept_tree(
            &sections,
            &concepts,
            &narrative_pages,
            &all_spans,
            &degraded_sources,
            lane,
            plan_observability,
        ),
        // graph-unavailable is evidence degradation, not a page failure (#978).
        degraded: degraded_sources
            .iter()
            .any(|code| code != GRAPH_UNAVAILABLE),
        summary: Some("Curated concept navigation over the code reference.".to_string()),
        neighbors: std::collections::BTreeSet::new(),
        invalidation_key: None,
        invalidation_key_requires_sources: false,
    });

    for concept in &concepts {
        let spans = item_spans(
            &concept.modules,
            &concept.files,
            &module_lookup,
            &file_lookup,
        );
        let flow = curated_content::curated_flow_diagram(
            &concept.modules,
            &concept.files,
            &module_lookup,
            &file_lookup,
            leading_chunks,
        );
        docs.push(BuiltDoc {
            path: concept_doc_path(&concept.slug),
            content: render_concept_page(
                concept,
                &spans,
                &degraded_sources,
                lane,
                flow.as_deref(),
                &module_lookup,
                &file_lookup,
            ),
            // A failed content pass falls back to the structural body — record
            // that honestly so the meta cache and the run summary surface it
            // instead of caching the page as healthy (#900). graph-unavailable is
            // evidence degradation only and never marks the page degraded (#978).
            degraded: degraded_sources
                .iter()
                .chain(concept.body_degraded_sources.iter())
                .any(|code| code != GRAPH_UNAVAILABLE),
            summary: Some(concept.summary.clone()),
            neighbors: std::collections::BTreeSet::new(),
            invalidation_key: None,
            invalidation_key_requires_sources: false,
        });
    }

    for (index, page) in narrative_pages.iter().enumerate() {
        let spans = narrative_spans(page, &concepts, &module_lookup, &file_lookup);
        // Reciprocal neighbors in the ordered tour drive the Previous/Next nav.
        let prev = index
            .checked_sub(1)
            .map(|i| chapter_link(&narrative_pages[i]));
        let next = narrative_pages.get(index + 1).map(chapter_link);
        let (flow_modules, flow_files) = narrative_members(page, &concepts);
        let flow = curated_content::curated_flow_diagram(
            &flow_modules,
            &flow_files,
            &module_lookup,
            &file_lookup,
            leading_chunks,
        );
        docs.push(BuiltDoc {
            path: narrative_doc_path(&page.slug),
            content: render_narrative_page(
                page,
                &spans,
                &concept_titles,
                &degraded_sources,
                lane,
                prev,
                next,
                flow.as_deref(),
                &module_lookup,
                &file_lookup,
            ),
            // See the concept page above: a structural-fallback narrative is
            // degraded, not healthy, so the cache and summary must say so (#900).
            // graph-unavailable is evidence degradation only (#978).
            degraded: degraded_sources
                .iter()
                .chain(page.body_degraded_sources.iter())
                .any(|code| code != GRAPH_UNAVAILABLE),
            summary: Some(page.summary.clone()),
            neighbors: std::collections::BTreeSet::new(),
            invalidation_key: None,
            invalidation_key_requires_sources: false,
        });
    }

    Ok(docs)
}

/// Borrows a narrative chapter's `(slug, title)` for guided-tour wikilinks.
fn chapter_link(page: &NarrativePage) -> (&str, &str) {
    (page.slug.as_str(), page.title.as_str())
}

#[allow(clippy::too_many_arguments)]
fn render_concept_tree(
    sections: &[ConceptSection],
    concepts: &[ConceptModule],
    narrative_pages: &[NarrativePage],
    spans: &[SourceSpan],
    degraded_sources: &[String],
    lane: &'static str,
    observability: &GenerationObservability,
) -> String {
    let lane_b = (lane == LANE_TOOL_LOOP).then_some(FrontmatterLaneB {
        lane,
        tool_call_count: observability.tool_call_count,
        turns: observability.turns,
    });
    let mut doc = frontmatter_aggregate_without_ranges(
        "Curated Concept Navigation",
        "code_concept_tree",
        spans,
        degraded_sources,
        lane_b,
    );
    append_curated_source_files(&mut doc, spans, MAX_CURATED_SOURCE_FILE_LINKS);
    doc.push_str("# Curated Concept Navigation\n\n");
    doc.push_str("Reader-first paths into the grounded code reference.\n\n");
    // The dependency-ordered narrative chapters are the primary entry point: a
    // numbered guided tour above the concept catalog.
    let chapters = narrative_pages.iter().map(chapter_link).collect::<Vec<_>>();
    curated_content::append_guided_tour(&mut doc, &chapters);
    doc.push_str("## Concept Tree\n\n");
    for section in sections {
        let _ = std::fmt::Write::write_fmt(&mut doc, format_args!("### {}\n\n", section.title));
        if !section.summary.trim().is_empty() {
            let _ = std::fmt::Write::write_fmt(&mut doc, format_args!("{}\n\n", section.summary));
        }
        for concept_slug in &section.concepts {
            if let Some(concept) = concepts
                .iter()
                .find(|concept| &concept.slug == concept_slug)
            {
                let _ = std::fmt::Write::write_fmt(
                    &mut doc,
                    format_args!(
                        "- [[{}|{}]] - {}\n",
                        concept_doc_stem(&concept.slug),
                        concept.title,
                        concept.summary
                    ),
                );
            }
        }
        doc.push('\n');
    }
    doc
}

#[allow(clippy::too_many_arguments)]
fn render_concept_page(
    concept: &ConceptModule,
    spans: &[SourceSpan],
    degraded_sources: &[String],
    lane: &'static str,
    flow: Option<&str>,
    module_lookup: &std::collections::BTreeMap<&str, &ModuleDoc>,
    file_lookup: &std::collections::BTreeMap<&str, &FileDoc>,
) -> String {
    let degraded_sources =
        combine_degraded_sources(degraded_sources, &concept.body_degraded_sources);
    // The nav structure pass runs Lane A, so the nav-level `lane` no longer
    // reflects how this page's *content* was generated. A curated content pass
    // that ran the Lane B tool loop always makes >=1 tool call (turn-0 tool use
    // is forced), while a Lane A one-shot makes none, so a non-zero tool-call
    // count is this page's own Lane B signal; otherwise fall back to the nav
    // lane (#993).
    let page_lane = if concept.body_observability.tool_call_count > 0 {
        LANE_TOOL_LOOP
    } else {
        lane
    };
    let lane_b = (page_lane == LANE_TOOL_LOOP).then_some(FrontmatterLaneB {
        lane: page_lane,
        tool_call_count: concept.body_observability.tool_call_count,
        turns: concept.body_observability.turns,
    });
    let mut doc = frontmatter_aggregate_with_verify_notes(
        &concept.title,
        "code_concept",
        spans,
        &degraded_sources,
        &concept.verify_notes,
        lane_b,
    );
    append_curated_source_files(&mut doc, spans, MAX_CURATED_SOURCE_FILE_LINKS);
    let _ = std::fmt::Write::write_fmt(&mut doc, format_args!("# {}\n\n", concept.title));
    append_curated_body(
        &mut doc,
        concept.body.as_deref(),
        "Overview",
        &ground_text(&concept.summary, spans, None),
    );
    if let Some(flow) = flow {
        doc.push_str(flow);
    }
    append_explore_section(
        &mut doc,
        &concept.modules,
        &concept.files,
        module_lookup,
        file_lookup,
    );
    doc
}

#[allow(clippy::too_many_arguments)]
fn render_narrative_page(
    page: &NarrativePage,
    spans: &[SourceSpan],
    concept_titles: &std::collections::BTreeMap<&str, &str>,
    degraded_sources: &[String],
    lane: &'static str,
    prev: Option<(&str, &str)>,
    next: Option<(&str, &str)>,
    flow: Option<&str>,
    module_lookup: &std::collections::BTreeMap<&str, &ModuleDoc>,
    file_lookup: &std::collections::BTreeMap<&str, &FileDoc>,
) -> String {
    let degraded_sources = combine_degraded_sources(degraded_sources, &page.body_degraded_sources);
    // The nav structure pass runs Lane A, so the nav-level `lane` no longer
    // reflects how this page's *content* was generated. A non-zero tool-call
    // count is this page's own Lane B signal (turn-0 tool use is forced);
    // otherwise fall back to the nav lane (#993).
    let page_lane = if page.body_observability.tool_call_count > 0 {
        LANE_TOOL_LOOP
    } else {
        lane
    };
    let lane_b = (page_lane == LANE_TOOL_LOOP).then_some(FrontmatterLaneB {
        lane: page_lane,
        tool_call_count: page.body_observability.tool_call_count,
        turns: page.body_observability.turns,
    });
    let mut doc = frontmatter_aggregate_with_verify_notes(
        &page.title,
        "code_narrative",
        spans,
        &degraded_sources,
        &page.verify_notes,
        lane_b,
    );
    append_curated_source_files(&mut doc, spans, MAX_CURATED_SOURCE_FILE_LINKS);
    let _ = std::fmt::Write::write_fmt(&mut doc, format_args!("# {}\n\n", page.title));
    append_curated_body(
        &mut doc,
        page.body.as_deref(),
        "Guide",
        &ground_text(&page.summary, spans, None),
    );
    if let Some(flow) = flow {
        doc.push_str(flow);
    }
    if !page.concepts.is_empty() {
        doc.push_str("## Concepts\n\n");
        for concept in &page.concepts {
            let title = concept_titles
                .get(concept.as_str())
                .copied()
                .unwrap_or(concept);
            let _ = std::fmt::Write::write_fmt(
                &mut doc,
                format_args!("- [[{}|{}]]\n", concept_doc_stem(concept), title),
            );
        }
        doc.push('\n');
    }
    append_explore_section(&mut doc, &page.modules, &[], module_lookup, file_lookup);
    curated_content::append_tour_nav(&mut doc, prev, next);
    doc
}

fn append_curated_body(
    doc: &mut String,
    body: Option<&str>,
    fallback_heading: &str,
    fallback_text: &str,
) {
    // The renderer already emitted the canonical `# {title}` H1, so drop a
    // duplicate H1 the model may have written at the top of its own body
    // (#905). If stripping leaves the body empty, fall back to the section.
    let body = body.map(strip_leading_model_h1);
    match body {
        Some(body) if !body.trim().is_empty() => {
            doc.push_str(body);
            if !body.ends_with('\n') {
                doc.push('\n');
            }
            doc.push('\n');
        }
        _ => write_section(doc, fallback_heading, fallback_text),
    }
}

/// Strip a single leading level-1 ATX heading from a model-authored curated
/// body.
///
/// Concept and narrative pages render a canonical `# {title}` H1 from the page
/// title (see [`render_concept_page`]/[`render_narrative_page`]); when the model
/// opens its body with its own top-level `# ...` the page shows two H1s (#905).
/// The renderer owns the title H1, so a leading H1 in the body is dropped. Only
/// a true level-1 heading (`#` followed by space/tab/end-of-line, with up to the
/// three leading spaces CommonMark allows) that is the body's first non-blank
/// line is removed; `##`+ subsections and bodies that do not open with a heading
/// are returned unchanged.
fn strip_leading_model_h1(body: &str) -> &str {
    // Ignore fully blank lines before the first content line, including lines
    // containing only spaces or tabs.
    let mut trimmed = body;
    while let Some(line_end) = trimmed.find('\n') {
        let line = trimmed[..line_end].trim_end_matches('\r');
        if !line.trim_matches([' ', '\t']).is_empty() {
            break;
        }
        trimmed = &trimmed[line_end + 1..];
    }
    let indent = trimmed.len() - trimmed.trim_start_matches(' ').len();
    if indent > 3 {
        return body;
    }
    let Some(after_hash) = trimmed[indent..].strip_prefix('#') else {
        return body;
    };
    let is_h1 = match after_hash.chars().next() {
        None => true,                                     // bare "#"
        Some('#') => false,                               // "##..." is level 2+
        Some(c) => matches!(c, ' ' | '\t' | '\n' | '\r'), // "# text"
    };
    if !is_h1 {
        return body;
    }
    // Drop the heading line, then any blank lines after it, so the body starts
    // at its first real paragraph.
    match trimmed.find('\n') {
        Some(newline) => trimmed[newline + 1..].trim_start_matches(['\n', '\r']),
        None => "",
    }
}

/// Bounded reference table for a curated page: module roots (not every member
/// file), capped at [`MAX_CURATED_KEY_COMPONENTS`], paired with their one-line
/// summaries. Files stay reachable and reciprocal via their parent module pages
/// and the reference appendix, so the old exhaustive `## Reference Modules`/
/// `## Source Files` down-link dumps - the missing_backlink source - collapse.
///
/// Rendered as a `Reference | Summary` Markdown table (via the shared table
/// helpers) so every curated page carries a deterministic, enumerable reference
/// table even when the model body omits one (#980). The wikilink keeps its
/// alias pipe; the table-cell writer escapes it to `\|`, which Obsidian renders
/// as an aliased link inside a table.
fn append_explore_section(
    doc: &mut String,
    modules: &[String],
    files: &[String],
    module_lookup: &std::collections::BTreeMap<&str, &ModuleDoc>,
    file_lookup: &std::collections::BTreeMap<&str, &FileDoc>,
) {
    let rows: Vec<(String, String)> = if !modules.is_empty() {
        modules
            .iter()
            .take(MAX_CURATED_KEY_COMPONENTS)
            .map(|module| {
                let summary = module_lookup
                    .get(module.as_str())
                    .map(|doc| doc.summary.as_str())
                    .unwrap_or_default();
                (module_wikilink(module), reference_summary_cell(summary))
            })
            .collect()
    } else {
        files
            .iter()
            .take(MAX_CURATED_KEY_COMPONENTS)
            .map(|file| {
                let summary = file_lookup
                    .get(file.as_str())
                    .map(|doc| doc.summary.as_str())
                    .unwrap_or_default();
                (file_wikilink(file), reference_summary_cell(summary))
            })
            .collect()
    };
    if rows.is_empty() {
        return;
    }
    doc.push_str("## Explore\n\n");
    write_markdown_table_header(doc, &["Reference", "Summary"]);
    for (reference, summary) in rows {
        write_markdown_table_row(doc, [reference, summary]);
    }
    doc.push('\n');
}

/// Summary cell for the Explore reference table: an em dash when no grounded
/// summary is available, so the column never renders empty. The table-cell
/// writer flattens any internal whitespace.
fn reference_summary_cell(summary: &str) -> String {
    let summary = summary.trim();
    if summary.is_empty() {
        "—".to_string()
    } else {
        summary.to_string()
    }
}

/// Union a narrative page's own modules/files with those of the concepts it
/// covers, so a chapter's content pass has real members to describe even when
/// the structure pass left its module/file lists sparse.
fn narrative_members(
    page: &NarrativePage,
    concepts: &[ConceptModule],
) -> (Vec<String>, Vec<String>) {
    let mut modules = page.modules.clone();
    let mut files = page.files.clone();
    for concept_slug in &page.concepts {
        if let Some(concept) = concepts
            .iter()
            .find(|concept| &concept.slug == concept_slug)
        {
            modules.extend(concept.modules.iter().cloned());
            files.extend(concept.files.iter().cloned());
        }
    }
    modules.sort();
    modules.dedup();
    files.sort();
    files.dedup();
    (modules, files)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn concept_page_records_its_own_content_lane_not_the_nav_lane() {
        let module_lookup = std::collections::BTreeMap::new();
        let file_lookup = std::collections::BTreeMap::new();
        let mut concept = ConceptModule {
            slug: "engine".to_string(),
            title: "Engine".to_string(),
            summary: "The core engine.".to_string(),
            modules: Vec::new(),
            files: Vec::new(),
            body: Some("## Purpose\n\nReal investigated body.\n".to_string()),
            body_degraded_sources: Vec::new(),
            verify_notes: Vec::new(),
            body_observability: GenerationObservability {
                tool_call_count: 3,
                turns: 4,
                ..GenerationObservability::default()
            },
        };

        // The nav structure pass is Lane A (one_shot), but this page's content
        // pass used the tool loop: the page must record its own tool_loop lane
        // and counts, not the nav lane (#993 observability regression).
        let doc = render_concept_page(
            &concept,
            &[],
            &[],
            LANE_ONE_SHOT,
            None,
            &module_lookup,
            &file_lookup,
        );
        assert!(doc.contains("lane: tool_loop"), "{doc}");
        assert!(doc.contains("tool_call_count: 3"), "{doc}");
        assert!(doc.contains("turns: 4"), "{doc}");

        // A content pass that made no tool calls is Lane A: no tool_loop lane.
        concept.body_observability.tool_call_count = 0;
        concept.body_observability.turns = 1;
        let doc_a = render_concept_page(
            &concept,
            &[],
            &[],
            LANE_ONE_SHOT,
            None,
            &module_lookup,
            &file_lookup,
        );
        assert!(!doc_a.contains("lane: tool_loop"), "{doc_a}");
    }

    #[test]
    fn strips_a_leading_model_h1() {
        assert_eq!(
            strip_leading_model_h1("# Introduction\n\nReal grounded prose.\n"),
            "Real grounded prose.\n"
        );
    }

    #[test]
    fn strips_a_leading_h1_after_blank_lines() {
        assert_eq!(strip_leading_model_h1("\n\n# Title\n\nBody.\n"), "Body.\n");
    }

    #[test]
    fn strips_a_leading_h1_after_whitespace_only_blank_lines() {
        assert_eq!(
            strip_leading_model_h1("  \n\t\n# Title\n\nBody.\n"),
            "Body.\n"
        );
    }

    #[test]
    fn keeps_a_body_without_a_leading_h1() {
        let body = "Real grounded prose.\n\n## Section\n";
        assert_eq!(strip_leading_model_h1(body), body);
    }

    #[test]
    fn leaves_subheadings_and_only_strips_the_first_h1() {
        // `##` is level 2+, not an H1, so a body that opens with one is untouched.
        let body = "## Overview\n\ntext\n";
        assert_eq!(strip_leading_model_h1(body), body);
        // Only the first top-level H1 is removed; a later one survives.
        assert_eq!(
            strip_leading_model_h1("# Title\n\nintro\n\n# Later\n"),
            "intro\n\n# Later\n"
        );
    }

    #[test]
    fn append_curated_body_drops_the_duplicate_h1() {
        let mut doc = String::from("# Introduction\n\n");
        append_curated_body(
            &mut doc,
            Some("# Introduction\n\nGrounded narrative.\n"),
            "Guide",
            "fallback",
        );
        // The model body repeated the page title; only the renderer's H1 survives.
        assert_eq!(doc.matches("# Introduction").count(), 1);
        assert!(doc.contains("Grounded narrative."));
        assert!(!doc.contains("fallback"));
    }

    #[test]
    fn append_curated_body_falls_back_when_body_is_only_a_heading() {
        let mut doc = String::new();
        append_curated_body(
            &mut doc,
            Some("# Only A Title\n"),
            "Guide",
            "Fallback text.",
        );
        // Stripping the lone heading leaves nothing, so the fallback section renders.
        assert!(doc.contains("Guide"));
        assert!(doc.contains("Fallback text."));
        assert!(!doc.contains("# Only A Title"));
    }
}
