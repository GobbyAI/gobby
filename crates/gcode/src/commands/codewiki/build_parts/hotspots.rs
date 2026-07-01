use super::super::*;
use gobby_core::graph_analytics::{self, AnalyticsEdge, AnalyticsGraph, AnalyticsNode};
use std::collections::{BTreeMap, BTreeSet};

pub(crate) fn build_hotspots_doc(
    files: &[FileDoc],
    graph_edges: &[CodewikiGraphEdge],
    graph_availability: CodewikiGraphAvailability,
) -> HotspotsDoc {
    // Graph availability is informational only and never degrades the hotspots
    // page. When the graph is unavailable the centrality analytics simply
    // cannot run, so the page omits its findings (the renderer prints a plain
    // "no hotspots" note) without setting `degraded`.
    if graph_availability != CodewikiGraphAvailability::Available {
        return HotspotsDoc {
            source_spans: Vec::new(),
            hotspots: Vec::new(),
            god_nodes: Vec::new(),
            bridges: Vec::new(),
            degraded_sources: Vec::new(),
        };
    }

    let nodes = hotspot_nodes(files);
    let graph = AnalyticsGraph {
        nodes: nodes
            .values()
            .map(|node| AnalyticsNode {
                id: node.id.clone(),
                kind: node.kind.clone(),
                weight: node
                    .source_span
                    .as_ref()
                    .map(|span| {
                        span.line_end
                            .saturating_sub(span.line_start)
                            .saturating_add(1)
                    })
                    .unwrap_or(1) as f64,
            })
            .collect(),
        edges: graph_edges
            .iter()
            .filter(|edge| {
                nodes.contains_key(&edge.source_component_id)
                    && nodes.contains_key(&edge.target_component_id)
            })
            .map(|edge| {
                let kind = match edge.kind {
                    CodewikiGraphEdgeKind::Call => "call",
                    CodewikiGraphEdgeKind::Import => "import",
                };
                AnalyticsEdge {
                    source: edge.source_component_id.clone(),
                    target: edge.target_component_id.clone(),
                    kind: kind.to_string(),
                    weight: graph_analytics::weight_for_kind(kind),
                }
            })
            .collect(),
    };
    let analytics = graph_analytics::analyze(&graph);
    let centrality = analytics
        .centrality
        .iter()
        .map(|score| (score.node.id.clone(), (score.degree, score.score)))
        .collect::<BTreeMap<_, _>>();

    let hotspots = analytics
        .hotspots
        .into_iter()
        .filter_map(|hotspot| {
            let node = nodes.get(&hotspot.node.id)?.clone();
            let (degree, score) = centrality.get(&node.id).copied().unwrap_or_default();
            Some(HotspotFinding {
                node,
                degree: Some(degree),
                score: Some(score),
                frequency: Some(hotspot.frequency),
                weight: Some(hotspot.weight),
            })
        })
        .collect::<Vec<_>>();
    let god_nodes = analytics
        .god_nodes
        .into_iter()
        .filter_map(|node_ref| {
            let node = nodes.get(&node_ref.id)?.clone();
            let (degree, score) = centrality.get(&node.id).copied().unwrap_or_default();
            Some(HotspotFinding {
                node,
                degree: Some(degree),
                score: Some(score),
                frequency: None,
                weight: None,
            })
        })
        .collect::<Vec<_>>();
    let bridges = analytics
        .bridges
        .into_iter()
        .filter_map(|node_ref| {
            let node = nodes.get(&node_ref.id)?.clone();
            let (degree, score) = centrality.get(&node.id).copied().unwrap_or_default();
            Some(HotspotFinding {
                node,
                degree: Some(degree),
                score: Some(score),
                frequency: None,
                weight: None,
            })
        })
        .collect::<Vec<_>>();
    let source_spans = hotspots
        .iter()
        .chain(god_nodes.iter())
        .chain(bridges.iter())
        .filter_map(|finding| finding.node.source_span.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();

    HotspotsDoc {
        source_spans,
        hotspots,
        god_nodes,
        bridges,
        degraded_sources: Vec::new(),
    }
}

fn hotspot_nodes(files: &[FileDoc]) -> BTreeMap<String, HotspotNode> {
    files
        .iter()
        .flat_map(|file| {
            file.symbols.iter().map(|symbol| {
                let label = if symbol.symbol.qualified_name.is_empty() {
                    symbol.symbol.name.clone()
                } else {
                    symbol.symbol.qualified_name.clone()
                };
                (
                    symbol.component_id.clone(),
                    HotspotNode {
                        id: symbol.component_id.clone(),
                        kind: symbol.symbol.kind.clone(),
                        label: label.clone(),
                        wikilink: format!("[[code/files/{}|{}]]", file.path, label),
                        file_wikilink: Some(file_wikilink(&file.path)),
                        source_span: Some(symbol.source_span.clone()),
                    },
                )
            })
        })
        .collect()
}
