use crate::communities::{LabelSource, StoredCommunity};
use crate::config::{CodeVectorSettings, Context};
use crate::models::{ProjectionMetadata, ProjectionProvenance};
use std::path::PathBuf;
use std::time::SystemTime;

use super::generation::{
    generate_report_from_snapshot, generate_report_from_snapshot_with_options,
};
use super::loading::{
    CODE_COMMUNITIES_INPUT, ENGINE_RESULT_CAP, MISSING_COMMUNITIES_DETAIL, community_input,
    read_to_completion,
};
use super::queries::{
    report_bridge_edges_query, report_code_edge_counts_query, report_hotspots_query,
    report_incoming_call_hotspots_query, report_node_counts_query, report_target_frequencies_query,
};
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
        machine_id: gobby_core::machine::read_local_machine_id().expect("read machine id"),
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

const ADMISSION_TOKENS: [&str; 4] = [
    "$admitted_paths",
    "= v.h",
    "$admitted_map[",
    "$endpoint_map[",
];

fn report_query_cases() -> Vec<(&'static str, String)> {
    vec![
        ("nodes", report_node_counts_query("child").0),
        ("edges", report_code_edge_counts_query("child").0),
        (
            "hotspots",
            report_hotspots_query("child", "symbol", Some(1)).0,
        ),
        (
            "incoming calls",
            report_incoming_call_hotspots_query("child", 1).0,
        ),
        ("bridges", report_bridge_edges_query("child").0),
        (
            "external targets",
            report_target_frequencies_query("child", "external", 1).0,
        ),
        (
            "unresolved targets",
            report_target_frequencies_query("child", "unresolved", 1).0,
        ),
    ]
}

fn last_aggregate(branch: &str) -> Option<usize> {
    [branch.rfind("count("), branch.rfind("sum(")]
        .into_iter()
        .flatten()
        .max()
}

fn first_admission(branch: &str) -> Option<usize> {
    // Admission is a version-map lookup inside an edge or endpoint filter. A bare
    // bind or import (`WITH $admitted_map AS am`) filters nothing, so it does not count.
    ADMISSION_TOKENS
        .iter()
        .copied()
        .chain(["IN coalesce(am[", "IN coalesce(em["])
        .filter_map(|token| branch.find(token))
        .min()
}

#[test]
fn hotspot_ranking_excludes_replaced_and_tombstoned_targets_before_ranking() {
    // Even a high-degree historical target must not consume the top-1 slot.
    for (surface, query) in [
        (
            "symbol degrees",
            report_hotspots_query("child", "symbol", Some(1)).0,
        ),
        (
            "incoming calls",
            report_incoming_call_hotspots_query("child", 1).0,
        ),
    ] {
        for branch in query.split("UNION ALL") {
            // The branch's count is the ranking input. A later outer sum() is not.
            let Some(count) = branch.rfind("count(") else {
                continue;
            };
            let filter = &branch[..count];
            assert!(
                filter.contains("IN coalesce(am[") || filter.contains("IN coalesce(em["),
                "{surface}: target version admission must precede ranking: {branch}"
            );
        }
    }
}

#[test]
fn report_queries_filter_admitted_versions_before_aggregation() {
    for (surface, query) in report_query_cases() {
        for branch in query.split("UNION ALL") {
            let Some(aggregate) = last_aggregate(branch) else {
                continue;
            };
            let admission = first_admission(branch).unwrap_or_else(|| {
                panic!("{surface}: aggregate must constrain admitted versions: {branch}")
            });
            assert!(
                admission < aggregate,
                "{surface}: filter must precede aggregation: {branch}"
            );
            if branch.contains("-[r") {
                assert!(
                    branch[..aggregate].contains("content_hash"),
                    "{surface}: an edge needs its version hash, path alone is insufficient: {branch}"
                );
            }
        }
    }
}

#[test]
fn report_node_counts_admit_module_and_external_nodes_only_through_admitted_edges() {
    let (query, _) = report_node_counts_query("child");
    for label in ["CodeModule", "UnresolvedCallee", "ExternalSymbol"] {
        assert!(query.contains(label), "{label} nodes are counted: {query}");
    }
    // A placeholder is reached through an edge, that edge is admitted by its
    // version, and only then is the endpoint counted.
    let edge = query.find("-[r:").expect("placeholder nodes need an edge");
    let admitted = query[edge..]
        .find("$admitted_map[")
        .map(|at| edge + at)
        .expect("placeholder edge admission");
    let aggregate = query.rfind("count(n)").expect("aggregate");
    assert!(
        edge < admitted && admitted < aggregate,
        "admitted edge must precede aggregation: {query}"
    );
}

#[test]
fn report_edge_patterns_include_inheritance_rels() {
    let (query, _) = report_code_edge_counts_query("proj-1");
    for rel in ["INHERITS", "EXTENDS", "IMPLEMENTS"] {
        assert!(
            query.contains(rel),
            "edge-count query missing {rel}: {query}"
        );
    }
}

#[test]
fn report_queries_anchor_on_admitted_pairs_or_version_maps() {
    for (surface, query) in report_query_cases() {
        assert!(
            query.contains("$admitted_pairs") || query.contains("$admitted_map"),
            "{surface}: anchor on admitted pairs or version maps: {query}"
        );
        assert!(
            !query.contains("IN $admitted_versions"),
            "{surface}: per-edge IN $admitted_versions scan: {query}"
        );
    }
}

#[test]
fn hotspots_query_uses_label_scoped_seed_and_typed_edges() {
    let (query, params) = report_hotspots_query("proj-1", "symbol", None);

    // Each branch seeds from the labeled node over the typed edge set. Neither
    // branch scans every project node and filters by label afterwards.
    assert!(
        query.contains("MATCH (n:CodeSymbol {project: $project})-[out:DEFINES|IMPORTS|CALLS|INHERITS|EXTENDS|IMPLEMENTS]->"),
        "expected label-scoped typed outgoing traversal, got: {query}"
    );
    assert!(
        query.contains("MATCH (in_source {project: $project})-[inc:DEFINES|IMPORTS|CALLS|INHERITS|EXTENDS|IMPLEMENTS]->(n:CodeSymbol {project: $project})"),
        "expected typed incoming traversal onto the labeled node, got: {query}"
    );
    assert!(
        !query.contains("WHERE n:CodeSymbol"),
        "label-less MATCH + WHERE label filter regressed: {query}"
    );
    assert!(
        !query.contains("type(out) IN") && !query.contains("type(inc) IN"),
        "untyped-then-type()-filter anti-pattern regressed: {query}"
    );
    assert!(
        !query.contains("OPTIONAL MATCH"),
        "node-driven double-expansion regressed: {query}"
    );
    // The maps bind once before the CALL; each union branch imports them. A
    // per-row map parameter lookup timed out.
    assert!(
        query.starts_with("WITH $admitted_map AS am, $endpoint_map AS em CALL {"),
        "maps must bind once before the CALL: {query}"
    );
    assert_eq!(
        query.matches("WITH am, em MATCH").count(),
        2,
        "each union branch must import the bound maps: {query}"
    );
    assert!(
        !query.contains("$admitted_map[") && !query.contains("$endpoint_map["),
        "per-row map parameter lookup regressed: {query}"
    );
    assert!(query.contains("count(out) AS outgoing"));
    assert!(query.contains("count(inc) AS incoming"));
    assert!(query.contains("sum(outgoing) AS outgoing"));
    // Unbounded: every ranked node returns; the limit applies after the merge.
    assert!(
        !query.contains("LIMIT"),
        "unbounded query must not limit early: {query}"
    );
    assert!(params.contains_key("project"));
}

#[test]
fn hotspots_query_scopes_seed_label_by_node_class() {
    let (file, _) = report_hotspots_query("proj-1", "file", Some(3));
    assert!(file.contains("MATCH (n:CodeFile {project: $project})-[out:"));
    assert!(file.contains("(n:CodeFile {project: $project})"));
    assert!(
        file.ends_with(" LIMIT 3"),
        "file hotspots keep their limit: {file}"
    );

    let (module, _) = report_hotspots_query("proj-1", "module", None);
    assert!(module.contains("MATCH (n:CodeModule {project: $project})-[out:"));
    assert!(module.contains("(n:CodeModule {project: $project})"));
    assert!(
        !module.contains("LIMIT"),
        "module hotspots are unbounded: {module}"
    );
}

#[test]
fn call_queries_keep_foreign_file_calls_by_their_own_version() {
    // A symbol's CALLS edge can name another file (`r.file`). The old form counted
    // those edges; the admitted version of that file must still gate them.
    for (surface, query) in [
        (
            "incoming calls",
            report_incoming_call_hotspots_query("child", 1).0,
        ),
        (
            "external targets",
            report_target_frequencies_query("child", "external", 1).0,
        ),
        (
            "unresolved targets",
            report_target_frequencies_query("child", "unresolved", 1).0,
        ),
    ] {
        assert!(
            query.contains("r.file <> src.file_path")
                && query.contains("r.content_hash IN coalesce(am[r.file], [])"),
            "{surface}: foreign-file CALLS must be admitted by their own version: {query}"
        );
    }
}

#[test]
fn unlimited_reads_page_past_engine_result_cap() {
    // The engine truncates each result set at RESULTSET_SIZE. An unlimited read
    // must page to completion, or rows past the cap vanish without an error.
    let rows: Vec<usize> = (0..ENGINE_RESULT_CAP + 1).collect();
    let engine = |skip: usize, limit: usize| -> anyhow::Result<Vec<usize>> {
        let start = skip.min(rows.len());
        let end = (start + limit.min(ENGINE_RESULT_CAP)).min(rows.len());
        Ok(rows[start..end].to_vec())
    };
    let read = read_to_completion(engine).expect("paged read");
    assert_eq!(
        read.len(),
        rows.len(),
        "rows past the engine cap must be read"
    );
    assert_eq!(read, rows);
}

#[test]
fn paged_report_queries_sort_every_column_before_windowing() {
    // A window takes a fixed slice per call. The order must be total over the
    // returned columns, or rows can repeat or vanish between pages.
    let (hotspots, _) = report_hotspots_query("child", "module", None);
    assert!(
        hotspots.contains(
            "ORDER BY degree DESC, name ASC, id ASC, node_type ASC, file_path ASC, incoming ASC, outgoing ASC"
        ),
        "module hotspots must order every returned column: {hotspots}"
    );
    let (bridges, _) = report_bridge_edges_query("child");
    assert!(
        bridges.contains(
            "ORDER BY source_id, target_symbol_id, relation, provenance, confidence, source_system, source_file_path, source_line, source_symbol_id, matching_method"
        ),
        "bridge edges must order every returned column: {bridges}"
    );
}

/// Fixture nodes for the live hotspot test. `a.hub.old` replaces `a.hub` and
/// `c.dead` is tombstoned; neither may reach any count.
const LIVE_NODES: &[&str] = &[
    "CREATE (:CodeSymbol {project: 'p1', id: 'a.hub', name: 'hub', file_path: 'a.py', file_content_hash: 'ha2'})",
    "CREATE (:CodeSymbol {project: 'p1', id: 'a.hub.old', name: 'hub', file_path: 'a.py', file_content_hash: 'ha1'})",
    "CREATE (:CodeSymbol {project: 'p1', id: 'b.f', name: 'f', file_path: 'b.py', file_content_hash: 'hb1'})",
    "CREATE (:CodeSymbol {project: 'p1', id: 'b.g', name: 'g', file_path: 'b.py', file_content_hash: 'hb1'})",
    "CREATE (:CodeSymbol {project: 'p1', id: 'c.live', name: 'live', file_path: 'c.py', file_content_hash: 'hc1'})",
    "CREATE (:CodeSymbol {project: 'p1', id: 'c.dead', name: 'dead', file_path: 'c.py', file_content_hash: 'hc0'})",
    "CREATE (:UnresolvedCallee {project: 'p1', id: 'u.x', name: 'x'})",
    "CREATE (:UnresolvedCallee {project: 'p1', id: 'u.y', name: 'y'})",
    "CREATE (:ExternalSymbol {project: 'p1', id: 'x.z', name: 'z'})",
];

/// CALLS edges as (source, target, edge file, edge content hash). The admitted
/// versions are a.py|ha2, b.py|hb1 and c.py|hc1. Rows whose edge file differs
/// from the source file are foreign-file CALLS.
const LIVE_CALLS: &[(&str, &str, &str, &str)] = &[
    ("a.hub", "b.f", "a.py", "ha2"),
    ("b.f", "a.hub", "b.py", "hb1"),
    ("b.g", "a.hub", "b.py", "hb1"),
    ("b.g", "a.hub", "a.py", "ha2"),
    ("a.hub.old", "b.f", "a.py", "ha1"),
    ("b.g", "c.dead", "b.py", "hb1"),
    ("b.f", "c.live", "b.py", "hb1"),
    ("b.f", "u.x", "b.py", "hb1"),
    ("b.g", "u.x", "a.py", "ha2"),
    ("a.hub", "u.x", "a.py", "ha2"),
    ("c.live", "u.y", "c.py", "hc1"),
    ("a.hub", "x.z", "a.py", "ha2"),
    ("b.g", "x.z", "b.py", "hb1"),
    ("b.g", "a.hub.old", "b.py", "hb1"),
    ("b.f", "a.hub.old", "b.py", "hb1"),
    ("c.live", "a.hub.old", "c.py", "hc1"),
    ("a.hub.old", "c.live", "a.py", "ha1"),
    ("a.hub.old", "x.z", "a.py", "ha1"),
    ("b.g", "a.hub.old", "b.py", "hb1"),
    ("b.f", "a.hub.old", "b.py", "hb1"),
];

fn live_text(row: &gobby_core::falkor::Row, key: &str) -> String {
    row.get(key)
        .and_then(|value| value.as_str())
        .unwrap_or_default()
        .to_string()
}

fn live_number(row: &gobby_core::falkor::Row, key: &str) -> i64 {
    // Mirrors `row_usize`: some engine integers arrive float-backed.
    row.get(key)
        .and_then(|value| value.as_i64().or_else(|| value.as_f64().map(|v| v as i64)))
        .unwrap_or(-1)
}

fn live_pairs(rows: &[gobby_core::falkor::Row], key: &str, value: &str) -> Vec<(String, i64)> {
    rows.iter()
        .map(|row| (live_text(row, key), live_number(row, value)))
        .collect()
}

/// Engine-backed check of the hotspot, incoming-call and target queries against
/// exact fixture rows. Runs only when `GCODE_HOTSPOT_TEST_FALKOR_URL` names a
/// loopback FalkorDB such as `redis://127.0.0.1:63790`; unset, it returns early.
/// The graph name is unique per run, so no production graph is touched.
#[test]
fn live_hotspot_queries_return_exact_fixture_rows() -> anyhow::Result<()> {
    use super::queries as builders;
    use crate::visibility::graph_versions::{AdmittedGraphVersions, with_admitted_versions};

    let Some(raw_url) = std::env::var_os("GCODE_HOTSPOT_TEST_FALKOR_URL") else {
        return Ok(());
    };
    let url = reqwest::Url::parse(
        raw_url
            .to_str()
            .ok_or_else(|| anyhow::anyhow!("utf-8 url"))?,
    )?;
    anyhow::ensure!(
        url.host_str() == Some("127.0.0.1"),
        "live hotspot test requires a loopback FalkorDB"
    );
    anyhow::ensure!(
        ![None, Some(6379), Some(6380), Some(6333), Some(16379)].contains(&url.port()),
        "production FalkorDB ports refused"
    );
    anyhow::ensure!(
        url.username().is_empty() && url.password().is_none(),
        "live hotspot test takes no credentials"
    );
    let config = gobby_core::config::FalkorConfig {
        host: "127.0.0.1".to_string(),
        port: url.port().ok_or_else(|| anyhow::anyhow!("test port"))?,
        password: None,
    };
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_nanos();
    let graph_name = format!("gcode_hotspot_live_{nanos}");
    let mut client = gobby_core::falkor::GraphClient::from_config(&config, &graph_name)?;

    for node in LIVE_NODES {
        client.query(node, None)?;
    }
    for (source, target, file, hash) in LIVE_CALLS {
        let statement = format!(
            "MATCH (a {{project: 'p1', id: '{source}'}}), (b {{project: 'p1', id: '{target}'}}) \
             CREATE (a)-[:CALLS {{file: '{file}', content_hash: '{hash}'}}]->(b)"
        );
        client.query(&statement, None)?;
    }

    let versions = AdmittedGraphVersions {
        keys: ["a.py|ha2", "b.py|hb1", "c.py|hc1"]
            .map(String::from)
            .to_vec(),
        paths: ["a.py", "b.py", "c.py"].map(String::from).to_vec(),
        endpoints: ["a.py|ha2", "b.py|hb1", "c.py|hc1"]
            .map(String::from)
            .to_vec(),
    };
    let mut run = |(query, params): (String, std::collections::HashMap<String, String>)| {
        let (query, params) = with_admitted_versions((query, params), &versions);
        client.query(&query, Some(params))
    };

    let top_symbol = run(builders::report_hotspots_query("p1", "symbol", Some(1)))?;
    let top_symbol: Vec<(String, i64, i64, i64)> = top_symbol
        .iter()
        .map(|row| {
            (
                live_text(row, "id"),
                live_number(row, "degree"),
                live_number(row, "incoming"),
                live_number(row, "outgoing"),
            )
        })
        .collect();
    assert_eq!(top_symbol, vec![("a.hub".to_string(), 6, 3, 3)]);

    let all_symbols = run(builders::report_hotspots_query("p1", "symbol", None))?;
    let all_symbols: Vec<(String, i64)> = live_pairs(&all_symbols, "id", "degree");
    assert_eq!(
        all_symbols,
        vec![
            ("a.hub".to_string(), 6),
            ("b.f".to_string(), 4),
            ("b.g".to_string(), 4),
            ("c.live".to_string(), 2),
        ]
    );

    let incoming = run(builders::report_incoming_call_hotspots_query("p1", 3))?;
    assert_eq!(
        live_pairs(&incoming, "id", "degree"),
        vec![
            ("a.hub".to_string(), 3),
            ("b.f".to_string(), 1),
            ("c.live".to_string(), 1),
        ]
    );

    let unresolved = run(builders::report_target_frequencies_query(
        "p1",
        "unresolved",
        2,
    ))?;
    assert_eq!(
        live_pairs(&unresolved, "id", "count"),
        vec![("u.x".to_string(), 3), ("u.y".to_string(), 1)]
    );

    let external = run(builders::report_target_frequencies_query(
        "p1", "external", 1,
    ))?;
    assert_eq!(
        live_pairs(&external, "id", "count"),
        vec![("x.z".to_string(), 2)]
    );

    let mut node_counts = std::collections::BTreeMap::new();
    for row in run(builders::report_node_counts_query("p1"))? {
        *node_counts.entry(live_text(&row, "name")).or_insert(0) += live_number(&row, "count");
    }
    assert_eq!(
        node_counts,
        std::collections::BTreeMap::from([
            ("external".to_string(), 1),
            ("symbol".to_string(), 4),
            ("unresolved".to_string(), 2),
        ])
    );

    let edges = run(builders::report_code_edge_counts_query("p1"))?;
    assert_eq!(
        live_pairs(&edges, "name", "count"),
        vec![("CALLS".to_string(), 11)]
    );
    Ok(())
}

mod hotspot_snapshot_contract {
    use crate::index::indexer::IndexDegradation;
    use crate::visibility::graph_versions::{AdmittedGraphVersions, AdmittedNamespace};

    use super::super::DEFAULT_TOP_LIMIT;
    use super::super::hotspot_snapshot::{
        SNAPSHOT_FORMAT_VERSION, StoredSnapshot, admitted_digest, commit_snapshot_query,
        decode_payload, encode_payload, hotspots_on_miss, invalidate_after_write,
        invalidate_snapshots_query, refresh_failure_degradation, reserve_snapshot_query,
        snapshot_nonce, stored_hotspots_for,
    };
    use super::super::types::{GraphHotspot, GraphReportHotspots};

    /// One namespace whose paths come from its keys; `endpoints` is the shared set.
    fn namespace(project: &str, keys: &[&str], endpoints: &[&str]) -> AdmittedNamespace {
        AdmittedNamespace {
            project_id: project.to_string(),
            versions: AdmittedGraphVersions {
                keys: keys.iter().map(|key| key.to_string()).collect(),
                paths: keys
                    .iter()
                    .map(|key| key.split('|').next().unwrap().to_string())
                    .collect(),
                endpoints: endpoints.iter().map(|key| key.to_string()).collect(),
            },
        }
    }

    fn hotspot(id: &str, degree: usize) -> GraphHotspot {
        GraphHotspot {
            id: id.to_string(),
            name: id.to_string(),
            node_type: "symbol".to_string(),
            degree,
            incoming: degree / 2,
            outgoing: degree - degree / 2,
            file_path: Some("src/a.py".to_string()),
        }
    }

    #[test]
    fn snapshot_digest_moves_when_a_key_moves_between_namespaces() {
        let key = "src/a.py|h1";
        let overlay_holds = [
            namespace("overlay", &[key], &[key]),
            namespace("source", &[], &[key]),
        ];
        let source_holds = [
            namespace("overlay", &[], &[key]),
            namespace("source", &[key], &[key]),
        ];

        let before = admitted_digest(&overlay_holds, false, SNAPSHOT_FORMAT_VERSION);
        let after = admitted_digest(&source_holds, false, SNAPSHOT_FORMAT_VERSION);

        assert_ne!(
            before, after,
            "a key that moves between namespaces must change the digest"
        );
        assert_eq!(
            before,
            admitted_digest(&overlay_holds, false, SNAPSHOT_FORMAT_VERSION),
            "equal admitted sets must give an equal digest"
        );
    }

    #[test]
    fn snapshot_digest_changes_with_format_version() {
        let namespaces = [namespace("overlay", &["src/a.py|h1"], &["src/a.py|h1"])];

        let v1 = admitted_digest(&namespaces, false, 1);
        let v2 = admitted_digest(&namespaces, false, 2);

        assert_ne!(v1, v2, "a format bump must invalidate every stored digest");
    }

    #[test]
    fn snapshot_digest_changes_with_pending_projection() {
        let namespaces = [namespace("overlay", &["src/a.py|h1"], &["src/a.py|h1"])];

        assert_ne!(
            admitted_digest(&namespaces, false, SNAPSHOT_FORMAT_VERSION),
            admitted_digest(&namespaces, true, SNAPSHOT_FORMAT_VERSION),
            "a pending pinned projection must change the digest"
        );
    }

    #[test]
    fn stored_hotspots_serve_only_a_matching_digest_format_and_top_n() {
        let hotspots = GraphReportHotspots {
            high_degree_symbols: vec![hotspot("sym:parse", 9), hotspot("sym:lex", 4)],
            ..GraphReportHotspots::default()
        };
        let stored = StoredSnapshot {
            format: SNAPSHOT_FORMAT_VERSION,
            top_n: 10,
            digest: "digest-a".to_string(),
            payload: encode_payload(&hotspots),
        };

        assert_eq!(
            stored_hotspots_for(Some(&stored), "digest-a", 10),
            Some(hotspots),
            "a matching digest, format and top-n serves the stored hotspots"
        );
        assert_eq!(stored_hotspots_for(Some(&stored), "digest-b", 10), None);
        assert_eq!(stored_hotspots_for(Some(&stored), "digest-a", 20), None);
        assert_eq!(stored_hotspots_for(None, "digest-a", 10), None);
    }

    #[test]
    fn stored_payload_round_trips_hotspots() {
        let hotspots = GraphReportHotspots {
            high_degree_files: vec![hotspot("file:src/a.py", 3)],
            high_degree_symbols: vec![hotspot("sym:parse", 9)],
            high_degree_modules: vec![hotspot("mod:api", 12)],
            incoming_call_hotspots: vec![hotspot("sym:parse", 5)],
        };

        assert_eq!(decode_payload(&encode_payload(&hotspots)), Some(hotspots));
        assert_eq!(decode_payload("not json"), None);
    }

    #[test]
    fn invalidation_deletes_every_snapshot_naming_the_written_project() {
        let (query, params) = invalidate_snapshots_query("source-1");

        assert!(query.contains("DELETE s"), "{query}");
        assert!(query.contains("$project IN s.namespaces"), "{query}");
        assert_eq!(params.get("project").map(String::as_str), Some("source-1"));
    }

    #[test]
    fn reservation_names_the_namespaces_and_holds_no_servable_snapshot() {
        let namespaces = [
            namespace("overlay-1", &[], &[]),
            namespace("source-1", &[], &[]),
        ];

        let reserve = reserve_snapshot_query("overlay-1", &namespaces, "nonce-a").unwrap();

        assert!(reserve.cypher.starts_with("MERGE"), "{}", reserve.cypher);
        assert!(
            reserve.cypher.contains("s.nonce = $nonce"),
            "{}",
            reserve.cypher
        );
        assert!(
            reserve.cypher.contains("s.namespaces = $namespaces"),
            "{}",
            reserve.cypher
        );
        assert!(
            reserve.cypher.contains("s.payload = NULL"),
            "{}",
            reserve.cypher
        );
        assert!(
            reserve.cypher.contains("s.digest = NULL"),
            "{}",
            reserve.cypher
        );
        assert_eq!(reserve.params["nonce"], "'nonce-a'");
        assert_eq!(reserve.params["namespaces"], "['overlay-1', 'source-1']");
    }

    #[test]
    fn commit_writes_only_into_its_own_surviving_reservation() {
        let commit =
            commit_snapshot_query("overlay-1", "digest-a", "nonce-a", &ranked_symbols(1)).unwrap();

        assert!(commit.cypher.starts_with("MATCH"), "{}", commit.cypher);
        assert!(!commit.cypher.contains("MERGE"), "{}", commit.cypher);
        assert!(commit.cypher.contains("nonce: $nonce"), "{}", commit.cypher);
        assert!(
            commit.cypher.contains("s.payload = $payload"),
            "{}",
            commit.cypher
        );
        assert_eq!(commit.params["nonce"], "'nonce-a'");
        assert_eq!(commit.params["digest"], "'digest-a'");
    }

    #[test]
    fn snapshot_nonces_differ_per_reservation() {
        assert_ne!(snapshot_nonce(), snapshot_nonce());
    }

    #[test]
    fn invalidation_runs_after_a_failed_write_and_keeps_the_writer_error() {
        let mut invalidated = Vec::new();

        let result: anyhow::Result<()> = invalidate_after_write(
            "overlay-1",
            Err(anyhow::anyhow!("write failed mid-batch")),
            |project| {
                invalidated.push(project.to_string());
                Ok(())
            },
        );

        assert_eq!(invalidated, vec!["overlay-1".to_string()]);
        let message = format!("{:#}", result.expect_err("writer error must surface"));
        assert!(message.contains("write failed mid-batch"), "{message}");
    }

    #[test]
    fn invalidation_error_surfaces_after_a_successful_write() {
        let result = invalidate_after_write("overlay-1", Ok(7usize), |_| {
            Err(anyhow::anyhow!("invalidation refused"))
        });

        let message = format!("{:#}", result.expect_err("invalidation error must surface"));
        assert!(message.contains("invalidation refused"), "{message}");
    }

    fn ranked_symbols(count: usize) -> GraphReportHotspots {
        GraphReportHotspots {
            high_degree_symbols: (0..count)
                .map(|rank| hotspot(&format!("sym:{rank}"), count - rank))
                .collect(),
            ..GraphReportHotspots::default()
        }
    }

    #[derive(Default)]
    struct MissCalls {
        computed_at: Vec<usize>,
        saved: Vec<GraphReportHotspots>,
    }

    fn miss(top_n: usize, may_save: bool) -> (MissCalls, Option<GraphReportHotspots>) {
        let mut calls = MissCalls::default();
        let served = hotspots_on_miss(
            &mut calls,
            top_n,
            may_save,
            |calls, limit| {
                calls.computed_at.push(limit);
                Ok(ranked_symbols(3))
            },
            |calls, hotspots| {
                calls.saved.push(hotspots.clone());
                Ok(())
            },
        )
        .expect("a miss never fails the report");
        (calls, served)
    }

    #[test]
    fn report_miss_saves_the_full_snapshot_and_serves_top_n() {
        let (calls, served) = miss(1, true);

        assert_eq!(calls.computed_at, vec![DEFAULT_TOP_LIMIT]);
        assert_eq!(
            calls.saved,
            vec![ranked_symbols(3)],
            "the saved payload must be the full default-limit result"
        );
        assert_eq!(
            served.map(|hotspots| hotspots.high_degree_symbols),
            Some(ranked_symbols(3).high_degree_symbols[..1].to_vec())
        );
    }

    #[test]
    fn report_miss_without_write_authority_computes_live_and_saves_nothing() {
        let (calls, served) = miss(1, false);

        assert_eq!(served, None);
        assert!(calls.computed_at.is_empty());
        assert!(calls.saved.is_empty());
    }

    #[test]
    fn report_miss_above_the_stored_limit_computes_live_and_saves_nothing() {
        let (calls, served) = miss(DEFAULT_TOP_LIMIT + 1, true);

        assert_eq!(served, None);
        assert!(calls.computed_at.is_empty());
        assert!(calls.saved.is_empty());
    }

    #[test]
    fn report_miss_save_failure_still_serves_the_computed_hotspots() {
        let served = hotspots_on_miss(
            &mut (),
            2,
            true,
            |_, _| Ok(ranked_symbols(3)),
            |_, _| Err(anyhow::anyhow!("snapshot write refused")),
        )
        .expect("a failed save never fails the report");

        assert_eq!(
            served.map(|hotspots| hotspots.high_degree_symbols),
            Some(ranked_symbols(3).high_degree_symbols[..2].to_vec())
        );
    }

    #[test]
    fn snapshot_write_failure_leaves_index_successful_with_visible_note() {
        assert_eq!(refresh_failure_degradation(Ok(())), None);

        let degradation =
            refresh_failure_degradation(Err(anyhow::anyhow!("snapshot write refused")));

        assert!(
            matches!(
                &degradation,
                Some(IndexDegradation::HotspotSnapshotFailed { message })
                    if message.contains("snapshot write refused")
            ),
            "{degradation:?}"
        );
    }
}
