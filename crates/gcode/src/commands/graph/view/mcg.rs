//! Module-coupling graph walker for `gcode graph view --view=mcg`.

mod fetch;
mod identity;

use std::collections::btree_map::Entry;
use std::collections::{BTreeMap, HashMap, HashSet};

use crate::codewiki_facts::PublicEdge;
use crate::communities::identity::ImportIdentity;
use crate::communities::{MISSING_PARTITION_HINT, StoredCommunity};

use super::render::{NodeKey, NodeKind, ViewCommunity, ViewEdgeInput, ViewNodeInput};
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

pub(super) struct LabeledCommunities {
    pub nodes: Vec<ViewNodeInput>,
    pub communities: Vec<ViewCommunity>,
    pub hint: Option<String>,
}

pub(super) fn label_communities(
    mut nodes: Vec<ViewNodeInput>,
    stored: &[StoredCommunity],
    identity: &ImportIdentity,
) -> LabeledCommunities {
    if stored.is_empty() {
        for node in &mut nodes {
            node.community = None;
        }
        return LabeledCommunities {
            nodes,
            communities: Vec::new(),
            hint: Some(MISSING_PARTITION_HINT.to_string()),
        };
    }

    let by_member = stored
        .iter()
        .flat_map(|community| {
            community
                .members
                .iter()
                .map(move |member| (member.as_str(), community))
        })
        .collect::<HashMap<_, _>>();
    let mut visible = BTreeMap::<i32, (&StoredCommunity, Vec<String>)>::new();
    for node in &mut nodes {
        let member = match node.key.kind {
            NodeKind::File => Some(node.key.identity.clone()),
            NodeKind::Module => identity.unique_provider(&node.key.identity),
            NodeKind::Symbol | NodeKind::Community | NodeKind::External | NodeKind::Unresolved => {
                None
            }
        };
        let community = member
            .as_deref()
            .and_then(|member| by_member.get(member).copied());
        node.community = community
            .map(|community| NodeKey::community(community.community_id.to_string()).canonical());
        if let Some(community) = community {
            visible
                .entry(community.community_id)
                .or_insert_with(|| (community, Vec::new()))
                .1
                .push(node.key.canonical());
        }
    }
    let communities = visible
        .into_values()
        .map(|(community, mut view_nodes)| {
            view_nodes.sort();
            view_nodes.dedup();
            ViewCommunity {
                id: NodeKey::community(community.community_id.to_string()).canonical(),
                label: community.label.clone(),
                size: community.member_count,
                cohesion: community.cohesion,
                label_source: community.label_source.as_str().to_string(),
                label_stale: community.label_stale,
                nodes: view_nodes,
                first_member: community.members.first().cloned().unwrap_or_default(),
            }
        })
        .collect();
    LabeledCommunities {
        nodes,
        communities,
        hint: None,
    }
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
