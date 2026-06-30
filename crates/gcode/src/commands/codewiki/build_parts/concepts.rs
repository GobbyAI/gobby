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
/// How many independent Lane A generations to try for the curated navigation
/// plan before falling back to the deterministic taxonomy. The structure
/// synthesis is a single large JSON emission from a weak local model and is
/// flaky run-to-run; a couple of retries recover a real AI taxonomy instead of
/// degrading the whole curated layer on a one-off malformed emission (#993).
const CURATED_NAV_PLAN_MAX_ATTEMPTS: usize = 3;

/// Diagnostic-only dump of a curated navigation plan that never parsed across
/// all attempts. When `GOBBY_CODEWIKI_LANE_B_DUMP_DIR` is set, write the nav
/// prompt and the last raw model output to `<dir>/curated_navigation_plan.dump.md`
/// so a persistent parse failure can be reproduced offline. Off by default.
fn maybe_dump_nav_failure(prompt: &str, raw: &str) {
    let Ok(dir) = std::env::var("GOBBY_CODEWIKI_LANE_B_DUMP_DIR") else {
        return;
    };
    if dir.trim().is_empty() {
        return;
    }
    let path = std::path::Path::new(&dir).join("curated_navigation_plan.dump.md");
    let dump = format!(
        "# Curated navigation plan: unparseable after {CURATED_NAV_PLAN_MAX_ATTEMPTS} attempts\n\n\
         - raw_bytes: {}\n\n## NAV PROMPT\n\n{prompt}\n\n## LAST RAW OUTPUT\n\n{raw}\n",
        raw.len(),
    );
    if let Err(err) = std::fs::create_dir_all(&dir).and_then(|()| std::fs::write(&path, dump)) {
        eprintln!("warning: failed to write nav-plan failure dump to {path:?}: {err}");
    }
}

pub(crate) fn build_curated_navigation_docs(
    files: &[FileDoc],
    modules: &[ModuleDoc],
    leading_chunks: &std::collections::BTreeMap<String, LeadingChunk>,
    generate: &mut Option<&mut TextGenerator<'_>>,
    verify: &mut Option<&mut TextVerifier<'_>>,
    reuse: &mut Option<&mut ReusePlan>,
    progress: &mut CodewikiProgress,
) -> anyhow::Result<Vec<BuiltDoc>> {
    let all_spans = all_input_spans(files, modules);
    let all_sources = span_files(&all_spans);
    if let Some(reused_docs) = reuse.as_deref_mut().and_then(|plan| {
        let lane_a_ai_outcome = plan.ai_outcome();
        plan.reusable_page_with_ai_outcome(
            "code/concepts/index.md",
            &all_sources,
            lane_a_ai_outcome,
        )?;
        plan.reusable_pages_with_prefixes_by_ai_outcome(
            &["code/concepts/", "code/narrative/"],
            // Concept/narrative bodies are Lane A one-shot (gobby-cli #1001),
            // like the nav index, so all curated pages reuse on the Lane A
            // outcome.
            |_path| lane_a_ai_outcome,
        )
    }) {
        progress.emit("reusing curated navigation docs (sources unchanged)");
        return Ok(reused_docs);
    }

    progress.emit("generating curated navigation docs");
    let mut degraded_sources = Vec::new();
    // The curated navigation taxonomy is a one-shot structure synthesis: it
    // clusters the already-supplied module/file summaries into a handbook plan
    // and has no per-claim source to investigate, so it runs Lane A even when a
    // tool-loop is configured. Forcing it through the Lane B tool loop made a
    // weak function-calling model investigate needlessly for minutes and then
    // emit JSON corrupted by that exploration, which failed to parse (#993).
    // Lane B grounding stays where it earns its cost — the curated narrative and
    // concept PROSE pages rendered below. On a Lane A parse failure the
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
                        "Lane B curated navigation plan was unparseable; \
                         no deterministic fallback (no skeleton)"
                    ));
                }
                // Otherwise retry: a fresh Lane A generation usually parses.
                last_unparseable = Some(generated);
            }
            // A Lane B failure already returned `Err`; these are Lane A paths.
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
                maybe_dump_nav_failure(&nav_prompt, raw);
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
        generate,
        verify,
    )
}
