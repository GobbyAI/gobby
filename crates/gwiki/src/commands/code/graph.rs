use std::collections::{BTreeMap, HashSet};

use gobby_code::codewiki_facts::{GraphEdgeKind, GraphOutcome, ScopeSelector};

use super::runtime::CodeEngineRuntime;
use super::*;

pub(crate) fn fetch_codewiki_graph_edges(
    runtime: &CodeEngineRuntime,
    files: &[String],
    symbols: &[Symbol],
    edge_limit: usize,
) -> anyhow::Result<CodewikiGraph> {
    let core_files = files
        .iter()
        .filter(|file| is_core_file(file))
        .cloned()
        .collect::<Vec<_>>();
    let core_symbol_ids = symbols
        .iter()
        .filter(|symbol| is_core_file(&symbol.file_path))
        .map(|symbol| symbol.id.clone())
        .collect::<HashSet<_>>();
    if core_files.is_empty() || core_symbol_ids.is_empty() {
        return Ok(CodewikiGraph::available(Vec::new()));
    }

    let scope = ScopeSelector::paths(core_files.iter().cloned());
    let call_outcome = runtime
        .facts
        .edges(&scope, GraphEdgeKind::Call, edge_limit)?;
    let import_outcome = runtime
        .facts
        .edges(&scope, GraphEdgeKind::Import, edge_limit)?;
    let (call_pairs, call_truncated) = match edge_pairs(call_outcome) {
        Some(outcome) => outcome,
        None => return Ok(CodewikiGraph::unavailable()),
    };
    let (import_pairs, import_truncated) = match edge_pairs(import_outcome) {
        Some(outcome) => outcome,
        None => return Ok(CodewikiGraph::unavailable()),
    };

    let mut edges = call_pairs
        .into_iter()
        .filter(|(source, target)| {
            core_symbol_ids.contains(source) && core_symbol_ids.contains(target)
        })
        .map(|(source, target)| CodewikiGraphEdge::call(source, target))
        .collect::<Vec<_>>();
    let file_symbols = symbols_by_file_component(symbols);
    edges.extend(import_edges_from_pairs(
        &import_pairs,
        &core_files,
        &file_symbols,
    ));

    if call_truncated || import_truncated {
        Ok(CodewikiGraph::truncated(edges))
    } else {
        Ok(CodewikiGraph::available(edges))
    }
}

fn edge_pairs(
    outcome: GraphOutcome<gobby_code::codewiki_facts::GraphEdge>,
) -> Option<(Vec<(String, String)>, bool)> {
    match outcome {
        GraphOutcome::Available(edges) => Some((pairs(edges), false)),
        GraphOutcome::Truncated(edges) => Some((pairs(edges), true)),
        GraphOutcome::Empty => Some((Vec::new(), false)),
        GraphOutcome::Unavailable { .. } => None,
    }
}

fn pairs(edges: Vec<gobby_code::codewiki_facts::GraphEdge>) -> Vec<(String, String)> {
    edges
        .into_iter()
        .map(|edge| (edge.source, edge.target))
        .collect()
}

/// Resolve project-scoped import rows into component edges, keeping only
/// edges whose source file is core.
pub(crate) fn import_edges_from_pairs(
    pairs: &[(String, String)],
    core_files: &[String],
    file_symbols: &BTreeMap<String, Vec<String>>,
) -> Vec<CodewikiGraphEdge> {
    let core_file_set = core_files
        .iter()
        .map(String::as_str)
        .collect::<HashSet<_>>();
    let mut edges = Vec::new();
    for (source_file, target_module) in pairs {
        if !core_file_set.contains(source_file.as_str()) {
            continue;
        }
        let Some(source_component_id) = first_component_for_file(file_symbols, source_file) else {
            continue;
        };
        for target_file in files_for_import_target(core_files, target_module) {
            let Some(target_component_id) = first_component_for_file(file_symbols, target_file)
            else {
                continue;
            };
            edges.push(CodewikiGraphEdge::import(
                source_component_id.clone(),
                target_component_id,
            ));
        }
    }
    edges
}
