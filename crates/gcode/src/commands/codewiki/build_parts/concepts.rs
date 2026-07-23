use super::super::*;

#[path = "concepts/plan.rs"]
mod plan;
#[path = "concepts/render.rs"]
mod render;
#[path = "concepts/spans.rs"]
mod spans;
#[path = "concepts/support.rs"]
mod support;
#[path = "concepts/types.rs"]
mod types;

pub(crate) use plan::default_chapter_links;

use plan::{curated_navigation_prompt, fallback_plan, parse_plan};
use render::render_curated_navigation_docs;
use spans::all_input_spans;

const MAX_CONCEPT_MODULES: usize = 12;
const MAX_CONCEPT_LINKS: usize = 6;
/// Cap on the bounded "Explore" reference links a curated page emits (module
/// roots, not every member file). Replaces the old exhaustive
/// `## Reference Modules`/`## Source Files` dumps so the curated->reference
/// down-link surface (the missing_backlink source) collapses (root cause 6;
/// also the #853D mechanism).
const MAX_CURATED_KEY_COMPONENTS: usize = 8;
/// Cap on the bounded "Relevant source files" links a curated page lists (no
/// per-range expansion). Curated pages keep a small provenance footprint;
/// reference pages keep the full range-complete block.
const MAX_CURATED_SOURCE_FILE_LINKS: usize = 8;
/// Cap on *extra* model-supplied narrative chapters beyond the required
/// nine-chapter handbook spine, so a verbose structure response
/// cannot crowd out the canonical guided tour.
const MAX_EXTRA_NARRATIVE_PAGES: usize = 2;
/// How many independent one-shot generations to try for the curated navigation
/// plan before falling back to the deterministic taxonomy. The structure
/// synthesis is a single large JSON emission from a weak local model and is
/// flaky run-to-run; a couple of retries recover a real AI taxonomy instead of
/// degrading the whole curated layer on a one-off malformed emission (#993).
const CURATED_NAV_PLAN_MAX_ATTEMPTS: usize = 3;

/// Diagnostic dump of a curated navigation plan that never parsed across all
/// attempts: write the nav prompt and the last raw model output to
/// `<dump_dir>/curated_navigation_plan.dump.md` so a persistent parse failure
/// can be reproduced offline. `dump_dir` comes from
/// [`super::curated_content::resolve_tool_loop_dump_dir`] via the run options;
/// `None` (tests, library callers) is a no-op.
fn maybe_dump_nav_failure(dump_dir: Option<&std::path::Path>, prompt: &str, raw: &str) {
    let Some(dir) = dump_dir else {
        return;
    };
    let path = dir.join("curated_navigation_plan.dump.md");
    let dump = format!(
        "# Curated navigation plan: unparseable after {CURATED_NAV_PLAN_MAX_ATTEMPTS} attempts\n\n\
         - raw_bytes: {}\n\n## NAV PROMPT\n\n{prompt}\n\n## LAST RAW OUTPUT\n\n{raw}\n",
        raw.len(),
    );
    if let Err(err) = std::fs::create_dir_all(dir).and_then(|()| std::fs::write(&path, dump)) {
        eprintln!("warning: failed to write nav-plan failure dump to {path:?}: {err}");
    }
}

/// True when every nav-internal wikilink (`code/concepts/…`,
/// `code/narrative/…`) in the docs resolves to a doc within the set itself.
/// The nav set is planned atomically, so a link escaping the set means the
/// on-disk pages were written by different plans (#18328); an unparseable
/// link is treated the same way. Links to other namespaces (module and file
/// reference pages) are validated by publication, not here.
fn nav_set_internally_consistent(docs: &[BuiltDoc]) -> bool {
    let paths = docs
        .iter()
        .map(|doc| doc.path.as_str())
        .collect::<std::collections::BTreeSet<_>>();
    docs.iter().all(|doc| {
        code_wikilinks(&doc.content).is_ok_and(|targets| {
            targets
                .iter()
                .filter(|target| {
                    target.starts_with("code/concepts/") || target.starts_with("code/narrative/")
                })
                .all(|target| paths.contains(target.as_str()))
        })
    })
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn build_curated_navigation_docs(
    files: &[FileDoc],
    modules: &[ModuleDoc],
    leading_chunks: &std::collections::BTreeMap<String, LeadingChunk>,
    graph_edges: &[CodewikiGraphEdge],
    tool_loop_dump_dir: Option<&std::path::Path>,
    generate: &mut Option<&mut TextGenerator<'_>>,
    verify: &mut Option<&mut TextVerifier<'_>>,
    reuse: &mut Option<&mut ReusePlan>,
    diagram_stats: &mut DiagramStats,
    progress: &mut CodewikiProgress,
) -> anyhow::Result<Vec<BuiltDoc>> {
    let all_spans = all_input_spans(files, modules);
    let all_sources = span_files(&all_spans);
    if let Some(reused_docs) = reuse.as_deref_mut().and_then(|plan| {
        let one_shot_ai_outcome = plan.ai_outcome();
        plan.reusable_page_with_ai_outcome(
            "code/concepts/index.md",
            &all_sources,
            one_shot_ai_outcome,
        )?;
        plan.reusable_pages_with_prefixes_by_ai_outcome(
            &["code/concepts/", "code/narrative/"],
            // Concept/narrative bodies use one-shot generation (gobby-cli #1001),
            // like the nav index, so all curated pages reuse on the one-shot
            // outcome.
            |_path| one_shot_ai_outcome,
        )
    }) {
        // The manifest tracks files, not links: a stage skewed by a prior
        // run's per-doc skips is manifest-consistent while its index still
        // references slugs that plan dropped (#18328). Reuse only a set whose
        // nav-internal links resolve within the set itself; otherwise fall
        // through to fresh generation, which heals the skew.
        if nav_set_internally_consistent(&reused_docs) {
            progress.emit("reusing curated navigation docs (sources unchanged)");
            return Ok(reused_docs);
        }
        progress.emit(
            "regenerating curated navigation docs (reused set links do not resolve within the set)",
        );
    }

    progress.emit("generating curated navigation docs");
    let mut degraded_sources = Vec::new();
    // The curated navigation taxonomy is a one-shot structure synthesis: it
    // clusters the already-supplied module/file summaries into a handbook plan
    // and has no per-claim source to investigate, so it runs one-shot even when a
    // tool loop is configured. Forcing it through the tool loop made a
    // weak function-calling model investigate needlessly for minutes and then
    // emit JSON corrupted by that exploration, which failed to parse (#993).
    // Tool-loop grounding stays where it earns its cost — the curated narrative and
    // concept PROSE pages rendered below. On a one-shot parse failure the
    // deterministic fallback taxonomy applies, as before.
    // The nav structure pass is a one-shot JSON synthesis on a weak local model,
    // and it is nondeterministic: the same prompt parses cleanly on one run and
    // emits truncated or garbled JSON on the next, which then falls back to the
    // structural taxonomy and degrades the entire curated layer (#993). Retry a
    // bounded number of independent generations so a flaky parse failure
    // recovers a real AI taxonomy before the deterministic fallback applies; a
    // genuine model failure (Failed/Skipped) still falls back immediately.
    let nav_prompt = curated_navigation_prompt(files, modules);
    let mut lane = LANE_ONE_SHOT;
    let mut plan_observability = GenerationObservability::default();
    let mut parsed_plan = None;
    let mut last_unparseable: Option<String> = None;
    for _ in 0..CURATED_NAV_PLAN_MAX_ATTEMPTS {
        let aggregate = generate_aggregate(
            &mut None,
            generate,
            &nav_prompt,
            prompts::CURATED_NAVIGATION_SYSTEM,
            "curated navigation plan",
        )?;
        lane = aggregate.lane;
        plan_observability = aggregate.observability.clone();
        // Data-source degradation reflects backend availability, identical
        // across attempts, so replace rather than accumulate it.
        degraded_sources = aggregate.data_source_degraded;
        match aggregate.content {
            GenerationContent::Generated(generated) => {
                if let Some(plan) = parse_plan(&generated) {
                    parsed_plan = Some(plan);
                    break;
                }
                if lane == LANE_TOOL_LOOP {
                    return Err(anyhow::anyhow!(
                        "Tool-loop curated navigation plan was unparseable; \
                         no deterministic fallback (no skeleton)"
                    ));
                }
                // Otherwise retry: a fresh one-shot generation usually parses.
                last_unparseable = Some(generated);
            }
            // A tool-loop failure already returned `Err`; these are one-shot paths.
            GenerationContent::Failed(cause) => {
                degraded_sources.push(cause.reason_code().to_string());
                break;
            }
            GenerationContent::Skipped => break,
        }
    }
    let plan = match parsed_plan {
        Some(plan) => plan,
        None => {
            // Every attempt produced unparseable JSON (or generation failed):
            // capture the last raw output for offline diagnosis, mark the layer
            // degraded, and fall back to the deterministic taxonomy.
            if let Some(raw) = &last_unparseable {
                maybe_dump_nav_failure(tool_loop_dump_dir, &nav_prompt, raw);
                degraded_sources.push("grounding-empty".to_string());
            }
            fallback_plan(files, modules)
        }
    };

    render_curated_navigation_docs(
        files,
        modules,
        plan,
        degraded_sources,
        lane,
        &plan_observability,
        leading_chunks,
        graph_edges,
        tool_loop_dump_dir,
        generate,
        verify,
        diagram_stats,
        progress,
    )
}
