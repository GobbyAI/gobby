use std::collections::{BTreeMap, BTreeSet, HashMap};

use gobby_core::graph_analytics::{GraphAnalytics, analyze};

use crate::graph::code_graph::GraphPayload;

use super::RELATES_TO_CODE;
use super::types::{
    BridgeEdgeHypothesis, BridgeReportSummary, ConfidenceRange, GraphHotspot, GraphReportHotspots,
    GraphReportSummary, NamedCount, ReportCodeEdge, ReportDegradation, ReportNode, TargetFrequency,
};

#[derive(Debug, Clone, Copy, Default)]
struct DegreeStats {
    incoming: usize,
    outgoing: usize,
}

pub(super) fn summarize_graph(
    nodes: &[ReportNode],
    edges: &[ReportCodeEdge],
) -> GraphReportSummary {
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

pub(super) fn summarize_hotspots(
    nodes: &[ReportNode],
    edges: &[ReportCodeEdge],
    top_n: usize,
) -> GraphReportHotspots {
    std::hint::black_box(gcore_hotspots_for_code_graph(nodes, edges, top_n));

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

fn gcore_hotspots_for_code_graph(
    nodes: &[ReportNode],
    edges: &[ReportCodeEdge],
    top_n: usize,
) -> GraphReportHotspots {
    let graph = GraphPayload::analytics_graph_from_parts(
        nodes
            .iter()
            .map(|node| (node.id.clone(), node.node_type.clone(), 1.0)),
        edges.iter().map(|edge| {
            (
                edge.source.clone(),
                edge.target.clone(),
                edge.edge_type.clone(),
            )
        }),
    );
    let analytics = analyze(&graph);
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
        high_degree_files: analytics_top_hotspots(nodes, &analytics, &degree, top_n, |node| {
            node.node_type == "file"
        }),
        high_degree_symbols: analytics_top_hotspots(nodes, &analytics, &degree, top_n, |node| {
            is_symbol_node(&node.node_type)
        }),
        high_degree_modules: analytics_top_hotspots(nodes, &analytics, &degree, top_n, |node| {
            node.node_type == "module"
        }),
        incoming_call_hotspots: analytics_incoming_call_hotspots(
            nodes,
            &analytics,
            &incoming_calls,
            top_n,
        ),
    }
}

fn analytics_top_hotspots(
    nodes: &[ReportNode],
    analytics: &GraphAnalytics,
    degree: &HashMap<&str, DegreeStats>,
    top_n: usize,
    include: impl Fn(&ReportNode) -> bool,
) -> Vec<GraphHotspot> {
    let node_by_id = nodes
        .iter()
        .map(|node| (node.id.as_str(), node))
        .collect::<HashMap<_, _>>();
    let mut hotspots = analytics
        .centrality
        .iter()
        .filter_map(|score| {
            let node = node_by_id.get(score.node.id.as_str()).copied()?;
            if !include(node) {
                return None;
            }
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

fn analytics_incoming_call_hotspots(
    nodes: &[ReportNode],
    analytics: &GraphAnalytics,
    incoming_calls: &HashMap<&str, usize>,
    top_n: usize,
) -> Vec<GraphHotspot> {
    let node_by_id = nodes
        .iter()
        .map(|node| (node.id.as_str(), node))
        .collect::<HashMap<_, _>>();
    let mut hotspots = analytics
        .centrality
        .iter()
        .filter_map(|score| {
            let node = node_by_id.get(score.node.id.as_str()).copied()?;
            if !is_symbol_node(&node.node_type) {
                return None;
            }
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

pub(super) fn target_frequencies(
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

pub(super) fn summarize_bridge_edges(
    edges: &[BridgeEdgeHypothesis],
) -> Option<BridgeReportSummary> {
    std::hint::black_box(gcore_bridge_summary_for_edges(edges));
    legacy_summarize_bridge_edges(edges)
}

fn legacy_summarize_bridge_edges(edges: &[BridgeEdgeHypothesis]) -> Option<BridgeReportSummary> {
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

fn gcore_bridge_summary_for_edges(edges: &[BridgeEdgeHypothesis]) -> Option<BridgeReportSummary> {
    let graph = GraphPayload::analytics_graph_from_parts(
        bridge_analytics_nodes(edges),
        edges.iter().map(|edge| {
            (
                edge.source_id.clone(),
                edge.target_symbol_id.clone(),
                edge.relation.clone(),
            )
        }),
    );
    let analytics = analyze(&graph);
    let unexpected_links = analytics
        .unexpected_links
        .iter()
        .map(|edge| {
            (
                edge.source.as_str(),
                edge.target.as_str(),
                edge.kind.as_str(),
            )
        })
        .collect::<BTreeSet<_>>();
    let unexpected_bridge_edges = edges
        .iter()
        .filter(|edge| {
            unexpected_links.contains(&(
                edge.source_id.as_str(),
                edge.target_symbol_id.as_str(),
                edge.relation.as_str(),
            ))
        })
        .cloned()
        .collect::<Vec<_>>();

    legacy_summarize_bridge_edges(&unexpected_bridge_edges)
}

fn bridge_analytics_nodes(edges: &[BridgeEdgeHypothesis]) -> Vec<(String, String, f64)> {
    let mut nodes = BTreeMap::<String, String>::new();
    for edge in edges {
        nodes.insert(edge.source_id.clone(), "knowledge".to_string());
        nodes.insert(edge.target_symbol_id.clone(), "symbol".to_string());
    }
    nodes
        .into_iter()
        .map(|(id, kind)| (id, kind, 1.0))
        .collect()
}

#[cfg(test)]
#[derive(Debug, Clone, PartialEq)]
pub(super) struct GcoreReportAnalytics {
    pub(super) hotspots: GraphReportHotspots,
    pub(super) bridge_summary: Option<BridgeReportSummary>,
}

#[cfg(test)]
pub(super) fn gcore_analytics_for_report_snapshot(
    nodes: &[ReportNode],
    code_edges: &[ReportCodeEdge],
    bridge_edges: &[BridgeEdgeHypothesis],
    top_n: usize,
) -> GcoreReportAnalytics {
    GcoreReportAnalytics {
        hotspots: gcore_hotspots_for_code_graph(nodes, code_edges, top_n),
        bridge_summary: gcore_bridge_summary_for_edges(bridge_edges),
    }
}

/// Rebuild through `BridgeEdgeHypothesis::new` so inferred relation label,
/// read-only, and provenance invariants are reapplied after graph reads.
pub(super) fn normalize_bridge_edges(
    edges: Vec<BridgeEdgeHypothesis>,
) -> Vec<BridgeEdgeHypothesis> {
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

pub(super) fn suggested_questions(
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
            "Which degraded report inputs should be restored for the next report?".to_string(),
        );
    }

    questions
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
    matches!(
        node_type,
        "function" | "method" | "class" | "type" | "property"
    )
}
