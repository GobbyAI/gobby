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
/// ten-chapter handbook spine, so a verbose structure response
/// cannot crowd out the canonical guided tour.
const MAX_EXTRA_NARRATIVE_PAGES: usize = 2;

pub(crate) fn build_curated_navigation_docs(
    files: &[FileDoc],
    modules: &[ModuleDoc],
    leading_chunks: &std::collections::BTreeMap<String, LeadingChunk>,
    generate: &mut Option<&mut TextGenerator<'_>>,
    verify: &mut Option<&mut TextVerifier<'_>>,
    reuse: &mut Option<&mut ReusePlan>,
    progress: &mut CodewikiProgress,
) -> Vec<BuiltDoc> {
    let all_spans = all_input_spans(files, modules);
    let all_sources = span_files(&all_spans);
    if let Some(reused_docs) = reuse.as_deref_mut().and_then(|plan| {
        plan.reusable_page("code/concepts/index.md", &all_sources)?;
        plan.reusable_pages_with_prefixes(&["code/concepts/", "code/narrative/"])
    }) {
        progress.emit("reusing curated navigation docs (sources unchanged)");
        return reused_docs;
    }

    progress.emit("generating curated navigation docs");
    let mut degraded = false;
    let plan = match maybe_generate(
        generate,
        &curated_navigation_prompt(files, modules),
        prompts::CURATED_NAVIGATION_SYSTEM,
        PromptTier::Aggregate,
    ) {
        Generation::Generated(generated) => match parse_plan(&generated) {
            Some(plan) => plan,
            None => {
                degraded = true;
                fallback_plan(files, modules)
            }
        },
        Generation::Failed => {
            degraded = true;
            fallback_plan(files, modules)
        }
        Generation::Skipped => fallback_plan(files, modules),
    };

    render_curated_navigation_docs(
        files,
        modules,
        plan,
        degraded,
        leading_chunks,
        generate,
        verify,
    )
}
