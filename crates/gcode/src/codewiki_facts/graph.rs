use anyhow::Result;
use gobby_core::degradation::ServiceState;
use gobby_core::falkor::Row;

use crate::graph::code_graph;
use crate::models::GraphResult;

use super::graph_query::{self, EdgeQueryPlan, QueryPlans, ScopeKeys, plans_for};
use super::{CodewikiFacts, ScopeSelector};

pub use super::graph_query::{GraphBounds, GraphDirection, GraphEdgeKind, GraphScopeMode};

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum GraphAvailability {
    Available,
    Unavailable { reason: String },
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

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ScopedGraph {
    pub outcome: GraphOutcome<GraphEdge>,
    pub incoming_truncated: bool,
    pub outgoing_truncated: bool,
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
        let mode = match kind {
            GraphEdgeKind::Import => GraphScopeMode::Incident,
            GraphEdgeKind::Call | GraphEdgeKind::Inheritance => GraphScopeMode::Closed,
        };
        Ok(self
            .scoped_edges(seed, kind, GraphBounds::outgoing(limit), mode)?
            .outcome)
    }

    pub fn scoped_edges(
        &self,
        seed: &ScopeSelector,
        kind: GraphEdgeKind,
        bounds: GraphBounds,
        mode: GraphScopeMode,
    ) -> Result<ScopedGraph> {
        if bounds.incoming_limit == 0 && bounds.outgoing_limit == 0 {
            return Ok(ScopedGraph {
                outcome: GraphOutcome::Empty,
                incoming_truncated: false,
                outgoing_truncated: false,
            });
        }
        let keys = self.resolve_scope_keys(seed)?;
        if keys.is_empty() {
            return Ok(ScopedGraph {
                outcome: GraphOutcome::Empty,
                incoming_truncated: false,
                outgoing_truncated: false,
            });
        }
        let plans = match plans_for(kind, bounds, mode, keys) {
            QueryPlans::ClosedScopeTooLarge { .. } => {
                return Ok(ScopedGraph {
                    outcome: GraphOutcome::Truncated(Vec::new()),
                    incoming_truncated: bounds.incoming_limit > 0,
                    outgoing_truncated: bounds.outgoing_limit > 0,
                });
            }
            QueryPlans::Ready(plans) => plans,
        };
        let mut incoming_rows = Vec::new();
        let mut outgoing_rows = Vec::new();
        let mut incoming_truncated = false;
        let mut outgoing_truncated = false;
        for plan in plans {
            match self.query_plan_rows(&plan)? {
                GraphOutcome::Unavailable { reason } => {
                    return Ok(ScopedGraph {
                        outcome: GraphOutcome::Unavailable { reason },
                        incoming_truncated: false,
                        outgoing_truncated: false,
                    });
                }
                GraphOutcome::Empty => {}
                GraphOutcome::Available(rows) => match plan.direction {
                    GraphDirection::Incoming => incoming_rows.extend(rows),
                    GraphDirection::Outgoing => outgoing_rows.extend(rows),
                },
                GraphOutcome::Truncated(rows) => match plan.direction {
                    GraphDirection::Incoming => {
                        incoming_truncated = true;
                        incoming_rows.extend(rows);
                    }
                    GraphDirection::Outgoing => {
                        outgoing_truncated = true;
                        outgoing_rows.extend(rows);
                    }
                },
            }
        }
        let incoming_limit = graph_query::clamp_declared_limit(bounds.incoming_limit);
        let outgoing_limit = graph_query::clamp_declared_limit(bounds.outgoing_limit);
        incoming_truncated |= take_bounded(&mut incoming_rows, incoming_limit);
        outgoing_truncated |= take_bounded(&mut outgoing_rows, outgoing_limit);
        let mut rows = outgoing_rows;
        rows.extend(incoming_rows);
        rows.sort();
        rows.dedup();
        let truncated = incoming_truncated || outgoing_truncated;
        let edges = rows
            .into_iter()
            .map(|(source, target)| GraphEdge {
                source,
                target,
                kind,
            })
            .collect::<Vec<_>>();
        let outcome = if edges.is_empty() {
            GraphOutcome::Empty
        } else if truncated {
            GraphOutcome::Truncated(edges)
        } else {
            GraphOutcome::Available(edges)
        };
        Ok(ScopedGraph {
            outcome,
            incoming_truncated,
            outgoing_truncated,
        })
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

    fn resolve_scope_keys(&self, seed: &ScopeSelector) -> Result<ScopeKeys> {
        if !seed.symbol_ids().is_empty() {
            return Ok(ScopeKeys::Symbols(seed.symbol_ids().to_vec()));
        }
        if seed.is_all() {
            return Ok(ScopeKeys::All);
        }
        let files = self
            .scoped_files(seed)?
            .into_iter()
            .map(|file| file.path)
            .collect::<Vec<_>>();
        Ok(ScopeKeys::Files(files))
    }

    fn query_plan_rows(&self, plan: &EdgeQueryPlan) -> Result<GraphOutcome<(String, String)>> {
        if plan.limit == 0 || plan.keys.is_empty() {
            return Ok(GraphOutcome::Empty);
        }
        classify_overfetch(self.query_edge_rows(plan), plan.limit)
    }

    fn query_edge_rows(&self, plan: &EdgeQueryPlan) -> Result<Vec<(String, String)>> {
        let Some(config) = &self.context().falkordb else {
            return Err(anyhow::Error::new(
                code_graph::GraphReadError::NotConfigured,
            ));
        };
        let (query, params) = plan.render(&self.context().project_id);
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

pub(super) fn classify_overfetch<T>(
    result: Result<Vec<T>>,
    limit: usize,
) -> Result<GraphOutcome<T>> {
    match result {
        Ok(rows) if rows.is_empty() => Ok(GraphOutcome::Empty),
        Ok(mut rows) if rows.len() > limit => {
            rows.truncate(limit);
            Ok(GraphOutcome::Truncated(rows))
        }
        Ok(rows) => Ok(GraphOutcome::Available(rows)),
        Err(error) => classify_query::<T>(Err(error), limit),
    }
}

fn take_bounded<T: Ord>(rows: &mut Vec<T>, limit: usize) -> bool {
    rows.sort();
    rows.dedup();
    let truncated = limit > 0 && rows.len() > limit;
    if limit == 0 {
        rows.clear();
        return false;
    }
    rows.truncate(limit);
    truncated
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn edge_queries_apply_scope_before_ordered_limit() {
        let plan = EdgeQueryPlan::new(
            GraphEdgeKind::Call,
            GraphDirection::Outgoing,
            GraphScopeMode::Closed,
            ScopeKeys::Files(vec!["src/a.rs".to_string(), "src/b.rs".to_string()]),
            7,
        );
        let (query, params) = plan.render("project-id");
        let where_position = query.find("WHERE").expect("query has scope predicate");
        let limit_position = query
            .find("LIMIT 8")
            .expect("query uses the sentinel fetch limit");
        assert!(where_position < limit_position);
        assert!(query.contains("'src/a.rs', 'src/b.rs'"));
        assert_eq!(params.len(), 1);
    }
}
