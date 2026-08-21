use anyhow::Result;
use std::collections::HashSet;

use gobby_core::degradation::ServiceState;
use gobby_core::falkor::Row;

use crate::graph::code_graph;
use crate::models::GraphResult;

use super::graph_query::{
    self, EdgeQueryPlan, PublicEdge, QueryPlans, ScopeKeys, default_rel, plans_for,
};
use super::{CodewikiFacts, ScopeSelector};

pub use super::graph_query::{
    GraphBounds, GraphDirection, GraphEdgeKind, GraphScopeMode, MAX_DECLARED_EDGE_LIMIT,
};

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
    pub rel: String,
    pub source_kind: String,
    pub target_kind: String,
    pub source_name: String,
    pub target_name: String,
    pub source_file: String,
    pub target_file: String,
    pub owner_path: String,
    pub owner_hash: String,
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
            .scoped_edges(seed, kind, GraphBounds::outgoing(limit), mode, None)?
            .outcome)
    }

    pub fn scoped_edges(
        &self,
        seed: &ScopeSelector,
        kind: GraphEdgeKind,
        bounds: GraphBounds,
        mode: GraphScopeMode,
        exclude: Option<&HashSet<PublicEdge>>,
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
        let empty_exclude = HashSet::new();
        let exclude = exclude.unwrap_or(&empty_exclude);
        let mut incoming_rows = Vec::new();
        let mut outgoing_rows = Vec::new();
        for plan in plans {
            match self.fetch_eligible_rows(&plan, exclude)? {
                GraphOutcome::Unavailable { reason } => {
                    return Ok(ScopedGraph {
                        outcome: GraphOutcome::Unavailable { reason },
                        incoming_truncated: false,
                        outgoing_truncated: false,
                    });
                }
                GraphOutcome::Empty => {}
                GraphOutcome::Available(rows) | GraphOutcome::Truncated(rows) => {
                    match plan.direction {
                        GraphDirection::Incoming => incoming_rows.extend(rows),
                        GraphDirection::Outgoing => outgoing_rows.extend(rows),
                    }
                }
            }
        }
        let incoming_limit = graph_query::clamp_declared_limit(bounds.incoming_limit);
        let outgoing_limit = graph_query::clamp_declared_limit(bounds.outgoing_limit);
        let incoming_truncated = take_bounded(&mut incoming_rows, incoming_limit);
        let outgoing_truncated = take_bounded(&mut outgoing_rows, outgoing_limit);
        let mut rows = outgoing_rows;
        rows.extend(incoming_rows);
        rows.sort();
        rows.dedup();
        let truncated = incoming_truncated || outgoing_truncated;
        let edges = rows
            .into_iter()
            .map(|edge| GraphEdge {
                source: edge.source,
                target: edge.target,
                kind,
                rel: edge.rel,
                source_kind: edge.source_kind,
                target_kind: edge.target_kind,
                source_name: edge.source_name,
                target_name: edge.target_name,
                source_file: edge.source_file,
                target_file: edge.target_file,
                owner_path: edge.owner_path,
                owner_hash: edge.owner_hash,
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
        if !seed.endpoint_files().is_empty() || !seed.endpoint_modules().is_empty() {
            return Ok(ScopeKeys::Endpoints {
                files: seed.endpoint_files().to_vec(),
                modules: seed.endpoint_modules().to_vec(),
            });
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

    fn fetch_eligible_rows(
        &self,
        plan: &EdgeQueryPlan,
        exclude: &HashSet<PublicEdge>,
    ) -> Result<GraphOutcome<FetchedEdge>> {
        if plan.limit == 0 || plan.keys.is_empty() {
            return Ok(GraphOutcome::Empty);
        }
        let mut after = None;
        let mut eligible = Vec::new();
        let mut seen = HashSet::new();
        loop {
            let page = plan.clone().with_after(after.clone());
            let rows = match self.query_edge_rows(&page) {
                Ok(rows) => rows,
                Err(error) => return classify_query::<FetchedEdge>(Err(error), plan.limit),
            };
            if rows.is_empty() {
                break;
            }
            let raw_count = rows.len();
            for row in rows {
                after = Some((row.source.clone(), row.target.clone(), row.rel.clone()));
                if graph_query::incident_incoming_source_in_frontier(
                    plan,
                    &row.source,
                    &row.source_file,
                ) {
                    continue;
                }
                let pair = row.public();
                if !seen.insert(pair.clone()) || graph_query::edge_is_excluded(exclude, &pair) {
                    continue;
                }
                eligible.push(row);
                if eligible.len() >= plan.fetch_limit() {
                    return Ok(GraphOutcome::Available(eligible));
                }
            }
            if raw_count < plan.fetch_limit() {
                break;
            }
        }
        Ok(if eligible.is_empty() {
            GraphOutcome::Empty
        } else {
            GraphOutcome::Available(eligible)
        })
    }

    fn query_edge_rows(&self, plan: &EdgeQueryPlan) -> Result<Vec<FetchedEdge>> {
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
            Ok((Some(rows), ServiceState::Available)) => Ok(rows_to_fetched(plan.kind, &rows)),
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

#[derive(Clone, Debug, Eq)]
struct FetchedEdge {
    source: String,
    target: String,
    rel: String,
    source_file: String,
    target_file: String,
    source_kind: String,
    target_kind: String,
    source_name: String,
    target_name: String,
    owner_path: String,
    owner_hash: String,
}

impl FetchedEdge {
    fn public(&self) -> PublicEdge {
        PublicEdge::new(&self.source, &self.target, &self.rel)
    }
}

impl PartialEq for FetchedEdge {
    fn eq(&self, other: &Self) -> bool {
        self.public() == other.public()
    }
}

impl PartialOrd for FetchedEdge {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for FetchedEdge {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.public().cmp(&other.public())
    }
}

fn rows_to_fetched(kind: GraphEdgeKind, rows: &[Row]) -> Vec<FetchedEdge> {
    rows.iter()
        .filter_map(|row| {
            let source = row.get("source").and_then(|value| value.as_str())?;
            let target = row.get("target").and_then(|value| value.as_str())?;
            let rel = row
                .get("rel")
                .and_then(|value| value.as_str())
                .unwrap_or_else(|| default_rel(kind));
            let source_file = row
                .get("source_file")
                .and_then(|value| value.as_str())
                .unwrap_or("");
            let target_file = row
                .get("target_file")
                .and_then(|value| value.as_str())
                .unwrap_or("");
            let source_kind = row
                .get("source_kind")
                .and_then(|value| value.as_str())
                .unwrap_or("symbol");
            let target_kind = row
                .get("target_kind")
                .and_then(|value| value.as_str())
                .unwrap_or("symbol");
            let source_name = row
                .get("source_name")
                .and_then(|value| value.as_str())
                .unwrap_or(source);
            let target_name = row
                .get("target_name")
                .and_then(|value| value.as_str())
                .unwrap_or(target);
            let owner_path = row
                .get("owner_path")
                .and_then(|value| value.as_str())
                .unwrap_or(source_file);
            let owner_hash = row
                .get("owner_hash")
                .and_then(|value| value.as_str())
                .unwrap_or("");
            Some(FetchedEdge {
                source: source.to_string(),
                target: target.to_string(),
                rel: rel.to_string(),
                source_file: source_file.to_string(),
                target_file: target_file.to_string(),
                source_kind: source_kind.to_string(),
                target_kind: target_kind.to_string(),
                source_name: source_name.to_string(),
                target_name: target_name.to_string(),
                owner_path: owner_path.to_string(),
                owner_hash: owner_hash.to_string(),
            })
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

    #[test]
    fn rows_to_fetched_leaves_missing_endpoint_file_empty() {
        let mut row = Row::new();
        row.insert("source".to_string(), serde_json::json!("sym-1"));
        row.insert("target".to_string(), serde_json::json!("ext-1"));
        row.insert("rel".to_string(), serde_json::json!("CALLS"));
        row.insert("source_file".to_string(), serde_json::json!("src/a.py"));
        row.insert("target_file".to_string(), serde_json::Value::Null);
        let fetched = rows_to_fetched(GraphEdgeKind::Call, &[row]);
        assert_eq!(fetched.len(), 1);
        assert_eq!(fetched[0].source_file, "src/a.py");
        assert_eq!(fetched[0].target_file, "");
        assert_eq!(fetched[0].owner_path, "src/a.py");

        let mut bare = Row::new();
        bare.insert("source".to_string(), serde_json::json!("sym-1"));
        bare.insert("target".to_string(), serde_json::json!("sym-2"));
        let fetched = rows_to_fetched(GraphEdgeKind::Inheritance, &[bare]);
        assert_eq!(fetched[0].source_file, "");
        assert_eq!(fetched[0].target_file, "");
        assert_eq!(fetched[0].owner_path, "");
        assert_eq!(fetched[0].rel, "INHERITS");
    }
}
