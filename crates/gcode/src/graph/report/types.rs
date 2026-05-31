use std::collections::BTreeMap;
use std::fmt;

use serde::{Deserialize, Serialize};

use crate::graph::report::DEFAULT_TOP_LIMIT;
use crate::models::{ProjectionMetadata, ProjectionProvenance};

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
    pub(super) fn normalized(self) -> Self {
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
pub(super) struct ReportGraphSnapshot {
    pub(super) nodes: Vec<ReportNode>,
    pub(super) code_edges: Vec<ReportCodeEdge>,
    pub(super) summary: Option<GraphReportSummary>,
    pub(super) hotspots: Option<GraphReportHotspots>,
    pub(super) unresolved_targets: Option<Vec<TargetFrequency>>,
    pub(super) external_targets: Option<Vec<TargetFrequency>>,
    pub(super) bridge_edges: BridgeEdgeInput,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct ReportNode {
    pub(super) id: String,
    pub(super) name: String,
    pub(super) node_type: String,
    pub(super) file_path: Option<String>,
}

impl ReportNode {
    #[cfg(test)]
    pub(super) fn new(
        id: impl Into<String>,
        name: impl Into<String>,
        node_type: impl Into<String>,
    ) -> Self {
        Self {
            id: id.into(),
            name: name.into(),
            node_type: node_type.into(),
            file_path: None,
        }
    }

    #[cfg(test)]
    pub(super) fn with_file_path(mut self, file_path: impl Into<String>) -> Self {
        self.file_path = Some(file_path.into());
        self
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct ReportCodeEdge {
    pub(super) source: String,
    pub(super) target: String,
    pub(super) edge_type: String,
}

impl ReportCodeEdge {
    #[cfg(test)]
    pub(super) fn new(
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
pub(super) enum BridgeEdgeInput {
    Available(Vec<BridgeEdgeHypothesis>),
    #[cfg(test)]
    Unavailable(String),
}

impl BridgeEdgeInput {
    pub(super) fn available(edges: Vec<BridgeEdgeHypothesis>) -> Self {
        Self::Available(edges)
    }

    #[cfg(test)]
    pub(super) fn unavailable(reason: impl Into<String>) -> Self {
        Self::Unavailable(reason.into())
    }
}

impl Default for BridgeEdgeInput {
    fn default() -> Self {
        Self::Available(vec![])
    }
}

fn inferred_bridge_metadata(mut metadata: ProjectionMetadata) -> ProjectionMetadata {
    metadata.provenance = ProjectionProvenance::Inferred;
    metadata
}
