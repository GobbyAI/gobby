use crate::config::{CodeVectorSettings, Context};
use crate::models::{ProjectionMetadata, ProjectionProvenance};
use std::path::PathBuf;

use super::generation::generate_report_from_snapshot;
use super::render::render_markdown;
use super::summary::{summarize_bridge_edges, summarize_hotspots};
use super::types::{
    BridgeEdgeInput, BridgeReportSummary, ConfidenceRange, GraphHotspot, GraphReportHotspots,
    NamedCount, ReportCodeEdge, ReportGraphSnapshot, ReportNode,
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
fn graph_report_hotspots_use_shared_centrality_degree() {
    let nodes = vec![
        ReportNode::new("src/lib.rs", "src/lib.rs", "file"),
        ReportNode::new("sym:handler", "handler", "function").with_file_path("src/lib.rs"),
    ];
    let edges = vec![
        ReportCodeEdge::new("src/lib.rs", "sym:handler", "DEFINES"),
        ReportCodeEdge::new("src/lib.rs", "sym:handler", "DEFINES"),
    ];

    let hotspots = summarize_hotspots(&nodes, &edges, DEFAULT_TOP_LIMIT);

    assert_eq!(hotspots.high_degree_files[0].degree, 1);
    assert_eq!(hotspots.high_degree_files[0].outgoing, 2);
    assert_eq!(hotspots.high_degree_symbols[0].degree, 1);
    assert_eq!(hotspots.high_degree_symbols[0].incoming, 2);
}

#[test]
fn graph_report_hotspots_and_bridge_summary_match_pinned_output() {
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

    let bridge_edges = match snapshot.bridge_edges.clone() {
        BridgeEdgeInput::Available(edges) => edges,
        BridgeEdgeInput::Unavailable(_) => vec![],
    };

    assert_eq!(
        summarize_hotspots(&snapshot.nodes, &snapshot.code_edges, DEFAULT_TOP_LIMIT),
        expected_graph_hotspots()
    );
    assert_eq!(
        summarize_bridge_edges(&bridge_edges),
        Some(expected_bridge_summary())
    );
}

fn expected_graph_hotspots() -> GraphReportHotspots {
    GraphReportHotspots {
        high_degree_files: vec![GraphHotspot {
            id: "src/lib.rs".to_string(),
            name: "src/lib.rs".to_string(),
            node_type: "file".to_string(),
            degree: 2,
            incoming: 0,
            outgoing: 2,
            file_path: None,
        }],
        high_degree_symbols: vec![
            GraphHotspot {
                id: "sym:handler".to_string(),
                name: "handler".to_string(),
                node_type: "function".to_string(),
                degree: 3,
                incoming: 1,
                outgoing: 2,
                file_path: Some("src/lib.rs".to_string()),
            },
            GraphHotspot {
                id: "sym:parse".to_string(),
                name: "parse".to_string(),
                node_type: "function".to_string(),
                degree: 2,
                incoming: 1,
                outgoing: 1,
                file_path: Some("src/lib.rs".to_string()),
            },
        ],
        high_degree_modules: vec![GraphHotspot {
            id: "mod:api".to_string(),
            name: "api".to_string(),
            node_type: "module".to_string(),
            degree: 1,
            incoming: 1,
            outgoing: 0,
            file_path: None,
        }],
        incoming_call_hotspots: vec![GraphHotspot {
            id: "sym:parse".to_string(),
            name: "parse".to_string(),
            node_type: "function".to_string(),
            degree: 1,
            incoming: 1,
            outgoing: 0,
            file_path: Some("src/lib.rs".to_string()),
        }],
    }
}

fn expected_bridge_summary() -> BridgeReportSummary {
    BridgeReportSummary {
        relation: RELATES_TO_CODE.to_string(),
        edge_count: 1,
        inferred: true,
        read_only: true,
        source_system_counts: vec![NamedCount {
            name: "gobby-memory".to_string(),
            count: 1,
        }],
        confidence_range: Some(ConfidenceRange {
            min: 0.72,
            max: 0.72,
        }),
    }
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
        indexing: gobby_core::config::IndexingConfig::default(),
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

#[test]
fn bridge_summary_aggregates_shared_symbol_hypotheses_across_source_systems() {
    // Two inferred memory->code hypotheses land on the same symbol from two
    // different source systems. The direct summary aggregates edge metadata
    // (never graph structure): it counts both edges, tallies each source
    // system, and spans the confidence range — surfacing both hypotheses the
    // old bridge-cut filter would have collapsed once memory nodes shared a
    // symbol.
    let edges = vec![
        BridgeEdgeHypothesis::inferred(
            "memory-1",
            "sym:handler",
            RELATES_TO_CODE,
            "gobby-memory",
            Some(0.6),
        ),
        BridgeEdgeHypothesis::inferred(
            "memory-2",
            "sym:handler",
            RELATES_TO_CODE,
            "gobby-graph",
            Some(0.9),
        ),
    ];

    assert_eq!(
        summarize_bridge_edges(&edges),
        Some(BridgeReportSummary {
            relation: RELATES_TO_CODE.to_string(),
            edge_count: 2,
            inferred: true,
            read_only: true,
            // BTreeMap-sorted by source-system name: "gobby-graph" < "gobby-memory".
            source_system_counts: vec![
                NamedCount {
                    name: "gobby-graph".to_string(),
                    count: 1,
                },
                NamedCount {
                    name: "gobby-memory".to_string(),
                    count: 1,
                },
            ],
            confidence_range: Some(ConfidenceRange { min: 0.6, max: 0.9 }),
        })
    );
}
