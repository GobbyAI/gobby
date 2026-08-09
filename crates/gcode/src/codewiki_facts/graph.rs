use std::collections::{HashMap, HashSet};

use anyhow::Result;
use gobby_core::degradation::ServiceState;
use gobby_core::falkor::Row;

use crate::graph::{code_graph, typed_query};
use crate::models::GraphResult;

use super::{CodewikiFacts, ScopeSelector};

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum GraphAvailability {
    Available,
    Unavailable { reason: String },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum GraphEdgeKind {
    Call,
    Import,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GraphEdge {
    pub source: String,
    pub target: String,
    pub kind: GraphEdgeKind,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GraphNodeFact {
    pub id: String,
    pub name: String,
    pub file_path: String,
    pub line: usize,
    pub confidence: String,
    pub relation: Option<String>,
    pub distance: Option<usize>,
}

impl From<GraphResult> for GraphNodeFact {
    fn from(result: GraphResult) -> Self {
        Self {
            id: result.id,
            name: result.name,
            file_path: result.file_path,
            line: result.line,
            confidence: result.confidence.to_string(),
            relation: result.relation,
            distance: result.distance,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum GraphOutcome<T> {
    Available(Vec<T>),
    Truncated(Vec<T>),
    Empty,
    Unavailable { reason: String },
}

impl CodewikiFacts {
    pub fn graph_availability(&self) -> GraphAvailability {
        let Some(config) = &self.context().falkordb else {
            return GraphAvailability::Unavailable {
                reason: "FalkorDB is not configured".to_string(),
            };
        };
        let connection_config = config.connection_config();
        match gobby_core::falkor::with_graph(
            Some(&connection_config),
            &config.graph_name,
            None,
            |_| Ok(Some(())),
        ) {
            Ok((Some(()), ServiceState::Available)) => GraphAvailability::Available,
            Ok((_, ServiceState::NotConfigured)) => GraphAvailability::Unavailable {
                reason: "FalkorDB is not configured".to_string(),
            },
            Ok((_, ServiceState::Unreachable { message })) => {
                GraphAvailability::Unavailable { reason: message }
            }
            Ok((None, ServiceState::Available)) => GraphAvailability::Unavailable {
                reason: "graph availability probe returned no value".to_string(),
            },
            Err(error) => GraphAvailability::Unavailable {
                reason: format!("{error:#}"),
            },
        }
    }

    pub fn edges(
        &self,
        seed: &ScopeSelector,
        kind: GraphEdgeKind,
        limit: usize,
    ) -> Result<GraphOutcome<GraphEdge>> {
        if limit == 0 {
            return Ok(GraphOutcome::Empty);
        }
        let files = self.scoped_files(seed)?;
        let file_paths = files
            .iter()
            .map(|file| file.path.clone())
            .collect::<HashSet<_>>();
        let symbol_ids = self
            .symbols_in(&files.iter().map(|file| file.id.clone()).collect::<Vec<_>>())?
            .into_iter()
            .map(|symbol| symbol.id)
            .collect::<HashSet<_>>();
        let query = self.query_edge_rows(kind, limit);
        let rows = match classify_query(query, limit)? {
            GraphOutcome::Available(rows) => (rows, false),
            GraphOutcome::Truncated(rows) => (rows, true),
            GraphOutcome::Empty => return Ok(GraphOutcome::Empty),
            GraphOutcome::Unavailable { reason } => {
                return Ok(GraphOutcome::Unavailable { reason });
            }
        };
        let edges = rows
            .0
            .into_iter()
            .filter(|(source, target)| match kind {
                GraphEdgeKind::Call => symbol_ids.contains(source) && symbol_ids.contains(target),
                GraphEdgeKind::Import => file_paths.contains(source),
            })
            .map(|(source, target)| GraphEdge {
                source,
                target,
                kind,
            })
            .collect::<Vec<_>>();
        if rows.1 {
            Ok(GraphOutcome::Truncated(edges))
        } else if edges.is_empty() {
            Ok(GraphOutcome::Empty)
        } else {
            Ok(GraphOutcome::Available(edges))
        }
    }

    pub fn callers(&self, symbol_id: &str, limit: usize) -> Result<GraphOutcome<GraphNodeFact>> {
        self.graph_nodes(limit, || {
            code_graph::find_callers(self.context(), symbol_id, 0, limit)
        })
    }

    pub fn usages(&self, symbol_id: &str, limit: usize) -> Result<GraphOutcome<GraphNodeFact>> {
        self.graph_nodes(limit, || {
            code_graph::find_usages(self.context(), symbol_id, 0, limit)
        })
    }

    pub fn imports(&self, file_path: &str, limit: usize) -> Result<GraphOutcome<GraphNodeFact>> {
        self.graph_nodes(limit, || {
            let mut results = code_graph::get_imports(self.context(), file_path)?;
            results.truncate(limit);
            Ok(results)
        })
    }

    fn graph_nodes(
        &self,
        limit: usize,
        query: impl FnOnce() -> Result<Vec<GraphResult>>,
    ) -> Result<GraphOutcome<GraphNodeFact>> {
        if limit == 0 {
            return Ok(GraphOutcome::Empty);
        }
        if let GraphAvailability::Unavailable { reason } = self.graph_availability() {
            return Ok(GraphOutcome::Unavailable { reason });
        }
        Ok(match classify_query(query(), limit)? {
            GraphOutcome::Available(rows) => {
                GraphOutcome::Available(rows.into_iter().map(GraphNodeFact::from).collect())
            }
            GraphOutcome::Truncated(rows) => {
                GraphOutcome::Truncated(rows.into_iter().map(GraphNodeFact::from).collect())
            }
            GraphOutcome::Empty => GraphOutcome::Empty,
            GraphOutcome::Unavailable { reason } => GraphOutcome::Unavailable { reason },
        })
    }

    fn query_edge_rows(&self, kind: GraphEdgeKind, limit: usize) -> Result<Vec<(String, String)>> {
        let Some(config) = &self.context().falkordb else {
            return Err(anyhow::Error::new(
                code_graph::GraphReadError::NotConfigured,
            ));
        };
        let (query, params) = edge_query(&self.context().project_id, kind, limit);
        let connection_config = config.connection_config();
        match gobby_core::falkor::with_graph(
            Some(&connection_config),
            &config.graph_name,
            None,
            |client| client.query(&query, Some(params)).map(Some),
        ) {
            Ok((Some(rows), ServiceState::Available)) => Ok(rows_to_pairs(&rows)),
            Ok((_, ServiceState::NotConfigured)) => Err(anyhow::Error::new(
                code_graph::GraphReadError::NotConfigured,
            )),
            Ok((_, ServiceState::Unreachable { message })) => Err(anyhow::Error::new(
                code_graph::GraphReadError::Unreachable { message },
            )),
            Ok((None, ServiceState::Available)) => Err(anyhow::Error::new(
                code_graph::GraphReadError::QueryFailed {
                    message: "graph read returned no value".to_string(),
                },
            )),
            Err(error) => Err(anyhow::Error::new(
                code_graph::GraphReadError::QueryFailed {
                    message: format!("{error:#}"),
                },
            )),
        }
    }
}

pub(super) fn classify_query<T>(result: Result<Vec<T>>, limit: usize) -> Result<GraphOutcome<T>> {
    match result {
        Ok(rows) if rows.is_empty() => Ok(GraphOutcome::Empty),
        Ok(rows) if rows.len() == limit => Ok(GraphOutcome::Truncated(rows)),
        Ok(rows) => Ok(GraphOutcome::Available(rows)),
        Err(error) => match error.downcast_ref::<code_graph::GraphReadError>() {
            Some(code_graph::GraphReadError::NotConfigured) => Ok(GraphOutcome::Unavailable {
                reason: error.to_string(),
            }),
            Some(code_graph::GraphReadError::Unreachable { .. }) => Ok(GraphOutcome::Unavailable {
                reason: error.to_string(),
            }),
            _ => Err(error),
        },
    }
}

fn edge_query(
    project_id: &str,
    kind: GraphEdgeKind,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let query = match kind {
        GraphEdgeKind::Call => format!(
            "MATCH (source:CodeSymbol {{project: $project}})-[:CALLS]->\
             (target:CodeSymbol {{project: $project}}) \
             RETURN source.id AS source, target.id AS target \
             ORDER BY source, target LIMIT {limit}"
        ),
        GraphEdgeKind::Import => format!(
            "MATCH (source:CodeFile {{project: $project}})-[:IMPORTS]->\
             (target:CodeModule {{project: $project}}) \
             RETURN source.path AS source, target.name AS target \
             ORDER BY source, target LIMIT {limit}"
        ),
    };
    (
        query,
        HashMap::from([(
            "project".to_string(),
            typed_query::cypher_string_literal(project_id),
        )]),
    )
}

fn rows_to_pairs(rows: &[Row]) -> Vec<(String, String)> {
    rows.iter()
        .filter_map(|row| {
            let source = row.get("source").and_then(|value| value.as_str())?;
            let target = row.get("target").and_then(|value| value.as_str())?;
            Some((source.to_string(), target.to_string()))
        })
        .collect()
}
