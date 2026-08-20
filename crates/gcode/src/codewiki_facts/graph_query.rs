//! Seed-scoped graph query plans.
//!
//! Relationship, direction, and scope predicates are applied before limits so a
//! small scope cannot be starved by a project-wide sample. Large identifier
//! lists are chunked so Cypher payloads stay bounded.

use std::collections::{HashMap, HashSet};

use crate::graph::typed_query;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum GraphEdgeKind {
    Call,
    Import,
    Inheritance,
}

pub const SCOPE_CHUNK_LEN: usize = 64;
pub const MAX_CLOSED_SCOPE_CHUNKS: usize = 8;
pub const MAX_DECLARED_EDGE_LIMIT: usize = 10_000;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum GraphDirection {
    Incoming,
    Outgoing,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct GraphBounds {
    pub incoming_limit: usize,
    pub outgoing_limit: usize,
}

impl GraphBounds {
    pub fn outgoing(limit: usize) -> Self {
        Self {
            incoming_limit: 0,
            outgoing_limit: limit,
        }
    }

    pub fn symmetric(limit: usize) -> Self {
        Self {
            incoming_limit: limit,
            outgoing_limit: limit,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum GraphScopeMode {
    /// Both endpoints must fall inside the resolved seed set.
    Closed,
    /// At least the seed-side endpoint must fall inside the resolved seed set.
    Incident,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ScopeKeys {
    All,
    Files(Vec<String>),
    Symbols(Vec<String>),
}

impl ScopeKeys {
    pub fn is_empty(&self) -> bool {
        match self {
            Self::All => false,
            Self::Files(values) | Self::Symbols(values) => values.is_empty(),
        }
    }

    pub fn chunks(&self, chunk_len: usize) -> Vec<Self> {
        let chunk_len = chunk_len.max(1);
        match self {
            Self::All => vec![Self::All],
            Self::Files(values) => chunk_values(values, chunk_len)
                .into_iter()
                .map(Self::Files)
                .collect(),
            Self::Symbols(values) => chunk_values(values, chunk_len)
                .into_iter()
                .map(Self::Symbols)
                .collect(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EdgeQueryPlan {
    pub kind: GraphEdgeKind,
    pub direction: GraphDirection,
    pub mode: GraphScopeMode,
    pub keys: ScopeKeys,
    pub peer_keys: ScopeKeys,
    pub limit: usize,
    pub after: Option<(String, String)>,
}

#[cfg(test)]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SampleGraphEdge {
    pub kind: GraphEdgeKind,
    pub source: String,
    pub target: String,
    pub source_file: String,
    pub target_file: String,
}

impl EdgeQueryPlan {
    pub fn new(
        kind: GraphEdgeKind,
        direction: GraphDirection,
        mode: GraphScopeMode,
        keys: ScopeKeys,
        limit: usize,
    ) -> Self {
        Self {
            kind,
            direction,
            mode,
            keys: keys.clone(),
            peer_keys: keys,
            limit: clamp_declared_limit(limit),
            after: None,
        }
    }

    pub fn with_after(mut self, after: Option<(String, String)>) -> Self {
        self.after = after;
        self
    }

    fn with_peer_keys(mut self, peer_keys: ScopeKeys) -> Self {
        self.peer_keys = peer_keys;
        self
    }

    pub fn fetch_limit(&self) -> usize {
        self.limit.saturating_add(1)
    }

    pub fn render(&self, project_id: &str) -> (String, HashMap<String, String>) {
        let predicates = self.predicates();
        let where_clause = if predicates.is_empty() {
            String::new()
        } else {
            format!(" WHERE {}", predicates.join(" AND "))
        };
        let query = format!(
            "{match_clause}{where_clause} {return_clause} \
             ORDER BY source, target, rel LIMIT {limit}",
            match_clause = match_clause(self.kind),
            return_clause = return_clause(self.kind),
            limit = self.fetch_limit(),
        );
        let mut params = HashMap::from([(
            "project".to_string(),
            typed_query::cypher_string_literal(project_id),
        )]);
        if let Some((after_source, after_target)) = &self.after {
            params.insert(
                "after_source".to_string(),
                typed_query::cypher_string_literal(after_source),
            );
            params.insert(
                "after_target".to_string(),
                typed_query::cypher_string_literal(after_target),
            );
        }
        (query, params)
    }

    #[cfg(test)]
    pub fn select<'a>(&self, edges: &'a [SampleGraphEdge]) -> Vec<&'a SampleGraphEdge> {
        let mut kept = edges
            .iter()
            .filter(|edge| edge.kind == self.kind && self.keeps(edge))
            .collect::<Vec<_>>();
        kept.sort_by(|left, right| {
            left.source
                .cmp(&right.source)
                .then_with(|| left.target.cmp(&right.target))
        });
        kept.truncate(self.limit);
        kept
    }

    #[cfg(test)]
    fn keeps(&self, edge: &SampleGraphEdge) -> bool {
        let source_in = endpoint_in_scope(&self.keys, &edge.source, &edge.source_file);
        let target_in = endpoint_in_scope(&self.peer_keys, &edge.target, &edge.target_file);
        let source_in_frontier =
            endpoint_in_scope(&self.peer_keys, &edge.source, &edge.source_file);
        match (self.direction, self.mode) {
            (GraphDirection::Outgoing, GraphScopeMode::Closed) => source_in && target_in,
            (GraphDirection::Outgoing, GraphScopeMode::Incident) => source_in,
            (GraphDirection::Incoming, GraphScopeMode::Closed) => source_in && target_in,
            (GraphDirection::Incoming, GraphScopeMode::Incident) => {
                target_in && !source_in_frontier
            }
        }
    }

    fn predicates(&self) -> Vec<String> {
        let mut predicates = match &self.keys {
            ScopeKeys::All => Vec::new(),
            keys if keys.is_empty() || self.peer_keys.is_empty() => vec!["false".to_string()],
            ScopeKeys::Files(files) => self.list_predicates(files, false),
            ScopeKeys::Symbols(ids) => self.list_predicates(ids, true),
        };
        if self.after.is_some() && predicates.first().map(String::as_str) != Some("false") {
            predicates.push(
                "(source > $after_source OR (source = $after_source AND target > $after_target))"
                    .to_string(),
            );
        }
        predicates
    }

    fn list_predicates(&self, values: &[String], symbols: bool) -> Vec<String> {
        let listed = typed_query::id_list_literal(values);
        let (source_field, target_field) = endpoint_fields(self.kind, symbols);
        match (self.direction, self.mode) {
            (GraphDirection::Outgoing, GraphScopeMode::Closed)
            | (GraphDirection::Incoming, GraphScopeMode::Closed) => {
                let peer_listed = match &self.peer_keys {
                    ScopeKeys::Files(peer) | ScopeKeys::Symbols(peer) => {
                        typed_query::id_list_literal(peer)
                    }
                    ScopeKeys::All => listed.clone(),
                };
                vec![
                    format!("{source_field} IN [{listed}]"),
                    format!("{target_field} IN [{peer_listed}]"),
                ]
            }
            (GraphDirection::Outgoing, GraphScopeMode::Incident) => {
                vec![format!("{source_field} IN [{listed}]")]
            }
            (GraphDirection::Incoming, GraphScopeMode::Incident) => {
                let mut predicates = vec![format!("{target_field} IN [{listed}]")];
                if self.can_push_frontier_exclusion() {
                    let frontier_listed = match &self.peer_keys {
                        ScopeKeys::Files(peer) | ScopeKeys::Symbols(peer) => {
                            typed_query::id_list_literal(peer)
                        }
                        ScopeKeys::All => listed.clone(),
                    };
                    predicates.push(format!("NOT {source_field} IN [{frontier_listed}]"));
                }
                predicates
            }
        }
    }

    fn can_push_frontier_exclusion(&self) -> bool {
        match &self.peer_keys {
            ScopeKeys::All => false,
            ScopeKeys::Files(values) | ScopeKeys::Symbols(values) => {
                !values.is_empty() && values.len() <= SCOPE_CHUNK_LEN
            }
        }
    }
}

pub fn clamp_declared_limit(limit: usize) -> usize {
    limit.min(MAX_DECLARED_EDGE_LIMIT)
}

#[derive(Clone, Debug, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub struct PublicEdge {
    pub source: String,
    pub target: String,
    pub rel: String,
}

impl PublicEdge {
    pub fn new(
        source: impl Into<String>,
        target: impl Into<String>,
        rel: impl Into<String>,
    ) -> Self {
        Self {
            source: source.into(),
            target: target.into(),
            rel: rel.into(),
        }
    }
}

pub fn default_rel(kind: GraphEdgeKind) -> &'static str {
    match kind {
        GraphEdgeKind::Call => "CALLS",
        GraphEdgeKind::Import => "IMPORTS",
        GraphEdgeKind::Inheritance => "INHERITS",
    }
}

pub fn edge_is_excluded(exclude: &HashSet<PublicEdge>, edge: &PublicEdge) -> bool {
    exclude.contains(edge)
}

pub fn incident_incoming_source_in_frontier(
    plan: &EdgeQueryPlan,
    source: &str,
    source_file: &str,
) -> bool {
    plan.direction == GraphDirection::Incoming
        && plan.mode == GraphScopeMode::Incident
        && endpoint_in_scope(&plan.peer_keys, source, source_file)
}

#[cfg(test)]
impl SampleGraphEdge {
    fn rel(&self) -> &'static str {
        default_rel(self.kind)
    }
}

#[cfg(test)]
pub fn collect_scoped_pairs(
    edges: &[SampleGraphEdge],
    kind: GraphEdgeKind,
    bounds: GraphBounds,
    mode: GraphScopeMode,
    keys: ScopeKeys,
    exclude: &HashSet<PublicEdge>,
) -> (Vec<PublicEdge>, bool, bool) {
    let plans = match plans_for(kind, bounds, mode, keys) {
        QueryPlans::Ready(plans) => plans,
        QueryPlans::ClosedScopeTooLarge { .. } => {
            return (
                Vec::new(),
                bounds.incoming_limit > 0,
                bounds.outgoing_limit > 0,
            );
        }
    };
    let mut incoming = Vec::new();
    let mut outgoing = Vec::new();
    for plan in plans {
        let mut seen = HashSet::new();
        let mut eligible = Vec::new();
        let mut ranked = edges
            .iter()
            .filter(|edge| edge.kind == plan.kind && plan.keeps(edge))
            .collect::<Vec<_>>();
        ranked.sort_by(|left, right| {
            left.source
                .cmp(&right.source)
                .then_with(|| left.target.cmp(&right.target))
                .then_with(|| left.rel().cmp(right.rel()))
        });
        for edge in ranked {
            let pair = PublicEdge::new(edge.source.clone(), edge.target.clone(), edge.rel());
            if !seen.insert(pair.clone()) {
                continue;
            }
            if edge_is_excluded(exclude, &pair) {
                continue;
            }
            eligible.push(pair);
            if eligible.len() == plan.fetch_limit() {
                break;
            }
        }
        match plan.direction {
            GraphDirection::Incoming => incoming.extend(eligible),
            GraphDirection::Outgoing => outgoing.extend(eligible),
        }
    }
    incoming.sort();
    incoming.dedup();
    outgoing.sort();
    outgoing.dedup();
    let incoming_limit = clamp_declared_limit(bounds.incoming_limit);
    let outgoing_limit = clamp_declared_limit(bounds.outgoing_limit);
    let incoming_truncated = incoming_limit > 0 && incoming.len() > incoming_limit;
    let outgoing_truncated = outgoing_limit > 0 && outgoing.len() > outgoing_limit;
    if incoming_limit > 0 {
        incoming.truncate(incoming_limit);
    } else {
        incoming.clear();
    }
    if outgoing_limit > 0 {
        outgoing.truncate(outgoing_limit);
    } else {
        outgoing.clear();
    }
    let mut rows = outgoing;
    rows.extend(incoming);
    rows.sort();
    rows.dedup();
    (rows, incoming_truncated, outgoing_truncated)
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum QueryPlans {
    Ready(Vec<EdgeQueryPlan>),
    ClosedScopeTooLarge { chunk_count: usize },
}

pub fn plans_for(
    kind: GraphEdgeKind,
    bounds: GraphBounds,
    mode: GraphScopeMode,
    keys: ScopeKeys,
) -> QueryPlans {
    if mode == GraphScopeMode::Closed {
        let chunk_count = keys.chunks(SCOPE_CHUNK_LEN).len();
        if chunk_count > MAX_CLOSED_SCOPE_CHUNKS {
            return QueryPlans::ClosedScopeTooLarge { chunk_count };
        }
    }
    let mut plans = Vec::new();
    if bounds.outgoing_limit > 0 {
        plans.extend(chunked_plans(
            kind,
            GraphDirection::Outgoing,
            mode,
            keys.clone(),
            bounds.outgoing_limit,
        ));
    }
    if bounds.incoming_limit > 0 {
        plans.extend(chunked_plans(
            kind,
            GraphDirection::Incoming,
            mode,
            keys,
            bounds.incoming_limit,
        ));
    }
    QueryPlans::Ready(plans)
}

fn chunked_plans(
    kind: GraphEdgeKind,
    direction: GraphDirection,
    mode: GraphScopeMode,
    keys: ScopeKeys,
    limit: usize,
) -> Vec<EdgeQueryPlan> {
    let chunks = keys.chunks(SCOPE_CHUNK_LEN);
    if mode == GraphScopeMode::Closed {
        let mut plans = Vec::new();
        for source in &chunks {
            for target in &chunks {
                plans.push(
                    EdgeQueryPlan::new(kind, direction, mode, source.clone(), limit)
                        .with_peer_keys(target.clone()),
                );
            }
        }
        return plans;
    }
    chunks
        .into_iter()
        .map(|chunk| {
            EdgeQueryPlan::new(kind, direction, mode, chunk, limit).with_peer_keys(keys.clone())
        })
        .collect()
}

fn chunk_values(values: &[String], chunk_len: usize) -> Vec<Vec<String>> {
    if values.is_empty() {
        return Vec::new();
    }
    values
        .chunks(chunk_len)
        .map(|chunk| chunk.to_vec())
        .collect()
}

fn match_clause(kind: GraphEdgeKind) -> &'static str {
    match kind {
        GraphEdgeKind::Call => {
            "MATCH (source:CodeSymbol {project: $project})-[r:CALLS]->\
             (target:CodeSymbol {project: $project})"
        }
        GraphEdgeKind::Import => {
            "MATCH (source:CodeFile {project: $project})-[r:IMPORTS]->\
             (target:CodeModule {project: $project})"
        }
        GraphEdgeKind::Inheritance => {
            "MATCH (source:CodeSymbol {project: $project})-[r:INHERITS|EXTENDS|IMPLEMENTS]->\
             (target:CodeSymbol {project: $project})"
        }
    }
}

fn return_clause(kind: GraphEdgeKind) -> &'static str {
    match kind {
        GraphEdgeKind::Call | GraphEdgeKind::Inheritance => {
            "RETURN DISTINCT source.id AS source, target.id AS target, type(r) AS rel, \
             source.file_path AS source_file, target.file_path AS target_file"
        }
        GraphEdgeKind::Import => {
            "RETURN DISTINCT source.path AS source, target.name AS target, type(r) AS rel, \
             source.path AS source_file, target.name AS target_file"
        }
    }
}

fn endpoint_fields(kind: GraphEdgeKind, symbols: bool) -> (&'static str, &'static str) {
    if symbols {
        return ("source.id", "target.id");
    }
    match kind {
        GraphEdgeKind::Import => ("source.path", "target.name"),
        GraphEdgeKind::Call | GraphEdgeKind::Inheritance => {
            ("source.file_path", "target.file_path")
        }
    }
}

fn endpoint_in_scope(keys: &ScopeKeys, id: &str, file: &str) -> bool {
    match keys {
        ScopeKeys::All => true,
        ScopeKeys::Files(values) | ScopeKeys::Symbols(values) => {
            values.iter().any(|value| value == file || value == id)
        }
    }
}

#[cfg(test)]
#[path = "graph_query/tests.rs"]
mod tests;
