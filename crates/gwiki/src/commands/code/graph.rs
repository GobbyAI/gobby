use std::collections::{BTreeMap, HashSet};

#[cfg(test)]
use std::collections::HashMap;

use gobby_code::codewiki_facts::{GraphEdgeKind, GraphOutcome, ScopeSelector};

use super::runtime::CodeEngineRuntime;
use super::*;

pub(crate) fn fetch_codewiki_graph_edges(
    runtime: &CodeEngineRuntime,
    files: &[String],
    symbols: &[Symbol],
    edge_limit: usize,
) -> anyhow::Result<CodewikiGraph> {
    let core_symbol_ids = symbols
        .iter()
        .filter(|symbol| is_core_file(&symbol.file_path))
        .map(|symbol| symbol.id.clone())
        .collect::<HashSet<_>>();
    if core_symbol_ids.is_empty() {
        return Ok(CodewikiGraph::available(Vec::new()));
    }

    let scope = ScopeSelector::paths(files.iter().cloned());
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
    let core_files = files
        .iter()
        .filter(|file| is_core_file(file))
        .cloned()
        .collect::<Vec<_>>();
    if !core_files.is_empty() {
        let file_symbols = symbols_by_file_component(symbols);
        edges.extend(import_edges_from_pairs(
            &import_pairs,
            &core_files,
            &file_symbols,
        ));
    }

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

#[cfg(test)]
pub(crate) fn codewiki_call_edges_query(
    project_id: &str,
    edge_limit: usize,
) -> (String, HashMap<String, String>) {
    edge_query(
        project_id,
        edge_limit,
        "CodeSymbol",
        "CALLS",
        "CodeSymbol",
        "source.id",
        "target.id",
    )
}

#[cfg(test)]
pub(crate) fn codewiki_import_edges_query(
    project_id: &str,
    edge_limit: usize,
) -> (String, HashMap<String, String>) {
    edge_query(
        project_id,
        edge_limit,
        "CodeFile",
        "IMPORTS",
        "CodeModule",
        "source.path",
        "target.name",
    )
}

#[cfg(test)]
fn edge_query(
    project_id: &str,
    edge_limit: usize,
    source_label: &str,
    relation: &str,
    target_label: &str,
    source_field: &str,
    target_field: &str,
) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (source:{source_label} {{project: $project}})-[:{relation}]->(target:{target_label} {{project: $project}}) \
             RETURN {source_field} AS source, {target_field} AS target \
             ORDER BY source, target \
             LIMIT {edge_limit}"
        ),
        HashMap::from([("project".to_string(), cypher_string_literal(project_id))]),
    )
}

#[cfg(test)]
fn cypher_string_literal(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    for character in value.chars() {
        match character {
            '\\' => escaped.push_str("\\\\"),
            '\'' => escaped.push_str("\\'"),
            '"' => escaped.push_str("\\\""),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            '\u{0008}' => escaped.push_str("\\b"),
            '\u{000C}' => escaped.push_str("\\f"),
            character if character.is_control() => {
                escaped.push_str(&format!("\\u{:04X}", character as u32));
            }
            character => escaped.push(character),
        }
    }
    format!("'{escaped}'")
}
