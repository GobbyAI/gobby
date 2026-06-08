use std::collections::BTreeMap;

use gobby_core::graph_analytics::{
    AnalyticsEdge, AnalyticsGraph, AnalyticsNode, CentralityScore, Community, EdgeRef,
    GraphAnalytics, Hotspot, NodeRef, analyze,
};

use super::{
    MemoryWikiGraph, WikiGraphFacts, WikiGraphLinkTarget, citation_node, document_id,
    document_kind, source_node_id, unresolved_target_id,
};

#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct GraphExportAnalytics {
    pub communities: Vec<GraphExportCommunity>,
    pub centrality: Vec<GraphExportCentrality>,
    pub bridges: Vec<GraphExportNodeRef>,
    pub god_nodes: Vec<GraphExportNodeRef>,
    pub unexpected_links: Vec<GraphExportEdgeRef>,
    pub hotspots: Vec<GraphExportHotspot>,
}

#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct GraphExportCommunity {
    pub id: String,
    pub nodes: Vec<GraphExportNodeRef>,
    pub weight: f64,
}

#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct GraphExportCentrality {
    pub node: GraphExportNodeRef,
    pub degree: usize,
    pub score: f64,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct GraphExportNodeRef {
    pub id: String,
    pub kind: String,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct GraphExportEdgeRef {
    pub source: String,
    pub target: String,
    pub kind: String,
}

#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct GraphExportHotspot {
    pub node: GraphExportNodeRef,
    pub frequency: usize,
    pub weight: f64,
}

pub fn analyze_facts(facts: &WikiGraphFacts) -> GraphExportAnalytics {
    GraphExportAnalytics::from_core(analyze(&analytics_graph_from_facts(facts)))
}

pub fn analytics_graph_from_memory(graph: &MemoryWikiGraph) -> AnalyticsGraph {
    analytics_graph_from_facts(&graph.facts)
}

pub fn analytics_graph_from_facts(facts: &WikiGraphFacts) -> AnalyticsGraph {
    let mut nodes = BTreeMap::new();
    let mut edges = Vec::new();

    for document in &facts.documents {
        insert_node(
            &mut nodes,
            document_id(&document.path),
            document_kind(&document.path),
            1.0,
        );
    }

    for source in &facts.sources {
        let source_id = source_node_id(&source.source_path);
        insert_node(&mut nodes, source_id.clone(), "source", 0.5);

        let citation_id = citation_node(source).id;
        insert_node(&mut nodes, citation_id.clone(), "citation", 0.25);

        edges.push(AnalyticsEdge {
            source: source_id,
            target: document_id(&source.document_path),
            kind: "supports".to_string(),
        });
        edges.push(AnalyticsEdge {
            source: citation_id,
            target: source_node_id(&source.source_path),
            kind: "cites".to_string(),
        });
    }

    for link in &facts.links {
        let target = match &link.target {
            WikiGraphLinkTarget::Resolved(path) => document_id(path),
            WikiGraphLinkTarget::Unresolved(target) => {
                let node_id = unresolved_target_id(target);
                insert_node(&mut nodes, node_id.clone(), "unresolved_target", 0.25);
                node_id
            }
        };
        edges.push(AnalyticsEdge {
            source: document_id(&link.source_path),
            target,
            kind: "links".to_string(),
        });
    }

    AnalyticsGraph {
        nodes: nodes.into_values().collect(),
        edges,
    }
}

fn insert_node(
    nodes: &mut BTreeMap<String, AnalyticsNode>,
    id: String,
    kind: impl Into<String>,
    weight: f64,
) {
    nodes.entry(id.clone()).or_insert_with(|| AnalyticsNode {
        id,
        kind: kind.into(),
        weight,
    });
}

impl GraphExportAnalytics {
    fn from_core(analytics: GraphAnalytics) -> Self {
        Self {
            communities: analytics
                .communities
                .into_iter()
                .map(GraphExportCommunity::from_core)
                .collect(),
            centrality: analytics
                .centrality
                .into_iter()
                .map(GraphExportCentrality::from_core)
                .collect(),
            bridges: analytics
                .bridges
                .into_iter()
                .map(GraphExportNodeRef::from)
                .collect(),
            god_nodes: analytics
                .god_nodes
                .into_iter()
                .map(GraphExportNodeRef::from)
                .collect(),
            unexpected_links: analytics
                .unexpected_links
                .into_iter()
                .map(GraphExportEdgeRef::from)
                .collect(),
            hotspots: analytics
                .hotspots
                .into_iter()
                .map(GraphExportHotspot::from_core)
                .collect(),
        }
    }
}

impl GraphExportCommunity {
    fn from_core(community: Community) -> Self {
        Self {
            id: community.id,
            nodes: community
                .nodes
                .into_iter()
                .map(GraphExportNodeRef::from)
                .collect(),
            weight: community.weight,
        }
    }
}

impl GraphExportCentrality {
    fn from_core(score: CentralityScore) -> Self {
        Self {
            node: GraphExportNodeRef::from(score.node),
            degree: score.degree,
            score: score.score,
        }
    }
}

impl GraphExportHotspot {
    fn from_core(hotspot: Hotspot) -> Self {
        Self {
            node: GraphExportNodeRef::from(hotspot.node),
            frequency: hotspot.frequency,
            weight: hotspot.weight,
        }
    }
}

impl From<NodeRef> for GraphExportNodeRef {
    fn from(node: NodeRef) -> Self {
        Self {
            id: node.id,
            kind: node.kind,
        }
    }
}

impl From<EdgeRef> for GraphExportEdgeRef {
    fn from(edge: EdgeRef) -> Self {
        Self {
            source: edge.source,
            target: edge.target,
            kind: edge.kind,
        }
    }
}

#[cfg(test)]
mod tests {
    use crate::graph::{
        MemoryWikiGraph, WikiGraphDocument, WikiGraphFacts, WikiGraphLink, WikiGraphLinkTarget,
    };
    use crate::search::SearchScope;

    use super::*;

    #[test]
    fn graph_analytics_converts_memory_graph_to_core_graph() {
        let scope = SearchScope::project("project-1");
        let mut graph = MemoryWikiGraph::default();
        graph.replace_facts(WikiGraphFacts {
            documents: vec![
                WikiGraphDocument {
                    scope: scope.clone(),
                    path: "wiki/a.md".into(),
                    title: None,
                },
                WikiGraphDocument {
                    scope: scope.clone(),
                    path: "wiki/b.md".into(),
                    title: None,
                },
            ],
            links: vec![WikiGraphLink {
                scope,
                source_path: "wiki/a.md".into(),
                raw_target: "B".to_string(),
                target: WikiGraphLinkTarget::Resolved("wiki/b.md".into()),
            }],
            sources: Vec::new(),
        });

        let analytics_graph = analytics_graph_from_memory(&graph);

        assert_eq!(analytics_graph.nodes.len(), 2);
        assert_eq!(analytics_graph.edges.len(), 1);
        assert_eq!(analytics_graph.edges[0].kind, "links");
    }
}
