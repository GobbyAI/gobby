use serde::{Deserialize, Serialize};

use crate::models::ProjectionMetadata;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BridgeEdgeHypothesis {
    pub source_id: String,
    pub target_symbol_id: String,
    pub relation: String,
    pub label: String,
    pub metadata: ProjectionMetadata,
}

impl BridgeEdgeHypothesis {
    pub fn new(
        source_id: impl Into<String>,
        target_symbol_id: impl Into<String>,
        relation: impl Into<String>,
        metadata: ProjectionMetadata,
    ) -> Self {
        let label = if metadata.is_hypothesis() {
            "inferred hypothesis"
        } else {
            "extracted fact"
        };
        Self {
            source_id: source_id.into(),
            target_symbol_id: target_symbol_id.into(),
            relation: relation.into(),
            label: label.to_string(),
            metadata,
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
    pub files: usize,
    pub symbols: usize,
    pub imports: usize,
    pub calls: usize,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub bridge_edges: Vec<BridgeEdgeHypothesis>,
}

pub fn empty_report(project_id: impl Into<String>) -> ProjectGraphReport {
    ProjectGraphReport {
        project_id: project_id.into(),
        files: 0,
        symbols: 0,
        imports: 0,
        calls: 0,
        bridge_edges: vec![],
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::ProjectionProvenance;

    #[test]
    fn bridge_edges_are_hypotheses() {
        let edge = BridgeEdgeHypothesis::inferred(
            "memory-1",
            "symbol-1",
            "RELATES_TO_CODE",
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
