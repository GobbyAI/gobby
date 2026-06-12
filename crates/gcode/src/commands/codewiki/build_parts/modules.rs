use super::super::*;
use std::collections::{BTreeMap, BTreeSet};

#[allow(clippy::too_many_arguments)]
pub(crate) fn build_module_docs(
    files: &[FileDoc],
    graph_edges: &[CodewikiGraphEdge],
    graph_availability: CodewikiGraphAvailability,
    leading_chunks: &BTreeMap<String, LeadingChunk>,
    generate: &mut Option<&mut TextGenerator<'_>>,
    reuse: &mut Option<&mut ReusePlan>,
    progress: &mut CodewikiProgress,
    emit: &mut dyn FnMut(&ModuleDoc) -> anyhow::Result<()>,
) -> anyhow::Result<Vec<ModuleDoc>> {
    let mut module_names = BTreeSet::new();
    for file in files {
        for module in module_ancestors(&file.module) {
            module_names.insert(module);
        }
        for module in module_ancestors(&module_for_file(&file.path)) {
            module_names.insert(module);
        }
    }

    let mut module_summaries: BTreeMap<String, String> = BTreeMap::new();
    let mut module_sources: BTreeMap<String, Vec<SourceSpan>> = BTreeMap::new();
    let mut modules = module_names.into_iter().collect::<Vec<_>>();
    modules.sort_by_key(|module| std::cmp::Reverse(module_depth(module)));

    let mut docs = Vec::new();
    let module_total = modules.len();
    for (index, module) in modules.into_iter().enumerate() {
        let mut seen_direct_files = BTreeSet::new();
        let direct_files = files
            .iter()
            .filter(|file| {
                file_is_direct_module_member(file, &module)
                    && seen_direct_files.insert(file.path.clone())
            })
            .map(|file| FileLink {
                path: file.path.clone(),
                summary: file.summary.clone(),
                source_spans: file.source_spans.clone(),
            })
            .collect::<Vec<_>>();
        let child_modules = direct_child_modules(&module, module_summaries.keys())
            .into_iter()
            .map(|child| ModuleLink {
                summary: module_summaries.get(&child).cloned().unwrap_or_default(),
                source_spans: module_sources.get(&child).cloned().unwrap_or_default(),
                module: child,
            })
            .collect::<Vec<_>>();
        let file_summaries = direct_files
            .iter()
            .map(|file| prompts::ChildSummary {
                name: file.path.clone(),
                summary: file.summary.clone(),
            })
            .collect::<Vec<_>>();
        let child_summaries = child_modules
            .iter()
            .map(|module| prompts::ChildSummary {
                name: module.module.clone(),
                summary: module.summary.clone(),
            })
            .collect::<Vec<_>>();
        let component_ids = direct_component_ids_for_module(files, &module);
        let prompt_component_ids = prompt_component_ids_for_module(files, &module);
        let dependency_diagram = render_module_dependency_mermaid(&module, files, graph_edges);
        let call_diagram = render_module_call_mermaid(&module, files, graph_edges);
        let fallback = structural_module_summary(&module, &direct_files, &child_modules);
        let source_spans = collect_link_spans(&direct_files, &child_modules);
        // A module's provenance rolls up every file under it (child spans
        // included), so unchanged hashes mean the prompt inputs that hashes
        // can see are unchanged and the recorded summary can stand in for a
        // fresh LLM call.
        let reused = reuse.as_deref_mut().and_then(|plan| {
            plan.reusable_page_with_summary(&module_doc_path(&module), &span_files(&source_spans))
        });
        progress.emit(format!(
            "{} module doc module {}/{} {}",
            if reused.is_some() {
                "reusing"
            } else {
                "generating"
            },
            index + 1,
            module_total,
            module
        ));
        let mut degraded = false;
        let (summary, reused_page) = match reused {
            Some((page, summary)) => (summary, Some(page)),
            None => {
                // Real retrieved input alongside summaries-of-summaries: the
                // module's busiest member files contribute leading source
                // excerpts to the brief.
                let sources = ranked_source_excerpts(
                    files
                        .iter()
                        .filter(|file| file_is_direct_module_member(file, &module)),
                    leading_chunks,
                    prompts::MAX_PROMPT_SOURCE_EXCERPTS,
                );
                let generated = maybe_generate(
                    generate,
                    &prompts::module_prompt(
                        &module,
                        &file_summaries,
                        &child_summaries,
                        &prompt_component_ids,
                        &sources,
                    ),
                    prompts::MODULE_SYSTEM,
                    PromptTier::Aggregate,
                )
                .unwrap_or_record(fallback, &mut degraded);
                let citations = citation_list(&source_spans, &generated);
                let summary = ground_text(&generated, &source_spans, Some(&citations));
                (summary, None)
            }
        };

        module_summaries.insert(module.clone(), summary.clone());
        module_sources.insert(module.clone(), source_spans.clone());
        let doc = ModuleDoc {
            module,
            summary,
            source_spans,
            direct_files,
            child_modules,
            component_ids,
            dependency_diagram,
            call_diagram,
            graph_availability,
            degraded,
            reused_page,
        };
        emit(&doc)?;
        docs.push(doc);
    }

    docs.sort_by(|a, b| a.module.cmp(&b.module));
    Ok(docs)
}

fn direct_component_ids_for_module(files: &[FileDoc], module: &str) -> Vec<String> {
    let mut seen = BTreeSet::new();
    files
        .iter()
        .filter(|file| file_is_direct_module_member(file, module))
        .flat_map(|file| &file.symbols)
        .filter_map(|symbol| {
            let component_id = symbol.component_id.clone();
            seen.insert(component_id.clone()).then_some(component_id)
        })
        .collect()
}

fn file_is_direct_module_member(file: &FileDoc, module: &str) -> bool {
    file.module == module || module_for_file(&file.path) == module
}

pub(super) fn prompt_component_ids_for_module(files: &[FileDoc], module: &str) -> Vec<String> {
    files
        .iter()
        .filter(|file| file.module == module || module_is_ancestor(module, &file.module))
        .flat_map(|file| {
            file.symbols
                .iter()
                .map(|symbol| format!("{} ({})", symbol.component_label, symbol.component_id))
        })
        .collect()
}
