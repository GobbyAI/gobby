//! Transport-free graph analytics for code and knowledge graphs.

use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};

mod leiden;

#[derive(Debug, Clone, PartialEq)]
pub struct AnalyticsNode {
    pub id: String,
    pub kind: String,
    pub weight: f64,
}

/// An edge in an analytics graph.
///
/// `weight` is a finite positive coupling strength consumed by community
/// detection; default it via [`weight_for_kind`] at construction sites.
/// `f64` is why this type is `PartialEq` but not `Eq`.
#[derive(Debug, Clone, PartialEq)]
pub struct AnalyticsEdge {
    pub source: String,
    pub target: String,
    pub kind: String,
    pub weight: f64,
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
    let (bridges, _) = prepared.bridge_nodes_and_edges();
    let (communities, memberships) = prepared.communities();
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

/// Default coupling weight for an edge of the given relationship `kind`.
///
/// These are starting values, tuned against modularity quality: structural
/// containment and inheritance couple more tightly than loose references. The
/// match is case-insensitive and accepts the singular/plural/UPPERCASE spellings
/// produced across gcode payloads, codewiki, and gwiki provenance. Unknown kinds
/// fall back to `1.0`. The result is always finite and positive; sanitizing the
/// public `AnalyticsEdge.weight` (NaN/∞/≤0) happens at the adapter boundary.
pub fn weight_for_kind(kind: &str) -> f64 {
    match kind.to_ascii_lowercase().as_str() {
        "contains" | "member" => 3.0,    // structural containment
        "extends" | "implements" => 2.5, // inheritance
        "import" | "imports" => 2.0,     // module dependency
        "call" | "calls" => 1.5,         // call coupling
        "cites" | "supports" => 1.5,     // gwiki provenance
        "references" | "refers" | "uses" | "callers" => 1.0,
        "links" | "link" | "neighbor" | "relates" => 1.0,
        _ => 1.0, // unknown DB rel-types, "changed", etc.
    }
}

#[derive(Debug, Clone)]
struct PreparedEdge {
    source: usize,
    target: usize,
    weight: f64,
    edge_ref: EdgeRef,
}

#[derive(Debug, Clone)]
struct PreparedGraph {
    nodes: Vec<NodeRef>,
    weights: Vec<f64>,
    weights_by_id: HashMap<String, f64>,
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
        let weights_by_id = nodes
            .iter()
            .map(|(id, _, weight)| (id.clone(), *weight))
            .collect::<HashMap<_, _>>();
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
                    weight: edge.weight,
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
            weights_by_id,
            edges,
            adjacency,
        }
    }

    fn centrality(&self) -> Vec<CentralityScore> {
        // Normalized degree centrality uses n - 1 as the maximum possible
        // neighbor count in the full graph. In disconnected graphs this keeps
        // component scores comparable to the whole input, not to component size.
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

    /// Partition the graph into communities with weighted Leiden.
    ///
    /// Replaces the old Tarjan bridge-cut heuristic. Isolated nodes (and every
    /// node when there are no usable edges) fall out as their own singleton
    /// community; each connected component is partitioned under one global `m`.
    /// Returns the communities (sorted by their smallest member id, then size)
    /// and a node-id → community-index membership map.
    fn communities(&self) -> (Vec<Community>, HashMap<String, usize>) {
        if self.nodes.is_empty() {
            return (Vec::new(), HashMap::new());
        }

        // Build a weighted edge list for Leiden, sanitizing public weights:
        // non-finite or non-positive weights fall back to 1.0.
        let mut total_weight = 0.0_f64;
        let edges: Vec<(usize, usize, f64)> = self
            .edges
            .iter()
            .map(|edge| {
                let weight = if edge.weight.is_finite() && edge.weight > 0.0 {
                    edge.weight
                } else {
                    1.0
                };
                total_weight += weight;
                (edge.source, edge.target, weight)
            })
            .collect();

        let memberships = if total_weight < f64::EPSILON {
            // Nodes but no usable edges: every node is its own community.
            (0..self.nodes.len()).collect::<Vec<_>>()
        } else {
            let graph = leiden::LeidenGraph::new(self.nodes.len(), &edges);
            leiden::detect_communities(&graph, leiden::DEFAULT_GAMMA)
        };

        // Group node indices by community id (Leiden returns a dense labeling).
        let community_count = memberships.iter().copied().max().map_or(0, |m| m + 1);
        let mut groups: Vec<Vec<usize>> = vec![Vec::new(); community_count];
        for (node, &comm) in memberships.iter().enumerate() {
            groups[comm].push(node);
        }
        for group in &mut groups {
            group.sort_unstable();
        }
        groups.sort_by(|left, right| {
            self.nodes[left[0]]
                .id
                .cmp(&self.nodes[right[0]].id)
                .then_with(|| left.len().cmp(&right.len()))
        });

        let mut memberships_by_id = HashMap::new();
        let communities = groups
            .into_iter()
            .enumerate()
            .map(|(index, group)| {
                let nodes = group
                    .iter()
                    .map(|&node| {
                        memberships_by_id.insert(self.nodes[node].id.clone(), index);
                        self.nodes[node].clone()
                    })
                    .collect::<Vec<_>>();
                let weight = group.iter().map(|&node| self.weights[node]).sum();
                Community {
                    id: format!("community-{}", index + 1),
                    nodes,
                    weight,
                }
            })
            .collect::<Vec<_>>();

        (communities, memberships_by_id)
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

struct BridgeFrame {
    node: usize,
    parent_edge: Option<usize>,
    next_neighbor: usize,
    child_count: usize,
    is_articulation: bool,
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
        if self.discovery[node].is_some() {
            return;
        }
        self.discover(node);
        let mut stack = vec![BridgeFrame {
            node,
            parent_edge,
            next_neighbor: 0,
            child_count: 0,
            is_articulation: false,
        }];

        while let Some(frame_index) = stack.len().checked_sub(1) {
            let node = stack[frame_index].node;
            if stack[frame_index].next_neighbor < graph.adjacency[node].len() {
                let (neighbor, edge_index) =
                    graph.adjacency[node][stack[frame_index].next_neighbor];
                stack[frame_index].next_neighbor += 1;
                if Some(edge_index) == stack[frame_index].parent_edge {
                    continue;
                }

                if self.discovery[neighbor].is_none() {
                    stack[frame_index].child_count += 1;
                    self.discover(neighbor);
                    stack.push(BridgeFrame {
                        node: neighbor,
                        parent_edge: Some(edge_index),
                        next_neighbor: 0,
                        child_count: 0,
                        is_articulation: false,
                    });
                } else {
                    self.low[node] = self.low[node]
                        .min(self.discovery[neighbor].expect("neighbor already discovered"));
                }
                continue;
            }

            let mut finished = stack.pop().expect("frame exists");
            if finished.parent_edge.is_none() && finished.child_count > 1 {
                finished.is_articulation = true;
            }
            if finished.is_articulation {
                self.articulation_points.insert(finished.node);
            }

            if let Some(parent) = stack.last_mut() {
                let parent_node = parent.node;
                self.low[parent_node] = self.low[parent_node].min(self.low[finished.node]);
                let parent_discovery =
                    self.discovery[parent_node].expect("parent node already discovered");
                if let Some(edge_index) = finished.parent_edge
                    && self.low[finished.node] > parent_discovery
                {
                    self.bridge_edges.insert(edge_index);
                }
                if parent.parent_edge.is_some() && self.low[finished.node] >= parent_discovery {
                    parent.is_articulation = true;
                }
            }
        }
    }

    fn discover(&mut self, node: usize) {
        self.discovery[node] = Some(self.next);
        self.low[node] = self.next;
        self.next += 1;
    }
}

fn compare_edge_ref(left: &EdgeRef, right: &EdgeRef) -> Ordering {
    left.source
        .cmp(&right.source)
        .then_with(|| left.target.cmp(&right.target))
        .then_with(|| left.kind.cmp(&right.kind))
}

fn weight_for(node: &NodeRef, graph: &PreparedGraph) -> f64 {
    graph.weights_by_id.get(&node.id).copied().unwrap_or(0.0)
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
            weight: weight_for_kind(kind),
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

    #[test]
    fn weight_for_kind_covers_observed_aliases_case_insensitively() {
        let cases = [
            ("contains", 3.0),
            ("member", 3.0),
            ("extends", 2.5),
            ("implements", 2.5),
            ("import", 2.0),
            ("imports", 2.0),
            ("IMPORTS", 2.0),
            ("call", 1.5),
            ("calls", 1.5),
            ("CALLS", 1.5),
            ("cites", 1.5),
            ("supports", 1.5),
            ("references", 1.0),
            ("uses", 1.0),
            ("callers", 1.0),
            ("links", 1.0),
            ("neighbor", 1.0),
            ("relates", 1.0),
            ("changed", 1.0),
            ("totally-unknown-kind", 1.0),
        ];
        for (kind, expected) in cases {
            assert_eq!(weight_for_kind(kind), expected, "kind={kind}");
        }
    }

    #[test]
    fn analyze_empty_graph_does_not_panic() {
        let analytics = analyze(&AnalyticsGraph {
            nodes: Vec::new(),
            edges: Vec::new(),
        });
        assert!(analytics.communities.is_empty());
        assert!(analytics.centrality.is_empty());
        assert!(analytics.bridges.is_empty());
    }

    #[test]
    fn analyze_nodes_without_edges_yields_singleton_communities() {
        let nodes = ["a", "b", "c"]
            .into_iter()
            .map(|id| AnalyticsNode {
                id: id.to_string(),
                kind: "symbol".to_string(),
                weight: 1.0,
            })
            .collect();
        let analytics = analyze(&AnalyticsGraph {
            nodes,
            edges: Vec::new(),
        });
        assert_eq!(analytics.communities.len(), 3);
        for community in &analytics.communities {
            assert_eq!(community.nodes.len(), 1);
        }
    }
}
