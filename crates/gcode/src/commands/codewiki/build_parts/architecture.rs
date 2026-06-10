use super::super::*;
use super::modules::prompt_component_ids_for_module;
use std::collections::{BTreeMap, BTreeSet, VecDeque};

pub(crate) fn build_architecture_doc(
    files: &[FileDoc],
    modules: &[ModuleDoc],
    graph_edges: &[CodewikiGraphEdge],
    graph_availability: CodewikiGraphAvailability,
    generate: &mut Option<&mut TextGenerator<'_>>,
    progress: &mut CodewikiProgress,
) -> ArchitectureDoc {
    let subsystem_names = files
        .iter()
        .map(|file| file.module.clone())
        .collect::<BTreeSet<_>>();
    let mut degraded_sources = BTreeSet::new();
    match graph_availability {
        CodewikiGraphAvailability::Available => {}
        CodewikiGraphAvailability::Truncated => {
            degraded_sources.insert("graph-truncated".to_string());
        }
        CodewikiGraphAvailability::Unavailable => {
            degraded_sources.insert("graph-unavailable".to_string());
        }
    }

    let mut subsystems = Vec::new();
    let subsystem_modules = modules
        .iter()
        .filter(|module| subsystem_names.contains(&module.module))
        .collect::<Vec<_>>();
    let subsystem_total = subsystem_modules.len();
    for (index, module) in subsystem_modules.into_iter().enumerate() {
        let file_summaries = module
            .direct_files
            .iter()
            .map(|file| prompts::ChildSummary {
                name: file.path.clone(),
                summary: file.summary.clone(),
            })
            .collect::<Vec<_>>();
        let child_summaries = module
            .child_modules
            .iter()
            .map(|module| prompts::ChildSummary {
                name: module.module.clone(),
                summary: module.summary.clone(),
            })
            .collect::<Vec<_>>();
        let fallback =
            structural_module_summary(&module.module, &module.direct_files, &module.child_modules);
        let source_spans = collect_link_spans(&module.direct_files, &module.child_modules);
        let prompt_component_ids = prompt_component_ids_for_module(files, &module.module);
        progress.emit(format!(
            "generating architecture doc subsystem {}/{} {}",
            index + 1,
            subsystem_total,
            module.module
        ));
        let generated = maybe_generate(
            generate,
            &prompts::architecture_prompt(
                &module.module,
                &file_summaries,
                &child_summaries,
                &prompt_component_ids,
            ),
            prompts::ARCHITECTURE_SYSTEM,
        );
        let responsibility = match generated {
            Some(generated) => ground_text(
                &generated,
                &source_spans,
                Some(&citation_list(&source_spans)),
            ),
            None => {
                degraded_sources.insert("model-unavailable".to_string());
                ground_text(&fallback, &source_spans, None)
            }
        };

        subsystems.push(ArchitectureSubsystem {
            module: module.module.clone(),
            responsibility,
            source_spans,
        });
    }

    let dependency_diagram = match graph_availability {
        CodewikiGraphAvailability::Unavailable => None,
        CodewikiGraphAvailability::Available | CodewikiGraphAvailability::Truncated => {
            render_architecture_dependency_mermaid(files, graph_edges)
        }
    };
    let source_spans = subsystems
        .iter()
        .flat_map(|subsystem| subsystem.source_spans.iter().cloned())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();

    ArchitectureDoc {
        source_spans,
        subsystems,
        dependency_diagram,
        degraded_sources: degraded_sources.into_iter().collect(),
    }
}

pub(super) fn module_dependency_edges(
    graph_edges: &[CodewikiGraphEdge],
    component_modules: &BTreeMap<String, String>,
    module_names: &BTreeSet<String>,
) -> BTreeSet<(String, String)> {
    graph_edges
        .iter()
        .filter(|edge| edge.kind == CodewikiGraphEdgeKind::Import)
        .filter_map(|edge| {
            let source = component_modules.get(&edge.source_component_id)?;
            let target = component_modules.get(&edge.target_component_id)?;
            (source != target && module_names.contains(source) && module_names.contains(target))
                .then(|| (source.clone(), target.clone()))
        })
        .collect()
}

/// Assigns deterministic topological ranks for module dependency ordering.
pub(super) fn dependency_topology(
    module_names: &BTreeSet<String>,
    module_edges: &BTreeSet<(String, String)>,
) -> BTreeMap<String, usize> {
    let mut dependency_count = module_names
        .iter()
        .map(|module| (module.clone(), 0usize))
        .collect::<BTreeMap<_, _>>();
    let mut dependents = module_names
        .iter()
        .map(|module| (module.clone(), BTreeSet::<String>::new()))
        .collect::<BTreeMap<_, _>>();

    for (source, target) in module_edges {
        if let Some(count) = dependency_count.get_mut(source) {
            *count += 1;
        }
        dependents
            .entry(target.clone())
            .or_default()
            .insert(source.clone());
    }

    let mut ready = dependency_count
        .iter()
        .filter_map(|(module, count)| (*count == 0).then_some(module.clone()))
        .collect::<VecDeque<_>>();
    let mut rank = BTreeMap::new();
    while let Some(module) = ready.pop_front() {
        if rank.contains_key(&module) {
            continue;
        }
        rank.insert(module.clone(), rank.len());
        for dependent in dependents.get(&module).into_iter().flatten() {
            let Some(count) = dependency_count.get_mut(dependent) else {
                continue;
            };
            *count = count.saturating_sub(1);
            if *count == 0 {
                ready.push_back(dependent.clone());
            }
        }
    }

    for module in module_names {
        if !rank.contains_key(module) {
            rank.insert(module.clone(), rank.len());
        }
    }
    rank
}
