//! Module-coupling graph walker for `gcode graph view --view=mcg`.

mod fetch;
mod identity;

use std::collections::btree_map::Entry;
use std::collections::{BTreeMap, HashMap, HashSet};

use gobby_core::graph_analytics::{
    AnalyticsEdge, AnalyticsGraph, AnalyticsNode, analyze, weight_for_kind,
};

use crate::codewiki_facts::PublicEdge;

use super::render::{NodeKey, ViewCommunity, ViewEdgeInput, ViewNodeInput};
use super::{
    CandidateEndpoint, CandidateEndpointKind, ViewEdgeCandidate, VisibleFileMap,
    take_visible_before_bound,
};

pub(crate) use fetch::run;
pub(crate) use identity::McgSeedSelector;

pub(super) struct McgHopFetch {
    pub incoming: Vec<ViewEdgeCandidate>,
    pub outgoing: Vec<ViewEdgeCandidate>,
    pub incoming_truncated: bool,
    pub outgoing_truncated: bool,
}

pub(super) struct McgWalk {
    pub nodes: Vec<ViewNodeInput>,
    pub edges: Vec<ViewEdgeInput>,
    pub incoming_truncated: bool,
    pub outgoing_truncated: bool,
}

pub(super) fn walk_mcg(
    seeds: Vec<CandidateEndpoint>,
    depth: u32,
    incoming_limit: usize,
    outgoing_limit: usize,
    mut visible_of: impl FnMut(&[ViewEdgeCandidate]) -> anyhow::Result<VisibleFileMap>,
    mut fetch: impl FnMut(
        &[CandidateEndpoint],
        &[CandidateEndpoint],
        &HashSet<PublicEdge>,
    ) -> anyhow::Result<McgHopFetch>,
    mut close: impl FnMut(&CandidateEndpoint) -> anyhow::Result<Vec<CandidateEndpoint>>,
) -> anyhow::Result<McgWalk> {
    let mut visited = HashSet::new();
    let mut typed_emitted = HashSet::new();
    let mut untyped_emitted = HashSet::new();
    let mut nodes = BTreeMap::new();
    let mut frontier = Vec::new();
    for endpoint in seeds {
        let key = upsert_node(&mut nodes, &endpoint);
        if visited.insert(key) {
            frontier.push(endpoint);
        }
    }
    let mut incoming_truncated = false;
    let mut outgoing_truncated = false;
    let mut edges = Vec::new();

    for _ in 0..depth {
        if frontier.is_empty() {
            break;
        }
        let files = frontier
            .iter()
            .filter(|endpoint| endpoint.kind == CandidateEndpointKind::File)
            .cloned()
            .collect::<Vec<_>>();
        let modules = frontier
            .iter()
            .filter(|endpoint| endpoint.kind == CandidateEndpointKind::Module)
            .cloned()
            .collect::<Vec<_>>();
        let page = fetch(&files, &modules, &untyped_emitted)?;
        incoming_truncated |= page.incoming_truncated;
        outgoing_truncated |= page.outgoing_truncated;
        let mut hop_edges = page.incoming.clone();
        hop_edges.extend(page.outgoing.clone());
        let visible = visible_of(&hop_edges)?;
        let incoming =
            take_visible_before_bound(page.incoming, &visible, Some(incoming_limit), None);
        let outgoing =
            take_visible_before_bound(page.outgoing, &visible, Some(outgoing_limit), None);
        incoming_truncated |= incoming.truncated;
        outgoing_truncated |= outgoing.truncated;

        let mut next_frontier = Vec::new();
        for edge in incoming.edges.into_iter().chain(outgoing.edges) {
            let typed = PublicEdge::new(
                edge.source.key().canonical(),
                edge.target.key().canonical(),
                &edge.rel,
            );
            if !typed_emitted.insert(typed) {
                continue;
            }
            untyped_emitted.insert(PublicEdge::new(&edge.source.id, &edge.target.id, &edge.rel));
            let source = upsert_node(&mut nodes, &edge.source);
            let target = upsert_node(&mut nodes, &edge.target);
            edges.push(ViewEdgeInput {
                source,
                target,
                rel: edge.rel,
            });
            consider_frontier(&mut next_frontier, &mut visited, &mut nodes, &edge.source);
            consider_frontier(&mut next_frontier, &mut visited, &mut nodes, &edge.target);
        }
        let discovered = next_frontier.clone();
        for endpoint in discovered {
            for extra in close(&endpoint)? {
                consider_frontier(&mut next_frontier, &mut visited, &mut nodes, &extra);
            }
        }
        frontier = next_frontier;
    }

    Ok(McgWalk {
        nodes: nodes.into_values().collect(),
        edges,
        incoming_truncated,
        outgoing_truncated,
    })
}

pub(super) fn assign_leiden_communities(
    mut nodes: Vec<ViewNodeInput>,
    edges: &[ViewEdgeInput],
) -> (Vec<ViewNodeInput>, Vec<ViewCommunity>) {
    let graph = AnalyticsGraph {
        nodes: nodes
            .iter()
            .map(|node| AnalyticsNode {
                id: node.key.canonical(),
                kind: node.kind.clone(),
                weight: 1.0,
            })
            .collect(),
        edges: edges
            .iter()
            .map(|edge| AnalyticsEdge {
                source: edge.source.canonical(),
                target: edge.target.canonical(),
                kind: edge.rel.clone(),
                weight: weight_for_kind(&edge.rel),
            })
            .collect(),
    };
    let analytics = analyze(&graph);
    let mut by_id = HashMap::new();
    let communities = analytics
        .communities
        .iter()
        .map(|community| {
            for node in &community.nodes {
                by_id.insert(node.id.clone(), community.id.clone());
            }
            ViewCommunity {
                id: community.id.clone(),
                nodes: community.nodes.iter().map(|node| node.id.clone()).collect(),
            }
        })
        .collect();
    for node in &mut nodes {
        node.community = by_id.get(&node.key.canonical()).cloned();
    }
    (nodes, communities)
}

fn expandable(kind: CandidateEndpointKind) -> bool {
    matches!(
        kind,
        CandidateEndpointKind::File | CandidateEndpointKind::Module
    )
}

/// Insert the endpoint's node, or upgrade an existing node's `file` from
/// `None` to the endpoint's provider. Never downgrades, so the order in which
/// an edge and its equivalence-class closure discover a module is irrelevant.
fn upsert_node(
    nodes: &mut BTreeMap<NodeKey, ViewNodeInput>,
    endpoint: &CandidateEndpoint,
) -> NodeKey {
    let node = endpoint.node();
    let key = node.key.clone();
    match nodes.entry(key.clone()) {
        Entry::Vacant(slot) => {
            slot.insert(node);
        }
        Entry::Occupied(mut slot) => {
            if slot.get().file.is_none() && node.file.is_some() {
                slot.get_mut().file = node.file;
            }
        }
    }
    key
}

fn consider_frontier(
    frontier: &mut Vec<CandidateEndpoint>,
    visited: &mut HashSet<NodeKey>,
    nodes: &mut BTreeMap<NodeKey, ViewNodeInput>,
    endpoint: &CandidateEndpoint,
) {
    let key = upsert_node(nodes, endpoint);
    if expandable(endpoint.kind) && visited.insert(key) {
        frontier.push(endpoint.clone());
    }
}

#[cfg(test)]
mod tests;
