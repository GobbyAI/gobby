use crate::config::{CodeVectorSettings, Context};
use crate::models::{ProjectionMetadata, ProjectionProvenance};
use std::path::PathBuf;

use super::generation::generate_report_from_snapshot;
use super::render::render_markdown;
use super::types::{
    BridgeEdgeInput, GraphHotspot, ReportCodeEdge, ReportGraphSnapshot, ReportNode,
};
use super::*;

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
    assert_eq!(edge.metadata.provenance, ProjectionProvenance::Extracted);

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
        "EXTRACTED"
    );
}

#[test]
fn markdown_inline_code_uses_commonmark_backtick_delimiters() {
    let report = empty_report("project`1");
    let markdown = render_markdown(super::render::RenderMarkdownInput {
        project_id: &report.project_id,
        generated_at: &report.generated_at,
        summary: &report.summary,
        hotspots: &report.hotspots,
        unresolved_targets: &[TargetFrequency {
            id: "target".to_string(),
            name: "call`target".to_string(),
            count: 1,
        }],
        external_targets: &[],
        bridge_summary: None,
        degradation_details: &[],
        top_n: 10,
    });

    assert!(markdown.contains("- Project: ``project`1``"));
    assert!(markdown.contains("- ``call`target`` (1)"));
    assert!(!markdown.contains("\\`"));
}

#[test]
fn markdown_renders_high_degree_modules() {
    let mut report = empty_report("project-1");
    report.hotspots.high_degree_modules.push(GraphHotspot {
        id: "mod:api".to_string(),
        name: "api".to_string(),
        node_type: "module".to_string(),
        degree: 4,
        incoming: 1,
        outgoing: 3,
        file_path: None,
    });
    let markdown = render_markdown(super::render::RenderMarkdownInput {
        project_id: &report.project_id,
        generated_at: &report.generated_at,
        summary: &report.summary,
        hotspots: &report.hotspots,
        unresolved_targets: &[],
        external_targets: &[],
        bridge_summary: None,
        degradation_details: &[],
        top_n: 10,
    });

    assert!(markdown.contains("## High-degree modules"));
    assert!(markdown.contains("- `api` (degree 4, in 1, out 3)"));
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
            bridge_edges: BridgeEdgeInput::unavailable("bridge *edge* <timed out>"),
            ..ReportGraphSnapshot::default()
        },
    );

    assert_eq!(report.summary.node_count, 1);
    assert_eq!(report.degradation_details.len(), 1);
    assert_eq!(report.degradation_details[0].input, RELATES_TO_CODE);
    assert!(!report.degradation_details[0].required);
    assert!(
        report
            .markdown
            .contains("- `RELATES_TO_CODE`: bridge \\*edge\\* \\<timed out\\>")
    );
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
