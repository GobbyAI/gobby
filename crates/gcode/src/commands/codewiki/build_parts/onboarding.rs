use super::super::*;
use super::architecture::{dependency_topology, module_dependency_edges};
use crate::models::Symbol;
use gobby_core::graph_analytics::{self, AnalyticsEdge, AnalyticsGraph, AnalyticsNode};
use std::collections::{BTreeMap, BTreeSet};

pub(crate) fn build_onboarding_doc(
    files: &[FileDoc],
    modules: &[ModuleDoc],
    graph_edges: &[CodewikiGraphEdge],
    graph_availability: CodewikiGraphAvailability,
) -> OnboardingDoc {
    let entry_points = onboarding_entry_points(files);
    let mut degraded_sources = BTreeSet::new();
    let mut fallback_to_unavailable = false;
    let reading_order = match graph_availability {
        CodewikiGraphAvailability::Unavailable => {
            degraded_sources.insert("graph-analytics-unavailable".to_string());
            Vec::new()
        }
        CodewikiGraphAvailability::Truncated => {
            degraded_sources.insert("graph-truncated".to_string());
            fallback_to_unavailable = true;
            ranked_onboarding_steps(files, modules, graph_edges)
        }
        CodewikiGraphAvailability::Available => {
            ranked_onboarding_steps(files, modules, graph_edges)
        }
    };
    if fallback_to_unavailable && reading_order.is_empty() {
        degraded_sources.insert("graph-analytics-unavailable".to_string());
    }

    let source_spans = entry_points
        .iter()
        .map(|entry| entry.source_span.clone())
        .chain(
            reading_order
                .iter()
                .flat_map(|step| step_source_spans(&step.module, modules)),
        )
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();

    OnboardingDoc {
        source_spans,
        entry_points,
        reading_order,
        degraded_sources: degraded_sources.into_iter().collect(),
    }
}

fn onboarding_entry_points(files: &[FileDoc]) -> Vec<OnboardingEntryPoint> {
    let mut entries = Vec::new();
    let mut seen = BTreeSet::new();

    for file in files {
        if is_rust_entry_file(&file.path) {
            let Some(source_span) = file.source_spans.first().cloned().or_else(|| {
                file.symbols
                    .first()
                    .map(|symbol| symbol.source_span.clone())
            }) else {
                continue;
            };
            let description = if file.path.ends_with("main.rs") {
                "Binary entry file".to_string()
            } else {
                "Library entry file".to_string()
            };
            let key = format!("file:{}", file.path);
            if seen.insert(key) {
                entries.push(OnboardingEntryPoint {
                    link: file_wikilink(&file.path),
                    description,
                    source_span,
                });
            }
        }
    }

    for file in files {
        for symbol in &file.symbols {
            if !is_public_api_symbol(&symbol.symbol) {
                continue;
            }
            let signature = symbol
                .symbol
                .signature
                .as_deref()
                .unwrap_or(&symbol.symbol.qualified_name);
            let key = format!("symbol:{}", symbol.symbol.id);
            if seen.insert(key) {
                entries.push(OnboardingEntryPoint {
                    link: file_wikilink(&file.path),
                    description: format!(
                        "{} public API {}",
                        symbol.symbol.qualified_name,
                        inline_code(signature)
                    ),
                    source_span: symbol.source_span.clone(),
                });
            }
        }
    }

    entries
}

fn ranked_onboarding_steps(
    files: &[FileDoc],
    modules: &[ModuleDoc],
    graph_edges: &[CodewikiGraphEdge],
) -> Vec<OnboardingStep> {
    let module_names = modules
        .iter()
        .filter(|module| !module.direct_files.is_empty())
        .map(|module| module.module.clone())
        .collect::<BTreeSet<_>>();
    if module_names.is_empty() {
        return Vec::new();
    }

    let component_modules = files
        .iter()
        .flat_map(|file| {
            file.symbols
                .iter()
                .map(|symbol| (symbol.component_id.clone(), file.module.clone()))
        })
        .collect::<BTreeMap<_, _>>();
    let module_edges = module_dependency_edges(graph_edges, &component_modules, &module_names);
    if module_edges.is_empty() {
        return Vec::new();
    }

    let topology = dependency_topology(&module_names, &module_edges);
    let graph = AnalyticsGraph {
        nodes: module_names
            .iter()
            .map(|module| AnalyticsNode {
                id: module.clone(),
                kind: "module".to_string(),
                weight: topology.get(module).copied().unwrap_or_default() as f64,
            })
            .collect(),
        edges: module_edges
            .iter()
            .map(|(source, target)| AnalyticsEdge {
                source: source.clone(),
                target: target.clone(),
                kind: "imports".to_string(),
            })
            .collect(),
    };
    let analytics = graph_analytics::analyze(&graph);
    let module_summaries = modules
        .iter()
        .map(|module| (module.module.clone(), module.summary.clone()))
        .collect::<BTreeMap<_, _>>();
    let centrality = analytics
        .centrality
        .into_iter()
        .map(|score| (score.node.id, (score.degree, score.score)))
        .collect::<BTreeMap<_, _>>();

    let mut steps = module_names
        .into_iter()
        .filter_map(|module| {
            let (degree, score) = centrality.get(&module).copied().unwrap_or_default();
            (degree > 0).then(|| OnboardingStep {
                summary: module_summaries.get(&module).cloned().unwrap_or_default(),
                module,
                degree,
                score,
            })
        })
        .collect::<Vec<_>>();
    steps.sort_by(|left, right| {
        right
            .degree
            .cmp(&left.degree)
            .then_with(|| {
                right
                    .score
                    .partial_cmp(&left.score)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .then_with(|| {
                topology
                    .get(&left.module)
                    .copied()
                    .unwrap_or_default()
                    .cmp(&topology.get(&right.module).copied().unwrap_or_default())
            })
            .then_with(|| left.module.cmp(&right.module))
    });
    steps
}

fn step_source_spans(module: &str, modules: &[ModuleDoc]) -> Vec<SourceSpan> {
    modules
        .iter()
        .find(|doc| doc.module == module)
        .map(|doc| doc.source_spans.clone())
        .unwrap_or_default()
}

fn is_rust_entry_file(file: &str) -> bool {
    file == "main.rs" || file == "lib.rs" || file.ends_with("/main.rs") || file.ends_with("/lib.rs")
}

fn is_public_api_symbol(symbol: &Symbol) -> bool {
    symbol.signature.as_deref().is_some_and(|signature| {
        let signature = signature.trim_start();
        signature == "pub" || signature.starts_with("pub ")
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn symbol_with_signature(signature: &str) -> Symbol {
        Symbol {
            id: "symbol-1".to_string(),
            project_id: "project-1".to_string(),
            file_path: "src/lib.rs".to_string(),
            name: "run".to_string(),
            qualified_name: "crate::run".to_string(),
            kind: "function".to_string(),
            language: "rust".to_string(),
            byte_start: 0,
            byte_end: 0,
            line_start: 1,
            line_end: 1,
            signature: Some(signature.to_string()),
            docstring: None,
            parent_symbol_id: None,
            content_hash: String::new(),
            summary: None,
            created_at: String::new(),
            updated_at: String::new(),
        }
    }

    #[test]
    fn public_api_symbol_accepts_plain_public_visibility() {
        assert!(is_public_api_symbol(&symbol_with_signature("pub")));
        assert!(is_public_api_symbol(&symbol_with_signature("pub fn run()")));
        assert!(is_public_api_symbol(&symbol_with_signature(
            "    pub struct Runner;"
        )));
    }

    #[test]
    fn public_api_symbol_rejects_restricted_visibility() {
        assert!(!is_public_api_symbol(&symbol_with_signature(
            "pub(crate) fn run()"
        )));
        assert!(!is_public_api_symbol(&symbol_with_signature(
            "pub(super) mod inner"
        )));
        assert!(!is_public_api_symbol(&symbol_with_signature(
            "pub(in crate::internal) fn run()"
        )));
    }
}
