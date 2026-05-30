use std::collections::{BTreeMap, HashMap};
use std::fmt;
use std::time::{SystemTime, UNIX_EPOCH};

use gobby_core::degradation::ServiceState;
use gobby_core::falkor::{GraphClient, Row};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::config::Context;
use crate::graph::typed_query;
use crate::models::{ProjectionMetadata, ProjectionProvenance};

const RELATES_TO_CODE: &str = "RELATES_TO_CODE";
const DEFAULT_TOP_LIMIT: usize = 10;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BridgeEdgeHypothesis {
    pub source_id: String,
    pub target_symbol_id: String,
    pub relation: String,
    pub label: String,
    pub read_only: bool,
    pub metadata: ProjectionMetadata,
}

impl BridgeEdgeHypothesis {
    pub fn new(
        source_id: impl Into<String>,
        target_symbol_id: impl Into<String>,
        relation: impl Into<String>,
        metadata: ProjectionMetadata,
    ) -> Self {
        Self {
            source_id: source_id.into(),
            target_symbol_id: target_symbol_id.into(),
            relation: relation.into(),
            label: "inferred hypothesis".to_string(),
            read_only: true,
            metadata: inferred_bridge_metadata(metadata),
        }
    }

    pub fn inferred(
        source_id: impl Into<String>,
        target_symbol_id: impl Into<String>,
        relation: impl Into<String>,
        source_system: impl Into<String>,
        confidence: Option<f64>,
    ) -> Self {
        Self::new(
            source_id,
            target_symbol_id,
            relation,
            ProjectionMetadata::inferred(source_system, confidence),
        )
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProjectGraphReport {
    pub project_id: String,
    pub generated_at: String,
    pub summary: GraphReportSummary,
    pub hotspots: GraphReportHotspots,
    pub unresolved_targets: Vec<TargetFrequency>,
    pub external_targets: Vec<TargetFrequency>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bridge_summary: Option<BridgeReportSummary>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub bridge_edges: Vec<BridgeEdgeHypothesis>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub degradation_details: Vec<ReportDegradation>,
    pub suggested_investigation_questions: Vec<String>,
    pub markdown: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProjectGraphReportOptions {
    pub top_n: usize,
}

impl Default for ProjectGraphReportOptions {
    fn default() -> Self {
        Self {
            top_n: DEFAULT_TOP_LIMIT,
        }
    }
}

impl ProjectGraphReportOptions {
    fn normalized(self) -> Self {
        Self {
            top_n: self.top_n.max(1),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GraphReportSummary {
    pub node_count: usize,
    pub edge_count: usize,
    pub node_counts_by_type: BTreeMap<String, usize>,
    pub code_edge_counts: BTreeMap<String, usize>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GraphReportHotspots {
    pub high_degree_files: Vec<GraphHotspot>,
    pub high_degree_symbols: Vec<GraphHotspot>,
    pub high_degree_modules: Vec<GraphHotspot>,
    pub incoming_call_hotspots: Vec<GraphHotspot>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GraphHotspot {
    pub id: String,
    pub name: String,
    #[serde(rename = "type")]
    pub node_type: String,
    pub degree: usize,
    pub incoming: usize,
    pub outgoing: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub file_path: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TargetFrequency {
    pub id: String,
    pub name: String,
    pub count: usize,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BridgeReportSummary {
    pub relation: String,
    pub edge_count: usize,
    pub inferred: bool,
    pub read_only: bool,
    pub source_system_counts: Vec<NamedCount>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub confidence_range: Option<ConfidenceRange>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NamedCount {
    pub name: String,
    pub count: usize,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ConfidenceRange {
    pub min: f64,
    pub max: f64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReportDegradation {
    pub input: String,
    pub required: bool,
    pub detail: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProjectGraphReportError {
    GraphServiceNotConfigured,
    GraphServiceUnreachable { message: String },
    GraphQueryFailed { message: String },
}

impl fmt::Display for ProjectGraphReportError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::GraphServiceNotConfigured => {
                f.write_str("FalkorDB is not configured; project graph report requires FalkorDB")
            }
            Self::GraphServiceUnreachable { message } => write!(
                f,
                "FalkorDB is unreachable; project graph report requires FalkorDB: {message}"
            ),
            Self::GraphQueryFailed { message } => {
                write!(f, "project graph report query failed: {message}")
            }
        }
    }
}

impl std::error::Error for ProjectGraphReportError {}

#[derive(Debug, Clone, Default, PartialEq)]
struct ReportGraphSnapshot {
    nodes: Vec<ReportNode>,
    code_edges: Vec<ReportCodeEdge>,
    summary: Option<GraphReportSummary>,
    hotspots: Option<GraphReportHotspots>,
    unresolved_targets: Option<Vec<TargetFrequency>>,
    external_targets: Option<Vec<TargetFrequency>>,
    bridge_edges: BridgeEdgeInput,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ReportNode {
    id: String,
    name: String,
    node_type: String,
    file_path: Option<String>,
}

impl ReportNode {
    #[cfg(test)]
    fn new(id: impl Into<String>, name: impl Into<String>, node_type: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            name: name.into(),
            node_type: node_type.into(),
            file_path: None,
        }
    }

    #[cfg(test)]
    fn with_file_path(mut self, file_path: impl Into<String>) -> Self {
        self.file_path = Some(file_path.into());
        self
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ReportCodeEdge {
    source: String,
    target: String,
    edge_type: String,
}

impl ReportCodeEdge {
    #[cfg(test)]
    fn new(
        source: impl Into<String>,
        target: impl Into<String>,
        edge_type: impl Into<String>,
    ) -> Self {
        Self {
            source: source.into(),
            target: target.into(),
            edge_type: edge_type.into(),
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
enum BridgeEdgeInput {
    Available(Vec<BridgeEdgeHypothesis>),
    Unavailable(String),
}

impl BridgeEdgeInput {
    fn available(edges: Vec<BridgeEdgeHypothesis>) -> Self {
        Self::Available(edges)
    }

    fn unavailable(reason: impl Into<String>) -> Self {
        Self::Unavailable(reason.into())
    }
}

impl Default for BridgeEdgeInput {
    fn default() -> Self {
        Self::Available(vec![])
    }
}

#[derive(Debug, Clone, Copy, Default)]
struct DegreeStats {
    incoming: usize,
    outgoing: usize,
}

pub fn generate_report(ctx: &Context) -> Result<ProjectGraphReport, ProjectGraphReportError> {
    generate_report_with_options(ctx, ProjectGraphReportOptions::default())
}

pub fn generate_report_with_options(
    ctx: &Context,
    options: ProjectGraphReportOptions,
) -> Result<ProjectGraphReport, ProjectGraphReportError> {
    let Some(config) = ctx.falkordb.as_ref() else {
        return Err(ProjectGraphReportError::GraphServiceNotConfigured);
    };

    let connection_config = config.connection_config();
    let options = options.normalized();
    let result = gobby_core::falkor::with_graph(
        Some(&connection_config),
        &config.graph_name,
        ReportGraphSnapshot::default(),
        |client| load_report_snapshot(client, &ctx.project_id, options.top_n),
    );

    match result {
        Ok((snapshot, ServiceState::Available)) => Ok(generate_report_from_snapshot_with_options(
            &ctx.project_id,
            now_iso8601(),
            snapshot,
            options,
        )),
        Ok((_, ServiceState::NotConfigured)) => {
            Err(ProjectGraphReportError::GraphServiceNotConfigured)
        }
        Ok((_, ServiceState::Unreachable { message })) => {
            Err(ProjectGraphReportError::GraphServiceUnreachable { message })
        }
        Err(error) => Err(ProjectGraphReportError::GraphQueryFailed {
            message: error.to_string(),
        }),
    }
}

pub fn empty_report(project_id: impl Into<String>) -> ProjectGraphReport {
    generate_report_from_snapshot(project_id, now_iso8601(), ReportGraphSnapshot::default())
}

fn generate_report_from_snapshot(
    project_id: impl Into<String>,
    generated_at: impl Into<String>,
    snapshot: ReportGraphSnapshot,
) -> ProjectGraphReport {
    generate_report_from_snapshot_with_options(
        project_id,
        generated_at,
        snapshot,
        ProjectGraphReportOptions::default(),
    )
}

fn generate_report_from_snapshot_with_options(
    project_id: impl Into<String>,
    generated_at: impl Into<String>,
    snapshot: ReportGraphSnapshot,
    options: ProjectGraphReportOptions,
) -> ProjectGraphReport {
    let options = options.normalized();
    let project_id = project_id.into();
    let generated_at = generated_at.into();
    let node_by_id = snapshot
        .nodes
        .iter()
        .map(|node| (node.id.as_str(), node))
        .collect::<HashMap<_, _>>();

    let summary = snapshot
        .summary
        .clone()
        .unwrap_or_else(|| summarize_graph(&snapshot.nodes, &snapshot.code_edges));
    let hotspots = snapshot.hotspots.clone().unwrap_or_else(|| {
        summarize_hotspots(&snapshot.nodes, &snapshot.code_edges, options.top_n)
    });
    let unresolved_targets = snapshot.unresolved_targets.clone().unwrap_or_else(|| {
        target_frequencies(
            &snapshot.code_edges,
            &node_by_id,
            "unresolved",
            options.top_n,
        )
    });
    let external_targets = snapshot.external_targets.clone().unwrap_or_else(|| {
        target_frequencies(&snapshot.code_edges, &node_by_id, "external", options.top_n)
    });

    let (bridge_edges, mut degradation_details) = match snapshot.bridge_edges {
        BridgeEdgeInput::Available(edges) => (normalize_bridge_edges(edges), vec![]),
        BridgeEdgeInput::Unavailable(reason) => (
            vec![],
            vec![ReportDegradation {
                input: RELATES_TO_CODE.to_string(),
                required: false,
                detail: reason,
            }],
        ),
    };
    let bridge_summary = summarize_bridge_edges(&bridge_edges);
    let suggested_investigation_questions = suggested_questions(
        &hotspots,
        &unresolved_targets,
        &external_targets,
        bridge_summary.as_ref(),
        &degradation_details,
    );
    let markdown = render_markdown(RenderMarkdownInput {
        project_id: &project_id,
        generated_at: &generated_at,
        summary: &summary,
        hotspots: &hotspots,
        unresolved_targets: &unresolved_targets,
        external_targets: &external_targets,
        bridge_summary: bridge_summary.as_ref(),
        degradation_details: &degradation_details,
        top_n: options.top_n,
    });

    degradation_details.sort_by(|left, right| left.input.cmp(&right.input));

    ProjectGraphReport {
        project_id,
        generated_at,
        summary,
        hotspots,
        unresolved_targets,
        external_targets,
        bridge_summary,
        bridge_edges,
        degradation_details,
        suggested_investigation_questions,
        markdown,
    }
}

fn load_report_snapshot(
    client: &mut GraphClient,
    project_id: &str,
    top_n: usize,
) -> anyhow::Result<ReportGraphSnapshot> {
    let (query, params) = report_node_counts_query(project_id);
    let node_counts_by_type = rows_to_named_counts(client.query(&query, Some(params))?);
    let node_count = node_counts_by_type.values().sum();

    let (query, params) = report_code_edge_counts_query(project_id);
    let code_edge_counts = rows_to_named_counts(client.query(&query, Some(params))?);
    let edge_count = code_edge_counts.values().sum();

    let summary = GraphReportSummary {
        node_count,
        edge_count,
        node_counts_by_type,
        code_edge_counts,
    };

    let hotspots = GraphReportHotspots {
        high_degree_files: load_hotspots(client, project_id, "file", top_n)?,
        high_degree_symbols: load_hotspots(client, project_id, "symbol", top_n)?,
        high_degree_modules: load_hotspots(client, project_id, "module", top_n)?,
        incoming_call_hotspots: load_incoming_call_hotspots(client, project_id, top_n)?,
    };

    let unresolved_targets = load_target_frequencies(client, project_id, "unresolved", top_n)?;
    let external_targets = load_target_frequencies(client, project_id, "external", top_n)?;

    let (query, params) = report_bridge_edges_query(project_id);
    let bridge_edges = match client.query(&query, Some(params)) {
        Ok(rows) => BridgeEdgeInput::available(
            rows.iter()
                .filter_map(row_to_bridge_edge_hypothesis)
                .collect(),
        ),
        Err(error) => BridgeEdgeInput::unavailable(format!("bridge edge query failed: {error}")),
    };

    Ok(ReportGraphSnapshot {
        nodes: vec![],
        code_edges: vec![],
        summary: Some(summary),
        hotspots: Some(hotspots),
        unresolved_targets: Some(unresolved_targets),
        external_targets: Some(external_targets),
        bridge_edges,
    })
}

fn report_node_type_case(alias: &str) -> String {
    format!(
        "CASE \
          WHEN {alias}:CodeFile THEN 'file' \
          WHEN {alias}:CodeModule THEN 'module' \
          WHEN {alias}:CodeSymbol THEN coalesce({alias}.kind, 'symbol') \
          WHEN {alias}:UnresolvedCallee THEN 'unresolved' \
          WHEN {alias}:ExternalSymbol THEN 'external' \
          ELSE 'node' \
        END"
    )
}

fn report_node_id_expr(alias: &str) -> String {
    format!("coalesce({alias}.id, {alias}.path, {alias}.name)")
}

fn report_node_name_expr(alias: &str) -> String {
    format!("coalesce({alias}.name, {alias}.path, {alias}.id)")
}

fn report_node_counts_query(project_id: &str) -> (String, HashMap<String, String>) {
    (
        "MATCH (n {project: $project}) \
         WHERE n:CodeFile OR n:CodeSymbol OR n:CodeModule OR n:UnresolvedCallee OR n:ExternalSymbol \
         RETURN CASE \
                  WHEN n:CodeFile THEN 'file' \
                  WHEN n:CodeModule THEN 'module' \
                  WHEN n:CodeSymbol THEN coalesce(n.kind, 'symbol') \
                  WHEN n:UnresolvedCallee THEN 'unresolved' \
                  WHEN n:ExternalSymbol THEN 'external' \
                  ELSE 'node' \
                END AS name, \
                count(n) AS count"
            .to_string(),
        typed_query::string_params(&[("project", project_id)]),
    )
}

fn report_code_edge_counts_query(project_id: &str) -> (String, HashMap<String, String>) {
    (
        "MATCH (source {project: $project})-[r]->(target {project: $project}) \
         WHERE type(r) IN ['DEFINES', 'IMPORTS', 'CALLS'] \
         RETURN type(r) AS name, count(r) AS count"
            .to_string(),
        typed_query::string_params(&[("project", project_id)]),
    )
}

fn report_hotspots_query(
    project_id: &str,
    node_class: &str,
    top_n: usize,
) -> (String, HashMap<String, String>) {
    let predicate = match node_class {
        "file" => "n:CodeFile",
        "module" => "n:CodeModule",
        _ => "n:CodeSymbol",
    };
    let limit = top_n.max(1);
    (
        format!(
            "MATCH (n {{project: $project}}) \
             WHERE {predicate} \
             OPTIONAL MATCH (n)-[out]->(out_target {{project: $project}}) \
             WHERE type(out) IN ['DEFINES', 'IMPORTS', 'CALLS'] \
               AND (out_target:CodeFile OR out_target:CodeSymbol OR out_target:CodeModule OR out_target:UnresolvedCallee OR out_target:ExternalSymbol) \
             WITH n, count(out) AS outgoing \
             OPTIONAL MATCH (in_source {{project: $project}})-[inc]->(n) \
             WHERE type(inc) IN ['DEFINES', 'IMPORTS', 'CALLS'] \
               AND (in_source:CodeFile OR in_source:CodeSymbol OR in_source:CodeModule OR in_source:UnresolvedCallee OR in_source:ExternalSymbol) \
             WITH n, outgoing, count(inc) AS incoming \
             WITH n, outgoing, incoming, outgoing + incoming AS degree \
             WHERE degree > 0 \
             RETURN {} AS id, {} AS name, {} AS node_type, degree, incoming, outgoing, coalesce(n.file_path, n.path) AS file_path \
             ORDER BY degree DESC, name ASC, id ASC \
             LIMIT {limit}",
            report_node_id_expr("n"),
            report_node_name_expr("n"),
            report_node_type_case("n")
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

fn report_incoming_call_hotspots_query(
    project_id: &str,
    top_n: usize,
) -> (String, HashMap<String, String>) {
    let limit = top_n.max(1);
    (
        format!(
            "MATCH (:CodeSymbol {{project: $project}})-[r:CALLS]->(n:CodeSymbol {{project: $project}}) \
             WITH n, count(r) AS incoming \
             WHERE incoming > 0 \
             RETURN n.id AS id, coalesce(n.name, n.id) AS name, {} AS node_type, incoming AS degree, incoming, 0 AS outgoing, n.file_path AS file_path \
             ORDER BY degree DESC, name ASC, id ASC \
             LIMIT {limit}",
            report_node_type_case("n")
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

fn report_target_frequencies_query(
    project_id: &str,
    target_type: &str,
    top_n: usize,
) -> (String, HashMap<String, String>) {
    let target_label = if target_type == "external" {
        "ExternalSymbol"
    } else {
        "UnresolvedCallee"
    };
    let limit = top_n.max(1);
    (
        format!(
            "MATCH (:CodeSymbol {{project: $project}})-[r:CALLS]->(target:{target_label} {{project: $project}}) \
             RETURN target.id AS id, coalesce(target.name, target.id) AS name, count(r) AS count \
             ORDER BY count DESC, name ASC, id ASC \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

fn report_bridge_edges_query(project_id: &str) -> (String, HashMap<String, String>) {
    (
        "MATCH (source)-[r:RELATES_TO_CODE]->(target:CodeSymbol {project: $project}) \
         RETURN coalesce(source.id, source.uuid, source.name) AS source_id, \
                target.id AS target_symbol_id, \
                'RELATES_TO_CODE' AS relation, \
                r.provenance AS provenance, \
                r.confidence AS confidence, \
                coalesce(r.source_system, 'gobby-memory') AS source_system, \
                r.source_file_path AS source_file_path, \
                r.source_line AS source_line, \
                r.source_symbol_id AS source_symbol_id, \
                r.matching_method AS matching_method"
            .to_string(),
        typed_query::string_params(&[("project", project_id)]),
    )
}

fn rows_to_named_counts(rows: Vec<Row>) -> BTreeMap<String, usize> {
    rows.iter()
        .filter_map(|row| {
            let name = row_string(row, &["name"])?;
            let count = row_usize(row, &["count"]).unwrap_or(0);
            Some((name, count))
        })
        .collect()
}

fn load_hotspots(
    client: &mut GraphClient,
    project_id: &str,
    node_class: &str,
    top_n: usize,
) -> anyhow::Result<Vec<GraphHotspot>> {
    let (query, params) = report_hotspots_query(project_id, node_class, top_n);
    Ok(client
        .query(&query, Some(params))?
        .iter()
        .filter_map(row_to_graph_hotspot)
        .collect())
}

fn load_incoming_call_hotspots(
    client: &mut GraphClient,
    project_id: &str,
    top_n: usize,
) -> anyhow::Result<Vec<GraphHotspot>> {
    let (query, params) = report_incoming_call_hotspots_query(project_id, top_n);
    Ok(client
        .query(&query, Some(params))?
        .iter()
        .filter_map(row_to_graph_hotspot)
        .collect())
}

fn load_target_frequencies(
    client: &mut GraphClient,
    project_id: &str,
    target_type: &str,
    top_n: usize,
) -> anyhow::Result<Vec<TargetFrequency>> {
    let (query, params) = report_target_frequencies_query(project_id, target_type, top_n);
    Ok(client
        .query(&query, Some(params))?
        .iter()
        .filter_map(row_to_target_frequency)
        .collect())
}

fn row_to_graph_hotspot(row: &Row) -> Option<GraphHotspot> {
    Some(GraphHotspot {
        id: row_string(row, &["id"])?,
        name: row_string(row, &["name"])?,
        node_type: row_string(row, &["node_type"]).unwrap_or_else(|| "node".to_string()),
        degree: row_usize(row, &["degree"]).unwrap_or(0),
        incoming: row_usize(row, &["incoming"]).unwrap_or(0),
        outgoing: row_usize(row, &["outgoing"]).unwrap_or(0),
        file_path: row_string(row, &["file_path"]),
    })
}

fn row_to_target_frequency(row: &Row) -> Option<TargetFrequency> {
    Some(TargetFrequency {
        id: row_string(row, &["id"])?,
        name: row_string(row, &["name"])?,
        count: row_usize(row, &["count"]).unwrap_or(0),
    })
}

fn row_to_bridge_edge_hypothesis(row: &Row) -> Option<BridgeEdgeHypothesis> {
    let source_id = row_string(row, &["source_id"])?;
    let target_symbol_id = row_string(row, &["target_symbol_id"])?;
    let relation = row_string(row, &["relation"]).unwrap_or_else(|| RELATES_TO_CODE.to_string());
    let source_system =
        row_string(row, &["source_system"]).unwrap_or_else(|| "gobby-memory".to_string());

    let mut metadata = ProjectionMetadata::new(
        row_string(row, &["provenance"])
            .and_then(|value| ProjectionProvenance::from_wire_value(&value))
            .unwrap_or(ProjectionProvenance::Inferred),
        source_system,
    );
    metadata.confidence = row_f64(row, &["confidence"]);
    metadata.source_file_path = row_string(row, &["source_file_path"]);
    metadata.source_line = row_usize(row, &["source_line"]);
    metadata.source_symbol_id = row_string(row, &["source_symbol_id"]);
    metadata.matching_method = row_string(row, &["matching_method"]);

    Some(BridgeEdgeHypothesis::new(
        source_id,
        target_symbol_id,
        relation,
        metadata,
    ))
}

fn summarize_graph(nodes: &[ReportNode], edges: &[ReportCodeEdge]) -> GraphReportSummary {
    let mut node_counts_by_type = BTreeMap::new();
    for node in nodes {
        *node_counts_by_type
            .entry(node.node_type.clone())
            .or_insert(0) += 1;
    }

    let mut code_edge_counts = BTreeMap::new();
    for edge in edges {
        *code_edge_counts.entry(edge.edge_type.clone()).or_insert(0) += 1;
    }

    GraphReportSummary {
        node_count: nodes.len(),
        edge_count: edges.len(),
        node_counts_by_type,
        code_edge_counts,
    }
}

fn summarize_hotspots(
    nodes: &[ReportNode],
    edges: &[ReportCodeEdge],
    top_n: usize,
) -> GraphReportHotspots {
    let mut degree = HashMap::<&str, DegreeStats>::new();
    let mut incoming_calls = HashMap::<&str, usize>::new();
    for edge in edges {
        degree.entry(&edge.source).or_default().outgoing += 1;
        degree.entry(&edge.target).or_default().incoming += 1;
        if edge.edge_type == "CALLS" {
            *incoming_calls.entry(&edge.target).or_insert(0) += 1;
        }
    }

    GraphReportHotspots {
        high_degree_files: top_hotspots(nodes, &degree, top_n, |node| node.node_type == "file"),
        high_degree_symbols: top_hotspots(nodes, &degree, top_n, |node| {
            is_symbol_node(&node.node_type)
        }),
        high_degree_modules: top_hotspots(nodes, &degree, top_n, |node| node.node_type == "module"),
        incoming_call_hotspots: top_incoming_call_hotspots(nodes, &incoming_calls, top_n),
    }
}

fn top_hotspots(
    nodes: &[ReportNode],
    degree: &HashMap<&str, DegreeStats>,
    top_n: usize,
    include: impl Fn(&ReportNode) -> bool,
) -> Vec<GraphHotspot> {
    let mut hotspots = nodes
        .iter()
        .filter(|node| include(node))
        .filter_map(|node| {
            let stats = degree.get(node.id.as_str())?;
            let total = stats.incoming + stats.outgoing;
            (total > 0).then(|| GraphHotspot {
                id: node.id.clone(),
                name: node.name.clone(),
                node_type: node.node_type.clone(),
                degree: total,
                incoming: stats.incoming,
                outgoing: stats.outgoing,
                file_path: node.file_path.clone(),
            })
        })
        .collect::<Vec<_>>();
    sort_hotspots(&mut hotspots);
    hotspots.truncate(top_n);
    hotspots
}

fn top_incoming_call_hotspots(
    nodes: &[ReportNode],
    incoming_calls: &HashMap<&str, usize>,
    top_n: usize,
) -> Vec<GraphHotspot> {
    let mut hotspots = nodes
        .iter()
        .filter(|node| is_symbol_node(&node.node_type))
        .filter_map(|node| {
            let incoming = incoming_calls.get(node.id.as_str()).copied().unwrap_or(0);
            (incoming > 0).then(|| GraphHotspot {
                id: node.id.clone(),
                name: node.name.clone(),
                node_type: node.node_type.clone(),
                degree: incoming,
                incoming,
                outgoing: 0,
                file_path: node.file_path.clone(),
            })
        })
        .collect::<Vec<_>>();
    sort_hotspots(&mut hotspots);
    hotspots.truncate(top_n);
    hotspots
}

fn target_frequencies(
    edges: &[ReportCodeEdge],
    node_by_id: &HashMap<&str, &ReportNode>,
    target_type: &str,
    top_n: usize,
) -> Vec<TargetFrequency> {
    let mut counts = BTreeMap::<String, TargetFrequency>::new();
    for edge in edges.iter().filter(|edge| edge.edge_type == "CALLS") {
        let Some(node) = node_by_id.get(edge.target.as_str()) else {
            continue;
        };
        if node.node_type != target_type {
            continue;
        }
        let entry = counts
            .entry(node.id.clone())
            .or_insert_with(|| TargetFrequency {
                id: node.id.clone(),
                name: node.name.clone(),
                count: 0,
            });
        entry.count += 1;
    }

    let mut frequencies = counts.into_values().collect::<Vec<_>>();
    frequencies.sort_by(|left, right| {
        right
            .count
            .cmp(&left.count)
            .then_with(|| left.name.cmp(&right.name))
            .then_with(|| left.id.cmp(&right.id))
    });
    frequencies.truncate(top_n);
    frequencies
}

fn summarize_bridge_edges(edges: &[BridgeEdgeHypothesis]) -> Option<BridgeReportSummary> {
    if edges.is_empty() {
        return None;
    }

    let mut source_counts = BTreeMap::<String, usize>::new();
    let mut confidence_min = f64::INFINITY;
    let mut confidence_max = f64::NEG_INFINITY;
    let mut has_confidence = false;
    for edge in edges {
        *source_counts
            .entry(edge.metadata.source_system.clone())
            .or_insert(0) += 1;
        if let Some(confidence) = edge.metadata.confidence
            && confidence.is_finite()
        {
            confidence_min = confidence_min.min(confidence);
            confidence_max = confidence_max.max(confidence);
            has_confidence = true;
        }
    }

    let source_system_counts = source_counts
        .into_iter()
        .map(|(name, count)| NamedCount { name, count })
        .collect();

    Some(BridgeReportSummary {
        relation: RELATES_TO_CODE.to_string(),
        edge_count: edges.len(),
        inferred: true,
        read_only: true,
        source_system_counts,
        confidence_range: has_confidence.then_some(ConfidenceRange {
            min: confidence_min,
            max: confidence_max,
        }),
    })
}

fn normalize_bridge_edges(edges: Vec<BridgeEdgeHypothesis>) -> Vec<BridgeEdgeHypothesis> {
    edges
        .into_iter()
        .map(|edge| {
            BridgeEdgeHypothesis::new(
                edge.source_id,
                edge.target_symbol_id,
                edge.relation,
                edge.metadata,
            )
        })
        .collect()
}

fn suggested_questions(
    hotspots: &GraphReportHotspots,
    unresolved_targets: &[TargetFrequency],
    external_targets: &[TargetFrequency],
    bridge_summary: Option<&BridgeReportSummary>,
    degradation_details: &[ReportDegradation],
) -> Vec<String> {
    let mut questions =
        vec!["Which high-degree files or symbols should be reviewed before refactors?".to_string()];

    if !hotspots.incoming_call_hotspots.is_empty() {
        questions.push("Which incoming-call hotspots define the largest blast radius?".to_string());
    }
    if !unresolved_targets.is_empty() || !external_targets.is_empty() {
        questions.push(
            "Which unresolved or external call targets should be resolved first?".to_string(),
        );
    }
    if bridge_summary.is_some() {
        questions
            .push("Which inferred RELATES_TO_CODE bridges need human confirmation?".to_string());
    }
    if !degradation_details.is_empty() {
        questions.push(
            "Which degraded optional inputs should be restored for the next report?".to_string(),
        );
    }

    questions
}

struct RenderMarkdownInput<'a> {
    project_id: &'a str,
    generated_at: &'a str,
    summary: &'a GraphReportSummary,
    hotspots: &'a GraphReportHotspots,
    unresolved_targets: &'a [TargetFrequency],
    external_targets: &'a [TargetFrequency],
    bridge_summary: Option<&'a BridgeReportSummary>,
    degradation_details: &'a [ReportDegradation],
    top_n: usize,
}

fn render_markdown(input: RenderMarkdownInput<'_>) -> String {
    let mut lines = vec![
        "# Project Graph Report".to_string(),
        String::new(),
        format!("- Project: {}", input.project_id),
        format!("- Generated: {}", input.generated_at),
        format!("- Nodes: {}", input.summary.node_count),
        format!("- Edges: {}", input.summary.edge_count),
    ];

    if !input.summary.code_edge_counts.is_empty() {
        lines.push(format!(
            "- Code edges: {}",
            named_counts_inline(&input.summary.code_edge_counts)
        ));
    }

    append_hotspot_section(
        &mut lines,
        "High-degree files",
        &input.hotspots.high_degree_files,
        input.top_n,
    );
    append_hotspot_section(
        &mut lines,
        "High-degree symbols",
        &input.hotspots.high_degree_symbols,
        input.top_n,
    );
    append_hotspot_section(
        &mut lines,
        "Incoming-call hotspots",
        &input.hotspots.incoming_call_hotspots,
        input.top_n,
    );
    append_target_section(
        &mut lines,
        "Unresolved call targets",
        input.unresolved_targets,
        input.top_n,
    );
    append_target_section(
        &mut lines,
        "External call targets",
        input.external_targets,
        input.top_n,
    );

    if let Some(summary) = input.bridge_summary {
        lines.push(String::new());
        lines.push("RELATES_TO_CODE bridges".to_string());
        lines.push(format!(
            "- {} inferred read-only edge(s)",
            summary.edge_count
        ));
        if let Some(range) = &summary.confidence_range {
            lines.push(format!("- Confidence: {:.3}..{:.3}", range.min, range.max));
        }
    }

    if !input.degradation_details.is_empty() {
        lines.push(String::new());
        lines.push("Degradation".to_string());
        for detail in input.degradation_details {
            lines.push(format!("- {}: {}", detail.input, detail.detail));
        }
    }

    lines.join("\n")
}

fn append_hotspot_section(
    lines: &mut Vec<String>,
    title: &str,
    hotspots: &[GraphHotspot],
    top_n: usize,
) {
    if hotspots.is_empty() {
        return;
    }
    lines.push(String::new());
    lines.push(title.to_string());
    for hotspot in hotspots.iter().take(top_n) {
        lines.push(format!(
            "- {} ({}, degree {})",
            hotspot.name, hotspot.node_type, hotspot.degree
        ));
    }
}

fn append_target_section(
    lines: &mut Vec<String>,
    title: &str,
    targets: &[TargetFrequency],
    top_n: usize,
) {
    if targets.is_empty() {
        return;
    }
    lines.push(String::new());
    lines.push(title.to_string());
    for target in targets.iter().take(top_n) {
        lines.push(format!("- {} ({})", target.name, target.count));
    }
}

fn named_counts_inline(counts: &BTreeMap<String, usize>) -> String {
    counts
        .iter()
        .map(|(name, count)| format!("{name}={count}"))
        .collect::<Vec<_>>()
        .join(", ")
}

fn sort_hotspots(hotspots: &mut [GraphHotspot]) {
    hotspots.sort_by(|left, right| {
        right
            .degree
            .cmp(&left.degree)
            .then_with(|| left.name.cmp(&right.name))
            .then_with(|| left.id.cmp(&right.id))
    });
}

fn is_symbol_node(node_type: &str) -> bool {
    !matches!(node_type, "file" | "module" | "unresolved" | "external")
}

fn inferred_bridge_metadata(mut metadata: ProjectionMetadata) -> ProjectionMetadata {
    metadata.provenance = ProjectionProvenance::Inferred;
    metadata
}

fn row_string(row: &Row, keys: &[&str]) -> Option<String> {
    keys.iter()
        .find_map(|key| row.get(*key).and_then(Value::as_str))
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn row_usize(row: &Row, keys: &[&str]) -> Option<usize> {
    keys.iter()
        .find_map(|key| row.get(*key))
        .and_then(|value| {
            value
                .as_u64()
                .or_else(|| value.as_i64().and_then(|value| value.try_into().ok()))
        })
        .map(|value| value as usize)
}

fn row_f64(row: &Row, keys: &[&str]) -> Option<f64> {
    keys.iter()
        .find_map(|key| row.get(*key))
        .and_then(Value::as_f64)
}

fn now_iso8601() -> String {
    let dur = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let secs = dur.as_secs();
    let micros = dur.subsec_micros();

    let (year, month, day) = days_to_ymd(secs / 86400);
    let daytime = secs % 86400;
    let hour = daytime / 3600;
    let minute = (daytime % 3600) / 60;
    let second = daytime % 60;

    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{micros:06}+00:00")
}

fn days_to_ymd(days: u64) -> (u64, u64, u64) {
    let z = days as i64 + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = (z - era * 146097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if month <= 2 { y + 1 } else { y };
    (year as u64, month, day)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{CodeVectorSettings, Context};
    use crate::models::{ProjectionMetadata, ProjectionProvenance};
    use std::path::PathBuf;

    #[test]
    fn report_shape() {
        let snapshot = ReportGraphSnapshot {
            nodes: vec![
                ReportNode::new("src/lib.rs", "src/lib.rs", "file"),
                ReportNode::new("mod:api", "api", "module"),
                ReportNode::new("sym:handler", "handler", "function").with_file_path("src/lib.rs"),
                ReportNode::new("sym:parse", "parse", "function").with_file_path("src/lib.rs"),
                ReportNode::new("unresolved:do_work", "do_work", "unresolved"),
                ReportNode::new("external:serde_json", "serde_json", "external"),
            ],
            code_edges: vec![
                ReportCodeEdge::new("src/lib.rs", "sym:handler", "DEFINES"),
                ReportCodeEdge::new("src/lib.rs", "mod:api", "IMPORTS"),
                ReportCodeEdge::new("sym:handler", "sym:parse", "CALLS"),
                ReportCodeEdge::new("sym:parse", "unresolved:do_work", "CALLS"),
                ReportCodeEdge::new("sym:handler", "external:serde_json", "CALLS"),
            ],
            bridge_edges: BridgeEdgeInput::available(vec![BridgeEdgeHypothesis::inferred(
                "memory-1",
                "sym:handler",
                RELATES_TO_CODE,
                "gobby-memory",
                Some(0.72),
            )]),
            ..ReportGraphSnapshot::default()
        };

        let report = generate_report_from_snapshot("project-1", "2026-05-28T00:00:00Z", snapshot);
        let json = serde_json::to_value(&report).expect("report serializes");

        assert_eq!(json["project_id"], "project-1");
        assert_eq!(json["summary"]["node_count"], 6);
        assert_eq!(json["summary"]["edge_count"], 5);
        assert_eq!(json["summary"]["code_edge_counts"]["CALLS"], 3);
        assert_eq!(json["hotspots"]["high_degree_files"][0]["id"], "src/lib.rs");
        assert_eq!(
            json["hotspots"]["incoming_call_hotspots"][0]["id"],
            "sym:parse"
        );
        assert_eq!(json["unresolved_targets"][0]["name"], "do_work");
        assert_eq!(json["external_targets"][0]["name"], "serde_json");
        assert_eq!(json["bridge_summary"]["relation"], RELATES_TO_CODE);
        assert_eq!(json["bridge_summary"]["confidence_range"]["min"], 0.72);
        assert!(json["markdown"].as_str().unwrap().contains("project-1"));
        assert!(
            !json["suggested_investigation_questions"]
                .as_array()
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn bridge_edges_are_read_only() {
        let edge = BridgeEdgeHypothesis::new(
            "memory-1",
            "symbol-1",
            RELATES_TO_CODE,
            ProjectionMetadata::gcode_extracted(),
        );

        assert!(edge.read_only);
        assert_eq!(edge.label, "inferred hypothesis");
        assert_eq!(edge.metadata.provenance, ProjectionProvenance::Inferred);

        let snapshot = ReportGraphSnapshot {
            nodes: vec![ReportNode::new("symbol-1", "handler", "function")],
            code_edges: vec![],
            bridge_edges: BridgeEdgeInput::available(vec![edge]),
            ..ReportGraphSnapshot::default()
        };
        let report = generate_report_from_snapshot("project-1", "2026-05-28T00:00:00Z", snapshot);
        let json = serde_json::to_value(&report).expect("report serializes");

        assert_eq!(json["bridge_edges"][0]["read_only"], true);
        assert_eq!(
            json["bridge_edges"][0]["metadata"]["provenance"],
            "INFERRED"
        );
    }

    #[test]
    fn report_degradation_contract() {
        let ctx = Context {
            database_url: "postgresql://localhost/unavailable".to_string(),
            project_root: PathBuf::from("/tmp/project"),
            project_id: "project-1".to_string(),
            quiet: true,
            falkordb: None,
            qdrant: None,
            embedding: None,
            code_vectors: CodeVectorSettings::default(),
            daemon_url: None,
            index_scope: crate::config::ProjectIndexScope::Single,
        };
        let err = generate_report(&ctx).expect_err("missing graph service is required");
        assert_eq!(err, ProjectGraphReportError::GraphServiceNotConfigured);

        let report = generate_report_from_snapshot(
            "project-1",
            "2026-05-28T00:00:00Z",
            ReportGraphSnapshot {
                nodes: vec![ReportNode::new("symbol-1", "handler", "function")],
                code_edges: vec![],
                bridge_edges: BridgeEdgeInput::unavailable("bridge edge query timed out"),
                ..ReportGraphSnapshot::default()
            },
        );

        assert_eq!(report.summary.node_count, 1);
        assert_eq!(report.degradation_details.len(), 1);
        assert_eq!(report.degradation_details[0].input, RELATES_TO_CODE);
        assert!(!report.degradation_details[0].required);
    }

    #[test]
    fn bridge_edges_are_hypotheses() {
        let edge = BridgeEdgeHypothesis::inferred(
            "memory-1",
            "symbol-1",
            RELATES_TO_CODE,
            "gobby-memory",
            Some(0.72),
        );

        assert_eq!(edge.label, "inferred hypothesis");
        assert_eq!(edge.metadata.provenance, ProjectionProvenance::Inferred);
        assert!(edge.metadata.is_hypothesis());

        let mut report = empty_report("project-1");
        report.bridge_edges.push(edge);

        let json = serde_json::to_value(&report).expect("report serializes");
        assert_eq!(json["bridge_edges"][0]["label"], "inferred hypothesis");
        assert_eq!(
            json["bridge_edges"][0]["metadata"]["provenance"],
            "INFERRED"
        );
    }
}
