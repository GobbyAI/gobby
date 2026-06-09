use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::PathBuf;

use gobby_core::config::{CODE_GRAPH_NAME, FalkorConfig};
use gobby_core::degradation::{DegradationKind, ServiceState};
use gobby_core::falkor::{self, GraphClient, Row};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::provenance::ProvenanceGraph;

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

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct CodeChangeSet {
    pub files: Vec<String>,
    pub symbols: Vec<String>,
}

#[derive(Debug, Clone)]
pub(crate) struct AffectedPages {
    pub pages: Vec<AffectedPage>,
    pub degradations: Vec<DegradationKind>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct AffectedPage {
    pub page_path: PathBuf,
    pub source_ids: Vec<String>,
    pub source_paths: Vec<PathBuf>,
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

pub(crate) fn affected_pages_for_changes(
    config: Option<&FalkorConfig>,
    project: &str,
    provenance: &ProvenanceGraph,
    changes: CodeChangeSet,
) -> anyhow::Result<AffectedPages> {
    affected_pages_for_changes_with_edges(provenance, &changes, |query| {
        code_edges(config, project, query)
    })
}

fn affected_pages_for_changes_with_edges(
    provenance: &ProvenanceGraph,
    changes: &CodeChangeSet,
    mut edges_for: impl FnMut(
        CodeGraphQuery<'_>,
    ) -> anyhow::Result<(Vec<CodeGraphEdge>, Option<DegradationKind>)>,
) -> anyhow::Result<AffectedPages> {
    let mut target_paths = changes
        .files
        .iter()
        .filter_map(|path| normalized_path(path))
        .collect::<BTreeSet<_>>();
    let target_source_ids = changes
        .symbols
        .iter()
        .filter_map(|symbol| normalized_source_id(symbol))
        .collect::<BTreeSet<_>>();
    let mut degradations = Vec::new();
    let mut degradation_keys = HashSet::new();

    for file in &changes.files {
        let (edges, degradation) = edges_for(CodeGraphQuery::File(file))?;
        if let Some(degradation) = degradation
            && degradation_keys.insert(degradation_key(&degradation))
        {
            degradations.push(degradation);
        }
        add_edge_paths(&mut target_paths, edges);
    }
    for symbol in &changes.symbols {
        let (edges, degradation) = edges_for(CodeGraphQuery::Symbol(symbol))?;
        if let Some(degradation) = degradation
            && degradation_keys.insert(degradation_key(&degradation))
        {
            degradations.push(degradation);
        }
        add_edge_paths(&mut target_paths, edges);
    }

    Ok(AffectedPages {
        pages: affected_pages_for_targets(provenance, &target_paths, &target_source_ids),
        degradations,
    })
}

fn add_edge_paths(target_paths: &mut BTreeSet<PathBuf>, edges: Vec<CodeGraphEdge>) {
    for edge in edges {
        if let Some(path) = edge.file_path.as_deref().and_then(normalized_path) {
            target_paths.insert(path);
        }
    }
}

fn affected_pages_for_targets(
    provenance: &ProvenanceGraph,
    target_paths: &BTreeSet<PathBuf>,
    target_source_ids: &BTreeSet<String>,
) -> Vec<AffectedPage> {
    let mut pages: BTreeMap<PathBuf, (BTreeSet<String>, BTreeSet<PathBuf>)> = BTreeMap::new();
    for link in provenance.links() {
        if !target_paths.contains(&link.source.path)
            && !target_source_ids.contains(&link.source.source_id)
            && !target_source_ids.contains(&link.source.chunk_id)
        {
            continue;
        }
        let (source_ids, source_paths) = pages
            .entry(link.section.page_path.clone())
            .or_insert_with(|| (BTreeSet::new(), BTreeSet::new()));
        source_ids.insert(link.source.source_id.clone());
        source_paths.insert(link.source.path.clone());
    }

    pages
        .into_iter()
        .map(|(page_path, (source_ids, source_paths))| AffectedPage {
            page_path,
            source_ids: source_ids.into_iter().collect(),
            source_paths: source_paths.into_iter().collect(),
        })
        .collect()
}

fn normalized_path(path: &str) -> Option<PathBuf> {
    let path = path.trim();
    (!path.is_empty()).then(|| PathBuf::from(path))
}

fn normalized_source_id(source_id: &str) -> Option<String> {
    let source_id = source_id.trim();
    (!source_id.is_empty()).then(|| source_id.to_string())
}

fn query_edges(
    client: &mut GraphClient,
    project: &str,
    target: CodeGraphQuery<'_>,
) -> anyhow::Result<Vec<CodeGraphEdge>> {
    let rows = client.query(&graph_query(target), Some(query_params(project, target)))?;
    Ok(rows.into_iter().filter_map(edge_from_row).collect())
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

fn edge_from_row(row: Row) -> Option<CodeGraphEdge> {
    let edge = required_row_string(&row, "edge")?;
    let source = required_row_string(&row, "source")?;
    let target = required_row_string(&row, "target")?;
    Some(CodeGraphEdge {
        edge,
        source,
        target,
        detail: row_string(&row, "detail"),
        file_path: row_string(&row, "file_path"),
        line: row_i64(&row, "line"),
    })
}

fn required_row_string(row: &Row, key: &str) -> Option<String> {
    let value = row_string(row, key);
    if value
        .as_deref()
        .is_some_and(|value| !value.trim().is_empty())
    {
        return value;
    }
    log::warn!("skipping malformed code graph row with missing or empty {key}");
    None
}

fn degradation_key(degradation: &DegradationKind) -> String {
    match degradation {
        DegradationKind::ServiceUnavailable { service, state } => {
            format!("service:{service}:{state:?}")
        }
        DegradationKind::PartialSearch {
            available,
            unavailable,
        } => format!(
            "partial-search:{}:{}",
            available.join(","),
            unavailable.join(",")
        ),
        DegradationKind::PartialData { component, message } => {
            format!("partial-data:{component}:{message}")
        }
        DegradationKind::StaleIndex { paths } => format!("stale-index:{}", paths.join(",")),
        DegradationKind::SkippedArtifacts { count, reason } => {
            format!("skipped-artifacts:{count}:{reason}")
        }
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
    fn code_graph_name_matches_gcode_projection() {
        assert_eq!(CODE_GRAPH_NAME, "gobby_code");
    }

    #[test]
    fn malformed_graph_rows_are_skipped() {
        let mut missing_source = Row::new();
        missing_source.insert("edge".to_string(), Value::String("CALLS".to_string()));
        missing_source.insert("target".to_string(), Value::String("callee".to_string()));
        assert!(edge_from_row(missing_source).is_none());

        let mut valid = Row::new();
        valid.insert("edge".to_string(), Value::String("CALLS".to_string()));
        valid.insert("source".to_string(), Value::String("caller".to_string()));
        valid.insert("target".to_string(), Value::String("callee".to_string()));
        assert_eq!(edge_from_row(valid).unwrap().source, "caller");
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

    #[test]
    fn change_triggered_refresh_maps_changed_files_through_graph_and_provenance() {
        let mut provenance = crate::provenance::ProvenanceGraph::default();
        provenance.add_link(crate::provenance::ProvenanceLink {
            source: crate::provenance::SourceChunkRef {
                source_id: "src-a".to_string(),
                chunk_id: "src-a#chunk-0".to_string(),
                path: "src/a.rs".into(),
                byte_start: 0,
                byte_end: 10,
            },
            section: crate::provenance::WikiSectionRef {
                page_path: "code/a.md".into(),
                heading: "A".to_string(),
                section_id: "a".to_string(),
            },
            claim: None,
        });
        provenance.add_link(crate::provenance::ProvenanceLink {
            source: crate::provenance::SourceChunkRef {
                source_id: "src-b".to_string(),
                chunk_id: "src-b#chunk-0".to_string(),
                path: "src/b.rs".into(),
                byte_start: 0,
                byte_end: 10,
            },
            section: crate::provenance::WikiSectionRef {
                page_path: "code/b.md".into(),
                heading: "B".to_string(),
                section_id: "b".to_string(),
            },
            claim: None,
        });
        let changes = CodeChangeSet {
            files: vec!["src/a.rs".to_string()],
            symbols: Vec::new(),
        };

        let affected = affected_pages_for_changes_with_edges(&provenance, &changes, |query| {
            assert_eq!(query, CodeGraphQuery::File("src/a.rs"));
            Ok((
                vec![CodeGraphEdge {
                    edge: "CALLS".to_string(),
                    source: "symbol-a".to_string(),
                    target: "symbol-b".to_string(),
                    detail: Some("b".to_string()),
                    file_path: Some("src/b.rs".into()),
                    line: Some(7),
                }],
                None,
            ))
        })
        .expect("affected pages");

        assert!(affected.degradations.is_empty());
        assert_eq!(
            affected
                .pages
                .iter()
                .map(|page| page.page_path.as_path())
                .collect::<Vec<_>>(),
            vec![
                std::path::Path::new("code/a.md"),
                std::path::Path::new("code/b.md")
            ]
        );
    }

    #[test]
    fn change_triggered_refresh_degrades_to_provenance_only_mapping() {
        let mut provenance = crate::provenance::ProvenanceGraph::default();
        provenance.add_link(crate::provenance::ProvenanceLink {
            source: crate::provenance::SourceChunkRef {
                source_id: "src-a".to_string(),
                chunk_id: "src-a#chunk-0".to_string(),
                path: "src/a.rs".into(),
                byte_start: 0,
                byte_end: 10,
            },
            section: crate::provenance::WikiSectionRef {
                page_path: "code/a.md".into(),
                heading: "A".to_string(),
                section_id: "a".to_string(),
            },
            claim: None,
        });
        let changes = CodeChangeSet {
            files: vec!["src/a.rs".to_string()],
            symbols: Vec::new(),
        };

        let affected = affected_pages_for_changes_with_edges(&provenance, &changes, |_query| {
            Ok((
                Vec::new(),
                Some(DegradationKind::ServiceUnavailable {
                    service: "gcode_code_graph".to_string(),
                    state: ServiceState::NotConfigured,
                }),
            ))
        })
        .expect("affected pages");

        assert_eq!(affected.pages.len(), 1);
        assert_eq!(
            affected.pages[0].page_path,
            std::path::PathBuf::from("code/a.md")
        );
        assert_eq!(affected.degradations.len(), 1);
    }

    #[test]
    fn duplicate_degradations_are_deduped_after_all_queries_run() {
        let provenance = crate::provenance::ProvenanceGraph::default();
        let changes = CodeChangeSet {
            files: vec!["src/a.rs".to_string()],
            symbols: vec!["symbol-a".to_string()],
        };
        let mut calls = Vec::new();
        let affected = affected_pages_for_changes_with_edges(&provenance, &changes, |query| {
            calls.push(format!("{query:?}"));
            Ok((
                Vec::new(),
                Some(DegradationKind::ServiceUnavailable {
                    service: GRAPH_SERVICE.to_string(),
                    state: ServiceState::Unreachable {
                        message: "synthetic outage".to_string(),
                    },
                }),
            ))
        })
        .expect("affected pages");

        assert_eq!(calls.len(), 2);
        assert_eq!(affected.degradations.len(), 1);
    }
}
