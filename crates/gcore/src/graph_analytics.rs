//! Transport-free graph analytics for code and knowledge graphs.

use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};

#[derive(Debug, Clone, PartialEq)]
pub struct AnalyticsNode {
    pub id: String,
    pub kind: String,
    pub weight: f64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AnalyticsEdge {
    pub source: String,
    pub target: String,
    pub kind: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AnalyticsGraph {
    pub nodes: Vec<AnalyticsNode>,
    pub edges: Vec<AnalyticsEdge>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Community {
    pub id: String,
    pub nodes: Vec<NodeRef>,
    pub weight: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct CentralityScore {
    pub node: NodeRef,
    pub degree: usize,
    pub score: f64,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct NodeRef {
    pub id: String,
    pub kind: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EdgeRef {
    pub source: String,
    pub target: String,
    pub kind: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Hotspot {
    pub node: NodeRef,
    pub frequency: usize,
    pub weight: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct GraphAnalytics {
    pub communities: Vec<Community>,
    pub centrality: Vec<CentralityScore>,
    pub bridges: Vec<NodeRef>,
    pub god_nodes: Vec<NodeRef>,
    pub unexpected_links: Vec<EdgeRef>,
    pub hotspots: Vec<Hotspot>,
}

pub fn analyze(graph: &AnalyticsGraph) -> GraphAnalytics {
    let prepared = PreparedGraph::new(graph);
    let (bridges, bridge_edges) = prepared.bridge_nodes_and_edges();
    let (communities, memberships) = prepared.communities_without_bridges(&bridge_edges);
    let centrality = prepared.centrality();
    let god_nodes = prepared.god_nodes(&centrality);
    let unexpected_links = prepared.unexpected_links(&memberships);
    let hotspots = prepared.hotspots();

    GraphAnalytics {
        communities,
        centrality,
        bridges,
        god_nodes,
        unexpected_links,
        hotspots,
    }
}

#[derive(Debug, Clone)]
struct PreparedEdge {
    source: usize,
    target: usize,
    edge_ref: EdgeRef,
}

#[derive(Debug, Clone)]
struct PreparedGraph {
    nodes: Vec<NodeRef>,
    weights: Vec<f64>,
    edges: Vec<PreparedEdge>,
    adjacency: Vec<Vec<(usize, usize)>>,
}

impl PreparedGraph {
    fn new(graph: &AnalyticsGraph) -> Self {
        let mut nodes = graph
            .nodes
            .iter()
            .map(|node| {
                (
                    node.id.clone(),
                    NodeRef {
                        id: node.id.clone(),
                        kind: node.kind.clone(),
                    },
                    node.weight,
                )
            })
            .collect::<Vec<_>>();
        nodes.sort_by(|left, right| left.0.cmp(&right.0));
        nodes.dedup_by(|left, right| left.0 == right.0);

        let node_refs = nodes
            .iter()
            .map(|(_, node_ref, _)| node_ref.clone())
            .collect::<Vec<_>>();
        let weights = nodes
            .iter()
            .map(|(_, _, weight)| *weight)
            .collect::<Vec<_>>();
        let indexes = nodes
            .iter()
            .enumerate()
            .map(|(index, (id, _, _))| (id.as_str(), index))
            .collect::<HashMap<_, _>>();

        let mut edges = graph
            .edges
            .iter()
            .filter_map(|edge| {
                let source = indexes.get(edge.source.as_str()).copied()?;
                let target = indexes.get(edge.target.as_str()).copied()?;
                (source != target).then(|| PreparedEdge {
                    source,
                    target,
                    edge_ref: EdgeRef {
                        source: edge.source.clone(),
                        target: edge.target.clone(),
                        kind: edge.kind.clone(),
                    },
                })
            })
            .collect::<Vec<_>>();
        edges.sort_by(|left, right| compare_edge_ref(&left.edge_ref, &right.edge_ref));
        edges.dedup_by(|left, right| left.edge_ref == right.edge_ref);

        let mut adjacency = vec![Vec::new(); node_refs.len()];
        for (edge_index, edge) in edges.iter().enumerate() {
            adjacency[edge.source].push((edge.target, edge_index));
            adjacency[edge.target].push((edge.source, edge_index));
        }
        for neighbors in &mut adjacency {
            neighbors.sort_unstable();
        }

        Self {
            nodes: node_refs,
            weights,
            edges,
            adjacency,
        }
    }

    fn centrality(&self) -> Vec<CentralityScore> {
        let denominator = self.nodes.len().saturating_sub(1) as f64;
        let mut scores = self
            .nodes
            .iter()
            .enumerate()
            .map(|(index, node)| {
                let degree = self.unique_neighbors(index).len();
                let score = if denominator == 0.0 {
                    0.0
                } else {
                    degree as f64 / denominator
                };
                CentralityScore {
                    node: node.clone(),
                    degree,
                    score,
                }
            })
            .collect::<Vec<_>>();

        scores.sort_by(|left, right| {
            right
                .degree
                .cmp(&left.degree)
                .then_with(|| {
                    right
                        .score
                        .partial_cmp(&left.score)
                        .unwrap_or(Ordering::Equal)
                })
                .then_with(|| {
                    weight_for(&right.node, self)
                        .partial_cmp(&weight_for(&left.node, self))
                        .unwrap_or(Ordering::Equal)
                })
                .then_with(|| left.node.id.cmp(&right.node.id))
        });
        scores
    }

    fn bridge_nodes_and_edges(&self) -> (Vec<NodeRef>, HashSet<usize>) {
        let mut state = BridgeSearch::new(self.nodes.len());
        for node in 0..self.nodes.len() {
            if state.discovery[node].is_none() {
                state.visit(node, None, self);
            }
        }

        let mut bridges = state
            .articulation_points
            .into_iter()
            .map(|index| self.nodes[index].clone())
            .collect::<Vec<_>>();
        bridges.sort_by(|left, right| left.id.cmp(&right.id));
        (bridges, state.bridge_edges)
    }

    fn communities_without_bridges(
        &self,
        bridge_edges: &HashSet<usize>,
    ) -> (Vec<Community>, HashMap<String, usize>) {
        let mut visited = vec![false; self.nodes.len()];
        let mut components = Vec::new();

        for start in 0..self.nodes.len() {
            if visited[start] {
                continue;
            }
            let mut stack = vec![start];
            let mut component = Vec::new();
            visited[start] = true;

            while let Some(node) = stack.pop() {
                component.push(node);
                for (neighbor, edge_index) in &self.adjacency[node] {
                    if bridge_edges.contains(edge_index) || visited[*neighbor] {
                        continue;
                    }
                    visited[*neighbor] = true;
                    stack.push(*neighbor);
                }
            }

            component.sort_unstable();
            components.push(component);
        }

        components.sort_by(|left, right| {
            self.nodes[left[0]]
                .id
                .cmp(&self.nodes[right[0]].id)
                .then_with(|| left.len().cmp(&right.len()))
        });

        let mut memberships = HashMap::new();
        let communities = components
            .into_iter()
            .enumerate()
            .map(|(index, component)| {
                let nodes = component
                    .iter()
                    .map(|node| {
                        memberships.insert(self.nodes[*node].id.clone(), index);
                        self.nodes[*node].clone()
                    })
                    .collect::<Vec<_>>();
                let weight = component.iter().map(|node| self.weights[*node]).sum();
                Community {
                    id: format!("community-{}", index + 1),
                    nodes,
                    weight,
                }
            })
            .collect::<Vec<_>>();

        (communities, memberships)
    }

    fn god_nodes(&self, centrality: &[CentralityScore]) -> Vec<NodeRef> {
        let Some(max_degree) = centrality.iter().map(|score| score.degree).max() else {
            return Vec::new();
        };
        if max_degree < 3 {
            return Vec::new();
        }

        centrality
            .iter()
            .filter(|score| score.degree == max_degree)
            .map(|score| score.node.clone())
            .collect()
    }

    fn unexpected_links(&self, memberships: &HashMap<String, usize>) -> Vec<EdgeRef> {
        self.edges
            .iter()
            .filter_map(|edge| {
                let source = memberships.get(&edge.edge_ref.source)?;
                let target = memberships.get(&edge.edge_ref.target)?;
                (source != target).then(|| edge.edge_ref.clone())
            })
            .collect()
    }

    fn hotspots(&self) -> Vec<Hotspot> {
        let mut frequencies = vec![0_usize; self.nodes.len()];
        for edge in &self.edges {
            frequencies[edge.source] += 1;
            frequencies[edge.target] += 1;
        }

        let Some(max_frequency) = frequencies.iter().copied().max() else {
            return Vec::new();
        };
        if max_frequency < 3 {
            return Vec::new();
        }

        let mut hotspots = self
            .nodes
            .iter()
            .enumerate()
            .filter(|(index, _)| frequencies[*index] == max_frequency)
            .map(|(index, node)| Hotspot {
                node: node.clone(),
                frequency: frequencies[index],
                weight: self.weights[index],
            })
            .collect::<Vec<_>>();
        hotspots.sort_by(|left, right| {
            right
                .frequency
                .cmp(&left.frequency)
                .then_with(|| {
                    right
                        .weight
                        .partial_cmp(&left.weight)
                        .unwrap_or(Ordering::Equal)
                })
                .then_with(|| left.node.id.cmp(&right.node.id))
        });
        hotspots
    }

    fn unique_neighbors(&self, index: usize) -> HashSet<usize> {
        self.adjacency[index]
            .iter()
            .map(|(neighbor, _)| *neighbor)
            .collect()
    }
}

struct BridgeSearch {
    next: usize,
    discovery: Vec<Option<usize>>,
    low: Vec<usize>,
    articulation_points: HashSet<usize>,
    bridge_edges: HashSet<usize>,
}

impl BridgeSearch {
    fn new(node_count: usize) -> Self {
        Self {
            next: 0,
            discovery: vec![None; node_count],
            low: vec![0; node_count],
            articulation_points: HashSet::new(),
            bridge_edges: HashSet::new(),
        }
    }

    fn visit(&mut self, node: usize, parent_edge: Option<usize>, graph: &PreparedGraph) {
        self.discovery[node] = Some(self.next);
        self.low[node] = self.next;
        self.next += 1;

        let mut child_count = 0;
        let mut is_articulation = false;

        for (neighbor, edge_index) in &graph.adjacency[node] {
            if Some(*edge_index) == parent_edge {
                continue;
            }

            if self.discovery[*neighbor].is_none() {
                child_count += 1;
                self.visit(*neighbor, Some(*edge_index), graph);
                self.low[node] = self.low[node].min(self.low[*neighbor]);

                if self.low[*neighbor] > self.discovery[node].unwrap_or(0) {
                    self.bridge_edges.insert(*edge_index);
                }
                if parent_edge.is_some() && self.low[*neighbor] >= self.discovery[node].unwrap_or(0)
                {
                    is_articulation = true;
                }
            } else {
                self.low[node] = self.low[node].min(self.discovery[*neighbor].unwrap_or(0));
            }
        }

        if parent_edge.is_none() && child_count > 1 {
            is_articulation = true;
        }
        if is_articulation {
            self.articulation_points.insert(node);
        }
    }
}

fn compare_edge_ref(left: &EdgeRef, right: &EdgeRef) -> Ordering {
    left.source
        .cmp(&right.source)
        .then_with(|| left.target.cmp(&right.target))
        .then_with(|| left.kind.cmp(&right.kind))
}

fn weight_for(node: &NodeRef, graph: &PreparedGraph) -> f64 {
    graph
        .nodes
        .iter()
        .position(|candidate| candidate.id == node.id)
        .map(|index| graph.weights[index])
        .unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seeded_graph() -> AnalyticsGraph {
        let nodes = ["a", "b", "c", "d", "e", "f"]
            .into_iter()
            .map(|id| AnalyticsNode {
                id: id.to_string(),
                kind: "symbol".to_string(),
                weight: if id == "c" { 5.0 } else { 1.0 },
            })
            .collect();

        let edges = [
            ("a", "b", "calls"),
            ("b", "c", "calls"),
            ("c", "a", "imports"),
            ("c", "d", "relates"),
            ("d", "e", "calls"),
            ("e", "f", "calls"),
            ("f", "d", "imports"),
        ]
        .into_iter()
        .map(|(source, target, kind)| AnalyticsEdge {
            source: source.to_string(),
            target: target.to_string(),
            kind: kind.to_string(),
        })
        .collect();

        AnalyticsGraph { nodes, edges }
    }

    #[test]
    fn graph_analytics_detects_seeded_graph_measures() {
        let analytics = analyze(&seeded_graph());

        assert_eq!(analytics.communities.len(), 2);
        assert_eq!(
            analytics
                .communities
                .iter()
                .map(|community| community
                    .nodes
                    .iter()
                    .map(|node| node.id.as_str())
                    .collect::<Vec<_>>())
                .collect::<Vec<_>>(),
            vec![vec!["a", "b", "c"], vec!["d", "e", "f"]]
        );

        let centrality = analytics
            .centrality
            .iter()
            .map(|score| (score.node.id.as_str(), score.degree, score.score))
            .collect::<Vec<_>>();
        assert_eq!(centrality[0], ("c", 3, 0.6));
        assert_eq!(centrality[1], ("d", 3, 0.6));

        assert_eq!(
            analytics
                .bridges
                .iter()
                .map(|node| node.id.as_str())
                .collect::<Vec<_>>(),
            vec!["c", "d"]
        );
        assert_eq!(
            analytics
                .god_nodes
                .iter()
                .map(|node| node.id.as_str())
                .collect::<Vec<_>>(),
            vec!["c", "d"]
        );
        assert_eq!(
            analytics
                .unexpected_links
                .iter()
                .map(|edge| (
                    edge.source.as_str(),
                    edge.target.as_str(),
                    edge.kind.as_str()
                ))
                .collect::<Vec<_>>(),
            vec![("c", "d", "relates")]
        );
        assert_eq!(
            analytics
                .hotspots
                .iter()
                .map(|hotspot| (hotspot.node.id.as_str(), hotspot.frequency, hotspot.weight))
                .collect::<Vec<_>>(),
            vec![("c", 3, 5.0), ("d", 3, 1.0)]
        );
    }
}
