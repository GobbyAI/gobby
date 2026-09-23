use crate::communities::{LabelSource, StoredCommunity};
use crate::config::{CodeVectorSettings, Context};
use crate::models::{ProjectionMetadata, ProjectionProvenance};
use std::path::PathBuf;
use std::time::SystemTime;

use super::generation::{
    generate_report_from_snapshot, generate_report_from_snapshot_with_options,
};
use super::loading::{CODE_COMMUNITIES_INPUT, MISSING_COMMUNITIES_DETAIL, community_input};
use super::render::render_markdown;
use super::summary::{CODE_GRAPH_INPUT, summarize_bridge_edges, summarize_hotspots};
use super::types::{
    BridgeEdgeInput, BridgeReportSummary, ConfidenceRange, GraphHotspot, GraphReportHotspots,
    NamedCount, ReportCodeEdge, ReportDegradation, ReportGraphSnapshot, ReportNode,
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

    let hotspots = summarize_hotspots(&nodes, &edges, DEFAULT_TOP_LIMIT).expect("valid code graph");

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
        summarize_hotspots(&snapshot.nodes, &snapshot.code_edges, DEFAULT_TOP_LIMIT)
            .expect("valid code graph"),
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
        communities: None,
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
        communities: None,
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
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
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
fn invalid_code_graph_input_degrades_hotspots() {
    let report = generate_report_from_snapshot(
        "project-1",
        "2026-05-28T00:00:00Z",
        ReportGraphSnapshot {
            nodes: vec![
                ReportNode::new("sym:handler", "handler", "function"),
                ReportNode::new("sym:handler", "handler", "function"),
            ],
            code_edges: vec![],
            ..ReportGraphSnapshot::default()
        },
    );

    assert_eq!(report.hotspots, GraphReportHotspots::default());
    assert_eq!(report.degradation_details.len(), 1);
    assert_eq!(report.degradation_details[0].input, CODE_GRAPH_INPUT);
    assert!(!report.degradation_details[0].required);
    assert_eq!(
        report.degradation_details[0].detail,
        "hotspots skipped: duplicate node id sym:handler"
    );
    assert!(
        report
            .markdown
            .contains("- `CODE_GRAPH`: hotspots skipped: duplicate node id sym:handler")
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

fn stored_community(id: i32, size: usize, label: &str) -> StoredCommunity {
    let members: Vec<String> = (0..size)
        .map(|index| format!("src/c{id}/m{index}.rs"))
        .collect();
    StoredCommunity {
        machine_id: "machine-1".into(),
        project_id: "project-1".into(),
        community_id: id,
        member_count: size,
        representatives: members.iter().take(5).cloned().collect(),
        members,
        internal_edges: size.saturating_sub(1),
        cohesion: 0.5,
        boundary: Vec::new(),
        member_signature: format!("signature-{id}"),
        label_deterministic: label.into(),
        label: label.into(),
        label_source: LabelSource::Deterministic,
        label_confidence: None,
        label_model: None,
        label_candidates: Vec::new(),
        labeled_signature: None,
        labeled_at: None,
        label_attempted_at: None,
        refreshed_at: SystemTime::UNIX_EPOCH,
        label_stale: false,
    }
}

fn report_with_communities(
    rows: anyhow::Result<Vec<StoredCommunity>>,
    top_n: usize,
) -> ProjectGraphReport {
    generate_report_from_snapshot_with_options(
        "project-1",
        "2026-09-23T00:00:00Z",
        ReportGraphSnapshot::default(),
        community_input(rows),
        ProjectGraphReportOptions { top_n },
    )
}

#[test]
fn report_communities_section_counts_reconcile() {
    let rows = vec![
        stored_community(3, 3, "session handoff"),
        stored_community(1, 12, "graph report"),
        stored_community(5, 1, "single a"),
        stored_community(2, 7, "task close gates"),
        stored_community(6, 1, "single b"),
        stored_community(4, 2, "pair"),
    ];
    let report = report_with_communities(Ok(rows), 2);

    let counts = report
        .communities
        .as_ref()
        .map(|communities| (communities.total, communities.thin_count));
    assert_eq!(counts, Some((6, 3)));
    let top_ids: Vec<i32> = report
        .communities
        .iter()
        .flat_map(|communities| &communities.top)
        .map(|community| community.community_id)
        .collect();
    assert_eq!(top_ids, vec![1, 2]);
    assert!(report.degradation_details.is_empty());

    let json = serde_json::to_value(&report).expect("report serializes");
    assert_eq!(json["communities"]["total"], 6);
    assert_eq!(json["communities"]["thin_count"], 3);
    assert_eq!(json["communities"]["top"][0]["size"], 12);

    assert!(report.markdown.contains("## Import communities"));
    assert!(
        report
            .markdown
            .contains("- 6 communities (3 thin, below 3 members)")
    );
    assert!(
        report
            .markdown
            .contains("| 1 | graph report | 12 | 0.50 | deterministic |")
    );
    assert!(!report.markdown.contains("| 3 | session handoff |"));
}

#[test]
fn report_without_communities_degrades_optional_input() {
    let report = report_with_communities(Ok(Vec::new()), DEFAULT_TOP_LIMIT);

    assert_eq!(report.communities, None);
    assert_eq!(
        report.degradation_details,
        vec![ReportDegradation {
            input: CODE_COMMUNITIES_INPUT.to_string(),
            required: false,
            detail: MISSING_COMMUNITIES_DETAIL.to_string(),
        }]
    );
    assert!(!report.markdown.contains("## Import communities"));
    assert!(
        report
            .markdown
            .contains("- `code_communities`: no stored import communities")
    );

    let failed = report_with_communities(
        Err(anyhow::anyhow!("connection refused")),
        DEFAULT_TOP_LIMIT,
    );
    assert_eq!(failed.communities, None);
    assert_eq!(failed.degradation_details.len(), 1);
    assert_eq!(failed.degradation_details[0].input, CODE_COMMUNITIES_INPUT);
    assert!(!failed.degradation_details[0].required);
    assert!(
        failed.degradation_details[0]
            .detail
            .contains("connection refused")
    );
}

#[test]
fn report_marks_stale_labels() {
    let mut stale = stored_community(1, 5, "graph report");
    stale.label_stale = true;
    let fresh = StoredCommunity {
        label: "Task close gates".into(),
        label_source: LabelSource::Model,
        ..stored_community(2, 4, "task close gates")
    };
    let report = report_with_communities(Ok(vec![stale, fresh]), DEFAULT_TOP_LIMIT);

    let labels: Vec<(i32, bool, String)> = report
        .communities
        .iter()
        .flat_map(|communities| &communities.top)
        .map(|community| {
            (
                community.community_id,
                community.label_stale,
                community.label_source.clone(),
            )
        })
        .collect();
    assert_eq!(
        labels,
        vec![
            (1, true, "deterministic".to_string()),
            (2, false, "model".to_string()),
        ]
    );
    assert!(
        report
            .markdown
            .contains("| 1 | graph report | 5 | 0.50 | deterministic (model label stale) |")
    );
    assert!(
        report
            .markdown
            .contains("| 2 | Task close gates | 4 | 0.50 | model |")
    );
}
