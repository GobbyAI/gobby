//! Transport-free graph analytics for code and knowledge graphs.

use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};
use std::fmt;

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

/// Input rejected by [`communities`]; [`analyze`] sanitizes the same conditions silently.
///
/// `Display` and `Error` are implemented by hand because a derived `thiserror::Error`
/// would treat the `source` endpoint field as the error's cause.
#[derive(Debug, Clone, PartialEq)]
pub enum GraphInputError {
    DuplicateNode {
        id: String,
    },
    UnknownEndpoint {
        source: String,
        target: String,
        kind: String,
    },
    InvalidWeight {
        source: String,
        target: String,
        kind: String,
        weight: f64,
    },
    SelfLoop {
        id: String,
        kind: String,
    },
}

impl fmt::Display for GraphInputError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DuplicateNode { id } => write!(f, "duplicate node id {id}"),
            Self::UnknownEndpoint {
                source,
                target,
                kind,
            } => write!(
                f,
                "edge {source} -> {target} ({kind}) references an unknown node"
            ),
            Self::InvalidWeight {
                source,
                target,
                kind,
                weight,
            } => write!(
                f,
                "edge {source} -> {target} ({kind}) has invalid weight {weight}"
            ),
            Self::SelfLoop { id, kind } => write!(f, "self-loop on {id} ({kind})"),
        }
    }
}

impl std::error::Error for GraphInputError {}

/// Leiden communities only. Validates input instead of sanitizing it.
///
/// Checks duplicate node ids first, then each edge in order for an unknown
/// endpoint, a self-loop, and a non-finite or non-positive weight. On success the
/// partition is exactly what [`analyze`] reports for the same graph: an empty graph
/// yields no communities and an edge-less graph one singleton per node.
pub fn communities(graph: &AnalyticsGraph) -> Result<Vec<Community>, GraphInputError> {
    validate_input(graph)?;
    Ok(PreparedGraph::new(graph).communities().0)
}

/// Degree centrality only. Validates input exactly like [`communities`].
///
/// On success the scores are exactly what [`analyze`] reports as `centrality` for the
/// same graph, without paying for the Leiden pass or the bridge search.
pub fn centrality(graph: &AnalyticsGraph) -> Result<Vec<CentralityScore>, GraphInputError> {
    validate_input(graph)?;
    Ok(PreparedGraph::new(graph).centrality())
}

fn validate_input(graph: &AnalyticsGraph) -> Result<(), GraphInputError> {
    let mut ids = HashSet::with_capacity(graph.nodes.len());
    for node in &graph.nodes {
        if !ids.insert(node.id.as_str()) {
            return Err(GraphInputError::DuplicateNode {
                id: node.id.clone(),
            });
        }
    }
    for edge in &graph.edges {
        if !ids.contains(edge.source.as_str()) || !ids.contains(edge.target.as_str()) {
            return Err(GraphInputError::UnknownEndpoint {
                source: edge.source.clone(),
                target: edge.target.clone(),
                kind: edge.kind.clone(),
            });
        }
        if edge.source == edge.target {
            return Err(GraphInputError::SelfLoop {
                id: edge.source.clone(),
                kind: edge.kind.clone(),
            });
        }
        if !(edge.weight.is_finite() && edge.weight > 0.0) {
            return Err(GraphInputError::InvalidWeight {
                source: edge.source.clone(),
                target: edge.target.clone(),
                kind: edge.kind.clone(),
                weight: edge.weight,
            });
        }
    }
    Ok(())
}

/// Default coupling weight for an edge of the given relationship `kind`.
///
/// These are starting values, tuned against modularity quality: structural
/// containment and inheritance couple more tightly than loose references. The
/// match is case-insensitive and accepts the singular/plural/UPPERCASE spellings
/// used by code graphs and provenance relationships. Unknown kinds
/// fall back to `1.0`. The result is always finite and positive; sanitizing the
/// public `AnalyticsEdge.weight` (NaN/∞/≤0) happens at the adapter boundary.
pub fn weight_for_kind(kind: &str) -> f64 {
    match kind.to_ascii_lowercase().as_str() {
        "contains" | "member" => 3.0,                 // structural containment
        "extends" | "implements" | "inherits" => 2.5, // inheritance
        "import" | "imports" => 2.0,                  // module dependency
        "call" | "calls" => 1.5,                      // call coupling
        "cites" | "supports" => 1.5,                  // provenance
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
#[path = "graph_analytics/tests.rs"]
mod tests;
