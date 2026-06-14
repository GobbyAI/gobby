use std::collections::BTreeMap;

use gobby_core::graph_analytics::{
    AnalyticsEdge, AnalyticsGraph, AnalyticsNode, CentralityScore, Community, EdgeRef,
    GraphAnalytics, Hotspot, NodeRef, analyze, weight_for_kind,
};

use super::{
    MemoryWikiGraph, WikiGraphFacts, WikiGraphLinkTarget, citation_node, document_id,
    document_kind, source_node_id, unresolved_target_id,
};

#[derive(Debug, Clone, PartialEq)]
pub enum GraphAnalyticsError {
    DuplicateNode {
        id: String,
        existing_kind: String,
        duplicate_kind: String,
        existing_weight: f64,
        duplicate_weight: f64,
    },
}

impl std::fmt::Display for GraphAnalyticsError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::DuplicateNode {
                id,
                existing_kind,
                duplicate_kind,
                existing_weight,
                duplicate_weight,
            } => write!(
                f,
                "duplicate graph node `{id}` has conflicting metadata: existing kind `{existing_kind}` weight {existing_weight}, duplicate kind `{duplicate_kind}` weight {duplicate_weight}"
            ),
        }
    }
}

impl std::error::Error for GraphAnalyticsError {}

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

pub fn analyze_facts(facts: &WikiGraphFacts) -> Result<GraphExportAnalytics, GraphAnalyticsError> {
    Ok(GraphExportAnalytics::from_core(analyze(
        &analytics_graph_from_facts(facts)?,
    )))
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub fn analytics_graph_from_memory(
    graph: &MemoryWikiGraph,
) -> Result<AnalyticsGraph, GraphAnalyticsError> {
    analytics_graph_from_facts(&graph.facts)
}

pub fn analytics_graph_from_facts(
    facts: &WikiGraphFacts,
) -> Result<AnalyticsGraph, GraphAnalyticsError> {
    let mut nodes = BTreeMap::new();
    let mut edges = Vec::new();

    for document in &facts.documents {
        insert_node(
            &mut nodes,
            document_id(&document.scope, &document.path),
            document_kind(&document.path),
            1.0,
        )?;
    }

    for source in &facts.sources {
        let source_id = source_node_id(&source.scope, &source.source_path);
        insert_node(&mut nodes, source_id.clone(), "source", 0.5)?;

        let citation_id = citation_node(source).id;
        insert_node(&mut nodes, citation_id.clone(), "citation", 0.25)?;

        edges.push(AnalyticsEdge {
            source: source_id,
            target: document_id(&source.scope, &source.document_path),
            kind: "supports".to_string(),
            weight: weight_for_kind("supports"),
        });
        edges.push(AnalyticsEdge {
            source: citation_id,
            target: source_node_id(&source.scope, &source.source_path),
            kind: "cites".to_string(),
            weight: weight_for_kind("cites"),
        });
    }

    for link in &facts.links {
        let target = match &link.target {
            WikiGraphLinkTarget::Resolved(path) => {
                let node_id = document_id(&link.scope, path);
                insert_node(&mut nodes, node_id.clone(), document_kind(path), 1.0)?;
                node_id
            }
            WikiGraphLinkTarget::Unresolved(target) => {
                let node_id = unresolved_target_id(&link.scope, target);
                insert_node(&mut nodes, node_id.clone(), "unresolved_target", 0.25)?;
                node_id
            }
        };
        edges.push(AnalyticsEdge {
            source: document_id(&link.scope, &link.source_path),
            target,
            kind: "links".to_string(),
            weight: weight_for_kind("links"),
        });
    }

    Ok(AnalyticsGraph {
        nodes: nodes.into_values().collect(),
        edges,
    })
}

fn insert_node(
    nodes: &mut BTreeMap<String, AnalyticsNode>,
    id: String,
    kind: impl Into<String>,
    weight: f64,
) -> Result<(), GraphAnalyticsError> {
    let kind = kind.into();
    if let Some(existing) = nodes.get(&id) {
        if existing.kind != kind || existing.weight != weight {
            return Err(GraphAnalyticsError::DuplicateNode {
                id,
                existing_kind: existing.kind.clone(),
                duplicate_kind: kind,
                existing_weight: existing.weight,
                duplicate_weight: weight,
            });
        }
        return Ok(());
    }
    nodes.insert(id.clone(), AnalyticsNode { id, kind, weight });
    Ok(())
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
    use std::path::PathBuf;

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
                    path: "knowledge/topics/a.md".into(),
                    title: None,
                },
                WikiGraphDocument {
                    scope: scope.clone(),
                    path: "knowledge/topics/b.md".into(),
                    title: None,
                },
            ],
            links: vec![WikiGraphLink {
                scope,
                source_path: "knowledge/topics/a.md".into(),
                raw_target: "B".to_string(),
                target: WikiGraphLinkTarget::Resolved("knowledge/topics/b.md".into()),
            }],
            sources: Vec::new(),
            code_edges: Vec::new(),
        });

        let analytics_graph = analytics_graph_from_memory(&graph).expect("analytics graph");

        assert_eq!(analytics_graph.nodes.len(), 2);
        assert_eq!(analytics_graph.edges.len(), 1);
        assert_eq!(analytics_graph.edges[0].kind, "links");
    }

    #[test]
    fn graph_analytics_adds_placeholder_for_missing_resolved_target() {
        let scope = SearchScope::project("project-1");
        let mut graph = MemoryWikiGraph::default();
        graph.replace_facts(WikiGraphFacts {
            documents: vec![WikiGraphDocument {
                scope: scope.clone(),
                path: "knowledge/topics/a.md".into(),
                title: None,
            }],
            links: vec![WikiGraphLink {
                scope: scope.clone(),
                source_path: "knowledge/topics/a.md".into(),
                raw_target: "B".to_string(),
                target: WikiGraphLinkTarget::Resolved("knowledge/topics/b.md".into()),
            }],
            sources: Vec::new(),
            code_edges: Vec::new(),
        });

        let analytics_graph = analytics_graph_from_memory(&graph).expect("analytics graph");
        let target_id = document_id(&scope, &PathBuf::from("knowledge/topics/b.md"));

        assert!(analytics_graph.nodes.iter().any(|node| {
            node.id == target_id && node.kind == "wiki_page" && node.weight == 1.0
        }));
    }

    #[test]
    fn graph_analytics_rejects_duplicate_node_metadata() {
        let mut nodes = BTreeMap::new();
        insert_node(&mut nodes, "node-1".to_string(), "topic", 1.0).expect("first insert");

        let error = insert_node(&mut nodes, "node-1".to_string(), "source", 0.5)
            .expect_err("duplicate node must fail");

        assert!(matches!(
            error,
            GraphAnalyticsError::DuplicateNode {
                existing_kind,
                duplicate_kind,
                ..
            } if existing_kind == "topic" && duplicate_kind == "source"
        ));
    }
}
