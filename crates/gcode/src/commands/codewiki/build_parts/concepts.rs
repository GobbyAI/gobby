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
const MAX_CURATED_SOURCE_FILE_LINKS: usize = 12;
/// Cap on *extra* model-supplied narrative chapters beyond the required
/// introduction/architecture/data-flow spine, so a verbose structure response
/// cannot crowd out the canonical guided tour.
const MAX_EXTRA_NARRATIVE_PAGES: usize = 4;

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

#[cfg(test)]
mod tests {
    use super::super::super::*;
    use super::plan::largest_member_module;
    use super::render::append_dependency_diagram;

    fn module_with_diagram(
        name: &str,
        diagram: Option<&str>,
        availability: CodewikiGraphAvailability,
    ) -> ModuleDoc {
        ModuleDoc {
            module: name.to_string(),
            summary: String::new(),
            source_spans: Vec::new(),
            direct_files: Vec::new(),
            child_modules: Vec::new(),
            dependency_diagram: diagram.map(str::to_string),
            call_diagram: None,
            graph_availability: availability,
            degraded: false,
            reused_page: None,
        }
    }

    #[test]
    fn curated_diagram_injected_when_graph_available() {
        let module = module_with_diagram(
            "src",
            Some("```mermaid\ngraph TD\n```"),
            CodewikiGraphAvailability::Available,
        );
        let mut doc = String::new();
        append_dependency_diagram(&mut doc, Some(&module));
        assert!(doc.contains("## Architecture diagram"), "{doc}");
        assert!(doc.contains("```mermaid"), "{doc}");
        assert!(!doc.contains("graph-truncated"), "{doc}");
    }

    #[test]
    fn curated_diagram_marks_truncated_graph() {
        let module = module_with_diagram(
            "src",
            Some("```mermaid\ngraph TD\n```"),
            CodewikiGraphAvailability::Truncated,
        );
        let mut doc = String::new();
        append_dependency_diagram(&mut doc, Some(&module));
        assert!(doc.contains("`degraded: graph-truncated`"), "{doc}");
        assert!(doc.contains("```mermaid"), "{doc}");
    }

    #[test]
    fn curated_diagram_skipped_when_unavailable_or_absent() {
        let mut doc = String::new();
        append_dependency_diagram(&mut doc, None);
        let unavailable = module_with_diagram(
            "src",
            Some("```mermaid\n```"),
            CodewikiGraphAvailability::Unavailable,
        );
        append_dependency_diagram(&mut doc, Some(&unavailable));
        let no_diagram = module_with_diagram("src", None, CodewikiGraphAvailability::Available);
        append_dependency_diagram(&mut doc, Some(&no_diagram));
        assert!(doc.is_empty(), "{doc}");
    }

    #[test]
    fn largest_member_module_requires_a_diagram() {
        let with = module_with_diagram(
            "src/a",
            Some("```mermaid\n```"),
            CodewikiGraphAvailability::Available,
        );
        let without = module_with_diagram("src/b", None, CodewikiGraphAvailability::Available);
        let lookup = std::collections::BTreeMap::from([("src/a", &with), ("src/b", &without)]);
        let members = ["src/a".to_string(), "src/b".to_string()];
        assert_eq!(
            largest_member_module(&members, &lookup).map(|module| module.module.as_str()),
            Some("src/a")
        );
        assert!(largest_member_module(&["src/b".to_string()], &lookup).is_none());
    }
}
