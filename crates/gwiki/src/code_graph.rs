use std::collections::HashMap;

use gobby_core::config::FalkorConfig;
use gobby_core::degradation::{DegradationKind, ServiceState};
use gobby_core::falkor::{self, GraphClient, Row};
use serde::{Deserialize, Serialize};
use serde_json::Value;

pub(crate) const CODE_GRAPH_NAME: &str = gobby_core::config::CODE_GRAPH_NAME;
const GRAPH_SERVICE: &str = "gcode_code_graph";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum CodeGraphQuery<'a> {
    File(&'a str),
    Symbol(&'a str),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct CodeGraphEdge {
    pub edge: String,
    pub source: String,
    pub target: String,
    pub detail: Option<String>,
    pub file_path: Option<String>,
    pub line: Option<i64>,
}

pub(crate) fn code_edges(
    config: Option<&FalkorConfig>,
    project: &str,
    target: CodeGraphQuery<'_>,
) -> anyhow::Result<(Vec<CodeGraphEdge>, Option<DegradationKind>)> {
    let default = Vec::new();
    let (edges, state) = falkor::with_graph(config, CODE_GRAPH_NAME, default, |client| {
        query_edges(client, project, target)
    })?;

    let degradation = match state {
        ServiceState::Available => None,
        state => Some(DegradationKind::ServiceUnavailable {
            service: GRAPH_SERVICE.to_string(),
            state,
        }),
    };

    Ok((edges, degradation))
}

fn query_edges(
    client: &mut GraphClient,
    project: &str,
    target: CodeGraphQuery<'_>,
) -> anyhow::Result<Vec<CodeGraphEdge>> {
    let rows = client.query(&graph_query(target), Some(query_params(project, target)))?;
    Ok(rows.into_iter().map(edge_from_row).collect())
}

fn query_params(project: &str, target: CodeGraphQuery<'_>) -> HashMap<String, String> {
    let mut params = HashMap::from([("project".to_string(), project.to_string())]);
    match target {
        CodeGraphQuery::File(path) => {
            params.insert("file_path".to_string(), path.to_string());
        }
        CodeGraphQuery::Symbol(id) => {
            params.insert("symbol_id".to_string(), id.to_string());
        }
    }
    params
}

fn edge_from_row(row: Row) -> CodeGraphEdge {
    CodeGraphEdge {
        edge: row_string(&row, "edge").unwrap_or_default(),
        source: row_string(&row, "source").unwrap_or_default(),
        target: row_string(&row, "target").unwrap_or_default(),
        detail: row_string(&row, "detail"),
        file_path: row_string(&row, "file_path"),
        line: row_i64(&row, "line"),
    }
}

fn row_string(row: &Row, key: &str) -> Option<String> {
    row.get(key)
        .and_then(Value::as_str)
        .map(ToString::to_string)
}

fn row_i64(row: &Row, key: &str) -> Option<i64> {
    row.get(key).and_then(Value::as_i64)
}

fn graph_query(target: CodeGraphQuery<'_>) -> String {
    match target {
        CodeGraphQuery::File(_) => file_graph_query(),
        CodeGraphQuery::Symbol(_) => symbol_graph_query(),
    }
}

fn file_graph_query() -> String {
    [
        "MATCH (file:CodeFile {project: $project, path: $file_path})-[import:IMPORTS]->(module:CodeModule {project: $project})
         RETURN 'IMPORTS' AS edge, file.path AS source, module.name AS target, import.import AS detail, file.path AS file_path, import.line AS line",
        "MATCH (file:CodeFile {project: $project, path: $file_path})-[defines:DEFINES]->(symbol:CodeSymbol {project: $project})
         RETURN 'DEFINES' AS edge, file.path AS source, symbol.id AS target, symbol.name AS detail, symbol.file_path AS file_path, symbol.line_start AS line",
        "MATCH (file:CodeFile {project: $project, path: $file_path})-[:DEFINES]->(symbol:CodeSymbol {project: $project})-[calls:CALLS]->(callee {project: $project})
         WHERE callee:CodeSymbol OR callee:ExternalSymbol OR callee:UnresolvedCallee
         RETURN 'CALLS' AS edge, symbol.id AS source, coalesce(callee.id, callee.name) AS target, callee.name AS detail, calls.file AS file_path, calls.line AS line",
        "MATCH (file:CodeFile {project: $project, path: $file_path})-[:DEFINES]->(symbol:CodeSymbol {project: $project})<-[caller_call:CALLS]-(caller:CodeSymbol {project: $project})
         RETURN 'CALLER' AS edge, caller.id AS source, symbol.id AS target, caller.name AS detail, caller_call.file AS file_path, caller_call.line AS line",
    ]
    .join("\nUNION ALL\n")
}

fn symbol_graph_query() -> String {
    [
        "MATCH (file:CodeFile {project: $project})-[defines:DEFINES]->(symbol:CodeSymbol {project: $project, id: $symbol_id})
         RETURN 'DEFINES' AS edge, file.path AS source, symbol.id AS target, symbol.name AS detail, symbol.file_path AS file_path, symbol.line_start AS line",
        "MATCH (file:CodeFile {project: $project})-[:DEFINES]->(symbol:CodeSymbol {project: $project, id: $symbol_id})
         MATCH (file)-[import:IMPORTS]->(module:CodeModule {project: $project})
         RETURN 'IMPORTS' AS edge, file.path AS source, module.name AS target, import.import AS detail, file.path AS file_path, import.line AS line",
        "MATCH (symbol:CodeSymbol {project: $project, id: $symbol_id})-[calls:CALLS]->(callee {project: $project})
         WHERE callee:CodeSymbol OR callee:ExternalSymbol OR callee:UnresolvedCallee
         RETURN 'CALLS' AS edge, symbol.id AS source, coalesce(callee.id, callee.name) AS target, callee.name AS detail, calls.file AS file_path, calls.line AS line",
        "MATCH (symbol:CodeSymbol {project: $project, id: $symbol_id})<-[caller_call:CALLS]-(caller:CodeSymbol {project: $project})
         RETURN 'CALLER' AS edge, caller.id AS source, symbol.id AS target, caller.name AS detail, caller_call.file AS file_path, caller_call.line AS line",
    ]
    .join("\nUNION ALL\n")
}

#[cfg(test)]
mod tests {
    use super::*;
    use gobby_core::degradation::{DegradationKind, ServiceState};

    #[test]
    fn code_graph_name_comes_from_shared_config() {
        assert_eq!(CODE_GRAPH_NAME, gobby_core::config::CODE_GRAPH_NAME);
        assert_eq!(CODE_GRAPH_NAME, "gobby_code");
    }

    #[test]
    fn unavailable_graph_degrades_to_empty_edges() {
        let (edges, degradation) =
            code_edges(None, "project-1", CodeGraphQuery::File("src/lib.rs"))
                .expect("missing config should degrade");

        assert!(edges.is_empty());
        assert!(matches!(
            degradation,
            Some(DegradationKind::ServiceUnavailable {
                service,
                state: ServiceState::NotConfigured,
            }) if service == "gcode_code_graph"
        ));
    }

    #[test]
    fn file_query_reads_imports_definitions_calls_and_callers() {
        let query = graph_query(CodeGraphQuery::File("src/lib.rs"));

        assert!(query.contains("MATCH (file:CodeFile {project: $project, path: $file_path})"));
        assert!(query.contains("-[import:IMPORTS]->"));
        assert!(query.contains("-[defines:DEFINES]->"));
        assert!(query.contains("-[calls:CALLS]->"));
        assert!(query.contains("<-[caller_call:CALLS]-"));
        assert!(query.contains("CodeSymbol"));
        assert!(query.contains("ExternalSymbol"));
        assert!(query.contains("UnresolvedCallee"));
    }

    #[test]
    fn symbol_query_reads_calls_and_callers() {
        let query = graph_query(CodeGraphQuery::Symbol("symbol-1"));

        assert!(query.contains("MATCH (symbol:CodeSymbol {project: $project, id: $symbol_id})"));
        assert!(query.contains("-[defines:DEFINES]->"));
        assert!(query.contains("-[import:IMPORTS]->"));
        assert!(query.contains("-[calls:CALLS]->"));
        assert!(query.contains("<-[caller_call:CALLS]-"));
    }
}
