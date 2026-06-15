use std::collections::{BTreeSet, HashMap};
use std::path::Path;

use gobby_core::config::{CODE_GRAPH_NAME, FalkorConfig};
use gobby_core::falkor::GraphClient;

use crate::graph::{WikiGraphCodeEdge, WikiGraphDocument};
use crate::search::SearchScope;
use crate::support::config::SharedCodeGraphLimits;

use super::query::{optional_row_string, optional_row_usize};
use super::{
    CODE_CALL_EDGE_TRUNCATION_COMPONENT, CODE_GRAPH_PROVENANCE,
    CODE_IMPORT_EDGE_TRUNCATION_COMPONENT, CODE_TOTAL_EDGE_TRUNCATION_COMPONENT,
    MAX_TOTAL_CODE_EDGES, SharedCodeGraphEdges, SharedCodeGraphTruncation,
};

struct LimitedCodeGraphEdges {
    edges: Vec<WikiGraphCodeEdge>,
    truncated: bool,
}

pub(crate) fn load_code_graph_edges(
    config: &FalkorConfig,
    project_id: &str,
    documents: &[WikiGraphDocument],
    limits: SharedCodeGraphLimits,
) -> anyhow::Result<SharedCodeGraphEdges> {
    let mut client = GraphClient::from_config(config, CODE_GRAPH_NAME)?;
    let code_documents = documents
        .iter()
        .filter_map(|document| {
            code_doc_source_path(&document.path)
                .map(|file_path| (document.scope.clone(), document.path.clone(), file_path))
        })
        .collect::<Vec<_>>();
    let mut edges = Vec::new();
    let mut truncated_components = BTreeSet::new();
    let mut remaining_edges = MAX_TOTAL_CODE_EDGES;
    for (scope, document_path, file_path) in code_documents {
        let Some(call_limit) = remaining_code_edge_limit(limits.call_edge_limit, remaining_edges)
        else {
            truncated_components.insert(truncation_component(
                CODE_TOTAL_EDGE_TRUNCATION_COMPONENT,
                MAX_TOTAL_CODE_EDGES,
            ));
            break;
        };
        let call_edges = code_call_edges(
            &mut client,
            project_id,
            &scope,
            &document_path,
            &file_path,
            call_limit,
        )?;
        if call_edges.truncated {
            record_code_edge_truncation(
                &mut truncated_components,
                CODE_CALL_EDGE_TRUNCATION_COMPONENT,
                limits.call_edge_limit,
                call_limit,
            );
        }
        remaining_edges = remaining_edges.saturating_sub(call_edges.edges.len());
        edges.extend(call_edges.edges);
        if remaining_edges == 0 {
            truncated_components.insert(truncation_component(
                CODE_TOTAL_EDGE_TRUNCATION_COMPONENT,
                MAX_TOTAL_CODE_EDGES,
            ));
            break;
        }

        let Some(import_limit) =
            remaining_code_edge_limit(limits.import_edge_limit, remaining_edges)
        else {
            truncated_components.insert(truncation_component(
                CODE_TOTAL_EDGE_TRUNCATION_COMPONENT,
                MAX_TOTAL_CODE_EDGES,
            ));
            break;
        };
        let import_edges = code_import_edges(
            &mut client,
            project_id,
            &scope,
            &document_path,
            &file_path,
            import_limit,
        )?;
        if import_edges.truncated {
            record_code_edge_truncation(
                &mut truncated_components,
                CODE_IMPORT_EDGE_TRUNCATION_COMPONENT,
                limits.import_edge_limit,
                import_limit,
            );
        }
        remaining_edges = remaining_edges.saturating_sub(import_edges.edges.len());
        edges.extend(import_edges.edges);
        if remaining_edges == 0 {
            truncated_components.insert(truncation_component(
                CODE_TOTAL_EDGE_TRUNCATION_COMPONENT,
                MAX_TOTAL_CODE_EDGES,
            ));
            break;
        }
    }
    Ok(SharedCodeGraphEdges {
        edges,
        truncation: SharedCodeGraphTruncation::from_components(truncated_components),
    })
}

fn code_call_edges(
    client: &mut GraphClient,
    project_id: &str,
    scope: &SearchScope,
    document_path: &Path,
    file_path: &str,
    limit: usize,
) -> anyhow::Result<LimitedCodeGraphEdges> {
    let query = code_call_edges_query();
    let mut rows = client.query(
        query,
        Some(code_edge_query_params(project_id, file_path, limit)?),
    )?;
    let truncated = truncate_to_limit(&mut rows, limit);
    Ok(LimitedCodeGraphEdges {
        edges: rows
            .into_iter()
            .map(|row| {
                let source_file = optional_row_string(&row, "source_file_path")
                    .unwrap_or_else(|| file_path.to_string());
                let source_name = optional_row_string(&row, "source_name")
                    .unwrap_or_else(|| "unknown".to_string());
                let target_file = optional_row_string(&row, "target_file_path")
                    .unwrap_or_else(|| "external".to_string());
                let target_name = optional_row_string(&row, "target_name")
                    .unwrap_or_else(|| "unknown".to_string());
                let incoming = target_file == file_path && source_file != file_path;
                WikiGraphCodeEdge {
                    scope: scope.clone(),
                    document_path: document_path.to_path_buf(),
                    source: code_endpoint(&source_file, &source_name),
                    target: code_endpoint(&target_file, &target_name),
                    kind: if incoming { "callers" } else { "calls" }.to_string(),
                    direction: if incoming { "incoming" } else { "outgoing" }.to_string(),
                    line: optional_row_usize(&row, "line"),
                    provenance: CODE_GRAPH_PROVENANCE.to_string(),
                }
            })
            .collect(),
        truncated,
    })
}

pub(super) fn code_call_edges_query() -> &'static str {
    "\
        MATCH (source:CodeSymbol {project: $project})-[r:CALLS]->(target {project: $project}) \
        WHERE source.file_path = $path OR (target:CodeSymbol AND target.file_path = $path) \
        RETURN source.file_path AS source_file_path, source.name AS source_name, \
               target.file_path AS target_file_path, target.name AS target_name, r.line AS line \
        ORDER BY source.file_path, source.name, target.file_path, target.name, r.line \
        LIMIT $limit"
}

fn code_import_edges(
    client: &mut GraphClient,
    project_id: &str,
    scope: &SearchScope,
    document_path: &Path,
    file_path: &str,
    limit: usize,
) -> anyhow::Result<LimitedCodeGraphEdges> {
    let query = code_import_edges_query();
    let mut rows = client.query(
        query,
        Some(code_edge_query_params(project_id, file_path, limit)?),
    )?;
    let truncated = truncate_to_limit(&mut rows, limit);
    Ok(LimitedCodeGraphEdges {
        edges: rows
            .into_iter()
            .map(|row| {
                let source_file = optional_row_string(&row, "source_file_path")
                    .unwrap_or_else(|| file_path.to_string());
                let target_name = optional_row_string(&row, "target_name")
                    .unwrap_or_else(|| "unknown".to_string());
                WikiGraphCodeEdge {
                    scope: scope.clone(),
                    document_path: document_path.to_path_buf(),
                    source: source_file,
                    target: target_name,
                    kind: "imports".to_string(),
                    direction: "outgoing".to_string(),
                    line: None,
                    provenance: CODE_GRAPH_PROVENANCE.to_string(),
                }
            })
            .collect(),
        truncated,
    })
}

pub(super) fn code_import_edges_query() -> &'static str {
    "\
        MATCH (file:CodeFile {path: $path, project: $project})-[r:IMPORTS]->(module:CodeModule {project: $project}) \
        RETURN file.path AS source_file_path, module.name AS target_name \
        ORDER BY file.path, module.name \
        LIMIT $limit"
}

pub(super) fn code_edge_query_params(
    project_id: &str,
    file_path: &str,
    limit: usize,
) -> anyhow::Result<HashMap<String, String>> {
    Ok(HashMap::from([
        (
            "project".to_string(),
            gobby_core::falkor::escape_string(project_id),
        ),
        (
            "path".to_string(),
            gobby_core::falkor::escape_string(file_path),
        ),
        ("limit".to_string(), sentinel_limit(limit)?.to_string()),
    ]))
}

fn sentinel_limit(limit: usize) -> anyhow::Result<usize> {
    limit
        .checked_add(1)
        .ok_or_else(|| anyhow::anyhow!("shared code graph edge limit is too large: {limit}"))
}

pub(super) fn truncate_to_limit<T>(rows: &mut Vec<T>, limit: usize) -> bool {
    let truncated = rows.len() > limit;
    if truncated {
        rows.truncate(limit);
    }
    truncated
}

pub(super) fn remaining_code_edge_limit(
    configured_limit: usize,
    remaining_edges: usize,
) -> Option<usize> {
    (remaining_edges > 0).then_some(configured_limit.min(remaining_edges))
}

pub(super) fn record_code_edge_truncation(
    components: &mut BTreeSet<String>,
    component: &str,
    configured_limit: usize,
    query_limit: usize,
) {
    let (component, limit) = if query_limit < configured_limit {
        (CODE_TOTAL_EDGE_TRUNCATION_COMPONENT, MAX_TOTAL_CODE_EDGES)
    } else {
        (component, configured_limit)
    };
    components.insert(truncation_component(component, limit));
}

pub(super) fn truncation_component(component: &str, limit: usize) -> String {
    format!("{component}>{limit}")
}

pub(super) fn code_doc_source_path(path: &Path) -> Option<String> {
    normalize_graph_path(path)
        .strip_prefix("code/files/")
        .and_then(|path| path.strip_suffix(".md"))
        .map(str::to_string)
}

fn code_endpoint(file_path: &str, symbol: &str) -> String {
    if symbol.is_empty() {
        file_path.to_string()
    } else {
        format!("{file_path}:{symbol}")
    }
}

fn normalize_graph_path(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}
