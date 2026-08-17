//! Seed-scoped graph query plans.
//!
//! Relationship, direction, and scope predicates are applied before limits so a
//! small scope cannot be starved by a project-wide sample. Large identifier
//! lists are chunked so Cypher payloads stay bounded.

use std::collections::HashMap;

use crate::graph::typed_query;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum GraphEdgeKind {
    Call,
    Import,
    Inheritance,
}

pub const SCOPE_CHUNK_LEN: usize = 64;
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
    pub limit: usize,
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
            keys,
            limit: clamp_declared_limit(limit),
        }
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
             ORDER BY source, target LIMIT {limit}",
            match_clause = match_clause(self.kind),
            return_clause = return_clause(self.kind),
            limit = self.fetch_limit(),
        );
        (
            query,
            HashMap::from([(
                "project".to_string(),
                typed_query::cypher_string_literal(project_id),
            )]),
        )
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
        let target_in = endpoint_in_scope(&self.keys, &edge.target, &edge.target_file);
        match (self.direction, self.mode) {
            (GraphDirection::Outgoing, GraphScopeMode::Closed) => source_in && target_in,
            (GraphDirection::Outgoing, GraphScopeMode::Incident) => source_in,
            (GraphDirection::Incoming, GraphScopeMode::Closed) => source_in && target_in,
            (GraphDirection::Incoming, GraphScopeMode::Incident) => target_in && !source_in,
        }
    }

    fn predicates(&self) -> Vec<String> {
        match &self.keys {
            ScopeKeys::All => Vec::new(),
            keys if keys.is_empty() => vec!["false".to_string()],
            ScopeKeys::Files(files) => self.list_predicates(files, false),
            ScopeKeys::Symbols(ids) => self.list_predicates(ids, true),
        }
    }

    fn list_predicates(&self, values: &[String], symbols: bool) -> Vec<String> {
        let listed = typed_query::id_list_literal(values);
        let (source_field, target_field) = endpoint_fields(self.kind, symbols);
        match (self.direction, self.mode) {
            (GraphDirection::Outgoing, GraphScopeMode::Closed)
            | (GraphDirection::Incoming, GraphScopeMode::Closed) => vec![
                format!("{source_field} IN [{listed}]"),
                format!("{target_field} IN [{listed}]"),
            ],
            (GraphDirection::Outgoing, GraphScopeMode::Incident) => {
                vec![format!("{source_field} IN [{listed}]")]
            }
            (GraphDirection::Incoming, GraphScopeMode::Incident) => vec![
                format!("{target_field} IN [{listed}]"),
                format!("NOT {source_field} IN [{listed}]"),
            ],
        }
    }
}

pub fn clamp_declared_limit(limit: usize) -> usize {
    limit.min(MAX_DECLARED_EDGE_LIMIT)
}

pub fn plans_for(
    kind: GraphEdgeKind,
    bounds: GraphBounds,
    mode: GraphScopeMode,
    keys: ScopeKeys,
) -> Vec<EdgeQueryPlan> {
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
    plans
}

fn chunked_plans(
    kind: GraphEdgeKind,
    direction: GraphDirection,
    mode: GraphScopeMode,
    keys: ScopeKeys,
    limit: usize,
) -> Vec<EdgeQueryPlan> {
    keys.chunks(SCOPE_CHUNK_LEN)
        .into_iter()
        .map(|chunk| EdgeQueryPlan::new(kind, direction, mode, chunk, limit))
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
            "MATCH (source:CodeSymbol {project: $project})-[:CALLS]->\
             (target:CodeSymbol {project: $project})"
        }
        GraphEdgeKind::Import => {
            "MATCH (source:CodeFile {project: $project})-[:IMPORTS]->\
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
            "RETURN source.id AS source, target.id AS target"
        }
        GraphEdgeKind::Import => "RETURN source.path AS source, target.name AS target",
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

#[cfg(test)]
fn endpoint_in_scope(keys: &ScopeKeys, id: &str, file: &str) -> bool {
    match keys {
        ScopeKeys::All => true,
        ScopeKeys::Files(values) | ScopeKeys::Symbols(values) => {
            values.iter().any(|value| value == file || value == id)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample(
        kind: GraphEdgeKind,
        source: &str,
        target: &str,
        source_file: &str,
        target_file: &str,
    ) -> SampleGraphEdge {
        SampleGraphEdge {
            kind,
            source: source.to_string(),
            target: target.to_string(),
            source_file: source_file.to_string(),
            target_file: target_file.to_string(),
        }
    }

    fn crowded_project() -> Vec<SampleGraphEdge> {
        let mut edges = Vec::new();
        for index in 0..80 {
            edges.push(sample(
                GraphEdgeKind::Call,
                &format!("noise-src-{index}"),
                &format!("noise-dst-{index}"),
                "src/noise.rs",
                "src/other.rs",
            ));
            edges.push(sample(
                GraphEdgeKind::Import,
                "src/noise.rs",
                &format!("noise_mod_{index}"),
                "src/noise.rs",
                &format!("noise_mod_{index}"),
            ));
            edges.push(sample(
                GraphEdgeKind::Inheritance,
                &format!("noise-child-{index}"),
                &format!("noise-base-{index}"),
                "src/noise.rs",
                "src/other.rs",
            ));
        }
        edges.extend([
            sample(
                GraphEdgeKind::Call,
                "keep-caller",
                "keep-callee",
                "src/keep.rs",
                "src/keep.rs",
            ),
            sample(
                GraphEdgeKind::Import,
                "src/keep.rs",
                "keep_mod",
                "src/keep.rs",
                "keep_mod",
            ),
            sample(
                GraphEdgeKind::Inheritance,
                "keep-child",
                "keep-base",
                "src/keep.rs",
                "src/keep.rs",
            ),
        ]);
        edges
    }

    #[test]
    fn small_scopes_keep_in_scope_edges_when_project_volume_exceeds_limit() {
        let files = ScopeKeys::Files(vec!["src/keep.rs".to_string()]);
        let edges = crowded_project();
        for kind in [
            GraphEdgeKind::Call,
            GraphEdgeKind::Import,
            GraphEdgeKind::Inheritance,
        ] {
            let mode = if kind == GraphEdgeKind::Import {
                GraphScopeMode::Incident
            } else {
                GraphScopeMode::Closed
            };
            let plan = EdgeQueryPlan::new(kind, GraphDirection::Outgoing, mode, files.clone(), 5);
            let kept = plan.select(&edges);
            assert_eq!(kept.len(), 1, "{kind:?} should retain its in-scope edge");
            assert!(
                kept.iter().all(|edge| edge.source_file == "src/keep.rs"),
                "out-of-scope {kind:?} edges must not appear"
            );
        }
    }

    #[test]
    fn dense_scopes_return_stable_bounded_rows() {
        let mut edges = Vec::new();
        for index in 0..20 {
            edges.push(sample(
                GraphEdgeKind::Call,
                &format!("src-{index:02}"),
                &format!("dst-{index:02}"),
                "src/dense.rs",
                "src/dense.rs",
            ));
        }
        let plan = EdgeQueryPlan::new(
            GraphEdgeKind::Call,
            GraphDirection::Outgoing,
            GraphScopeMode::Closed,
            ScopeKeys::Files(vec!["src/dense.rs".to_string()]),
            7,
        );
        let first = plan
            .select(&edges)
            .into_iter()
            .map(|edge| (edge.source.clone(), edge.target.clone()))
            .collect::<Vec<_>>();
        let second = plan
            .select(&edges)
            .into_iter()
            .map(|edge| (edge.source.clone(), edge.target.clone()))
            .collect::<Vec<_>>();
        assert_eq!(first.len(), 7);
        assert_eq!(first, second);
        assert_eq!(first[0], ("src-00".to_string(), "dst-00".to_string()));
    }

    #[test]
    fn incoming_and_outgoing_limits_are_independent() {
        let mut edges = Vec::new();
        for index in 0..6 {
            edges.push(sample(
                GraphEdgeKind::Call,
                &format!("out-{index}"),
                "seed",
                "src/out.rs",
                "src/seed.rs",
            ));
            edges.push(sample(
                GraphEdgeKind::Call,
                "seed",
                &format!("in-target-{index}"),
                "src/seed.rs",
                "src/out.rs",
            ));
        }
        let keys = ScopeKeys::Files(vec!["src/seed.rs".to_string()]);
        let outgoing = EdgeQueryPlan::new(
            GraphEdgeKind::Call,
            GraphDirection::Outgoing,
            GraphScopeMode::Incident,
            keys.clone(),
            2,
        )
        .select(&edges);
        let incoming = EdgeQueryPlan::new(
            GraphEdgeKind::Call,
            GraphDirection::Incoming,
            GraphScopeMode::Incident,
            keys,
            3,
        )
        .select(&edges);
        assert_eq!(outgoing.len(), 2);
        assert_eq!(incoming.len(), 3);
        assert!(
            outgoing
                .iter()
                .all(|edge| edge.source_file == "src/seed.rs")
        );
        assert!(incoming.iter().all(|edge| {
            edge.target_file == "src/seed.rs" && edge.source_file != "src/seed.rs"
        }));
    }

    #[test]
    fn query_plans_apply_scope_before_fetch_limit_and_stay_chunk_bounded() {
        let files = (0..130)
            .map(|index| format!("src/f{index}.rs"))
            .collect::<Vec<_>>();
        let plans = plans_for(
            GraphEdgeKind::Call,
            GraphBounds::symmetric(4),
            GraphScopeMode::Incident,
            ScopeKeys::Files(files.clone()),
        );
        assert!(plans.len() >= 4);
        for plan in &plans {
            let (query, params) = plan.render("project-id");
            let where_at = query.find("WHERE").expect("scoped query has WHERE");
            let limit_at = query
                .find(&format!("LIMIT {}", plan.fetch_limit()))
                .expect("query uses the sentinel fetch limit");
            assert!(where_at < limit_at);
            assert!(query.contains("ORDER BY source, target"));
            assert!(query.contains("file_path IN ["));
            assert!(query.len() < 8_000);
            assert_eq!(params.len(), 1);
            match &plan.keys {
                ScopeKeys::Files(chunk) => assert!(chunk.len() <= SCOPE_CHUNK_LEN),
                other => panic!("expected file chunk, got {other:?}"),
            }
        }
        assert!(
            plans
                .iter()
                .any(|plan| plan.render("project-id").0.contains(&files[0]))
        );
    }

    #[test]
    fn inheritance_queries_use_hierarchy_relationships() {
        let plan = EdgeQueryPlan::new(
            GraphEdgeKind::Inheritance,
            GraphDirection::Outgoing,
            GraphScopeMode::Closed,
            ScopeKeys::Symbols(vec!["child".to_string(), "base".to_string()]),
            9,
        );
        let (query, _) = plan.render("project-id");
        assert!(query.contains("INHERITS|EXTENDS|IMPLEMENTS"));
        assert!(query.contains("source.id IN ["));
        assert!(query.contains("target.id IN ["));
        assert!(query.contains("LIMIT 10"));
    }

    #[test]
    fn unscoped_plans_stay_project_bounded_without_identifier_lists() {
        let plan = EdgeQueryPlan::new(
            GraphEdgeKind::Call,
            GraphDirection::Outgoing,
            GraphScopeMode::Incident,
            ScopeKeys::All,
            11,
        );
        let (query, _) = plan.render("project-id");
        assert!(!query.contains(" IN ["));
        assert!(query.contains("LIMIT 12"));
    }
}
