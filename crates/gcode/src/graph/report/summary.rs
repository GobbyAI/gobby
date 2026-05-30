use std::collections::{BTreeMap, HashMap};

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
            "Which degraded optional inputs should be restored for the next report?".to_string(),
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
    !matches!(node_type, "file" | "module" | "unresolved" | "external")
}
