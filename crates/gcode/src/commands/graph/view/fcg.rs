//! Function-call graph walker for `gcode graph view --view=fcg`.

use std::collections::{BTreeMap, HashSet};

use anyhow::Context as _;

use crate::cli::GraphViewArgs;
use crate::codewiki_facts::{
    CodewikiFacts, GraphAvailability, GraphBounds, GraphEdge, GraphEdgeKind, GraphOutcome,
    GraphScopeMode, MAX_DECLARED_EDGE_LIMIT, PublicEdge, ScopeSelector,
};
use crate::config::Context;
use crate::output::Format;
use crate::search::fts::ResolvedGraphSymbol;
use crate::visibility;

use super::render::{
    NodeKey, ViewEdgeInput, ViewNodeInput, ViewSeed, build_view_payload, print_view,
};
use super::{
    CandidateEndpoint, CandidateEndpointKind, ViewEdgeCandidate, VisibleFileMap, VisibleOwnerKey,
    hint_for_availability, take_visible_before_bound,
};

pub(super) struct FcgHopFetch {
    pub incoming: Vec<ViewEdgeCandidate>,
    pub outgoing: Vec<ViewEdgeCandidate>,
    pub incoming_truncated: bool,
    pub outgoing_truncated: bool,
}

pub(super) struct FcgWalk {
    pub nodes: Vec<ViewNodeInput>,
    pub edges: Vec<ViewEdgeInput>,
    pub incoming_truncated: bool,
    pub outgoing_truncated: bool,
}

pub(super) fn walk_fcg(
    seed: CandidateEndpoint,
    depth: u32,
    incoming_limit: usize,
    outgoing_limit: usize,
    mut visible_of: impl FnMut(&[ViewEdgeCandidate]) -> anyhow::Result<VisibleFileMap>,
    mut fetch: impl FnMut(&[CandidateEndpoint], &HashSet<PublicEdge>) -> anyhow::Result<FcgHopFetch>,
) -> anyhow::Result<FcgWalk> {
    let mut visited = HashSet::from([seed.id.clone()]);
    let mut emitted = HashSet::new();
    let mut frontier = vec![seed.clone()];
    let mut incoming_truncated = false;
    let mut outgoing_truncated = false;
    let mut nodes = BTreeMap::from([(seed.id.clone(), node_from_endpoint(&seed))]);
    let mut edges = Vec::new();

    for _ in 0..depth {
        if frontier.is_empty() {
            break;
        }
        let page = fetch(&frontier, &emitted)?;
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
            let public = PublicEdge::new(&edge.source.id, &edge.target.id, &edge.rel);
            if !emitted.insert(public) {
                continue;
            }
            nodes
                .entry(edge.source.id.clone())
                .or_insert_with(|| node_from_endpoint(&edge.source));
            nodes
                .entry(edge.target.id.clone())
                .or_insert_with(|| node_from_endpoint(&edge.target));
            edges.push(ViewEdgeInput {
                source: endpoint_key(&edge.source),
                target: endpoint_key(&edge.target),
                rel: edge.rel,
            });
            push_frontier(&mut next_frontier, &mut visited, &edge.source);
            push_frontier(&mut next_frontier, &mut visited, &edge.target);
        }
        frontier = next_frontier;
    }

    Ok(FcgWalk {
        nodes: nodes.into_values().collect(),
        edges,
        incoming_truncated,
        outgoing_truncated,
    })
}

fn expandable(kind: CandidateEndpointKind) -> bool {
    matches!(kind, CandidateEndpointKind::Symbol)
}

fn endpoint_key(endpoint: &CandidateEndpoint) -> NodeKey {
    match endpoint.kind {
        CandidateEndpointKind::Symbol => NodeKey::symbol(&endpoint.id),
        CandidateEndpointKind::File => NodeKey::file(&endpoint.id),
        CandidateEndpointKind::Module => NodeKey::module(&endpoint.id),
        CandidateEndpointKind::External => NodeKey::external(&endpoint.id),
        CandidateEndpointKind::Unresolved => NodeKey::unresolved(&endpoint.id),
    }
}

fn node_from_endpoint(endpoint: &CandidateEndpoint) -> ViewNodeInput {
    ViewNodeInput {
        key: endpoint_key(endpoint),
        name: endpoint.name.clone().unwrap_or_else(|| endpoint.id.clone()),
        kind: match endpoint.kind {
            CandidateEndpointKind::Symbol => "symbol".to_string(),
            CandidateEndpointKind::File => "file".to_string(),
            CandidateEndpointKind::Module => "module".to_string(),
            CandidateEndpointKind::External => "external".to_string(),
            CandidateEndpointKind::Unresolved => "unresolved".to_string(),
        },
        file: endpoint.file.clone(),
        community: None,
    }
}

fn push_frontier(
    frontier: &mut Vec<CandidateEndpoint>,
    visited: &mut HashSet<String>,
    endpoint: &CandidateEndpoint,
) {
    if expandable(endpoint.kind) && visited.insert(endpoint.id.clone()) {
        frontier.push(endpoint.clone());
    }
}

fn endpoint_kind_from_label(label: &str) -> CandidateEndpointKind {
    match label {
        "external" => CandidateEndpointKind::External,
        "unresolved" => CandidateEndpointKind::Unresolved,
        "file" => CandidateEndpointKind::File,
        "module" => CandidateEndpointKind::Module,
        _ => CandidateEndpointKind::Symbol,
    }
}

fn graph_edge_to_candidate(edge: &GraphEdge, machine_id: &str, hop: usize) -> ViewEdgeCandidate {
    ViewEdgeCandidate {
        source: CandidateEndpoint {
            kind: endpoint_kind_from_label(&edge.source_kind),
            id: edge.source.clone(),
            name: Some(edge.source_name.clone()),
            file: non_empty(&edge.source_file),
            content_hash: None,
            machine_id: None,
        },
        target: CandidateEndpoint {
            kind: endpoint_kind_from_label(&edge.target_kind),
            id: edge.target.clone(),
            name: Some(edge.target_name.clone()),
            file: non_empty(&edge.target_file),
            content_hash: None,
            machine_id: None,
        },
        rel: edge.rel.clone(),
        owner_path: edge.owner_path.clone(),
        owner_hash: edge.owner_hash.clone(),
        owner_machine: machine_id.to_string(),
        overlay_shadowed: false,
        hop,
    }
}

fn non_empty(value: &str) -> Option<String> {
    if value.is_empty() {
        None
    } else {
        Some(value.to_string())
    }
}

fn local_machine_id() -> String {
    visibility::local_machine_uuid_or_invisible()
        .map(|id| id.to_string())
        .unwrap_or_default()
}

fn fetch_fcg_hop(
    facts: &CodewikiFacts,
    frontier: &[CandidateEndpoint],
    exclude: &HashSet<PublicEdge>,
) -> anyhow::Result<FcgHopFetch> {
    let ids = frontier
        .iter()
        .map(|endpoint| endpoint.id.clone())
        .collect::<Vec<_>>();
    let frontier_ids = ids.iter().cloned().collect::<HashSet<_>>();
    let machine_id = local_machine_id();
    let scoped = facts.scoped_edges(
        &ScopeSelector::symbols(ids),
        GraphEdgeKind::Call,
        GraphBounds::symmetric(MAX_DECLARED_EDGE_LIMIT),
        GraphScopeMode::Incident,
        Some(exclude),
    )?;
    let edges = match scoped.outcome {
        GraphOutcome::Unavailable { reason } => {
            anyhow::bail!(reason);
        }
        GraphOutcome::Empty => Vec::new(),
        GraphOutcome::Available(edges) | GraphOutcome::Truncated(edges) => edges,
    };
    let mut incoming = Vec::new();
    let mut outgoing = Vec::new();
    for edge in edges {
        let candidate = graph_edge_to_candidate(&edge, &machine_id, 1);
        if frontier_ids.contains(&edge.target) {
            incoming.push(candidate.clone());
        }
        if frontier_ids.contains(&edge.source) {
            outgoing.push(candidate);
        }
    }
    Ok(FcgHopFetch {
        incoming,
        outgoing,
        incoming_truncated: scoped.incoming_truncated,
        outgoing_truncated: scoped.outgoing_truncated,
    })
}

fn visible_map_for_candidates(
    ctx: &Context,
    candidates: impl IntoIterator<Item = ViewEdgeCandidate>,
) -> anyhow::Result<VisibleFileMap> {
    let candidates = candidates.into_iter().collect::<Vec<_>>();
    let mut paths = HashSet::new();
    for edge in &candidates {
        if !edge.owner_path.is_empty() {
            paths.insert(edge.owner_path.clone());
        }
        if let Some(file) = &edge.source.file {
            paths.insert(file.clone());
        }
        if let Some(file) = &edge.target.file {
            paths.insert(file.clone());
        }
    }
    let path_list = paths.into_iter().collect::<Vec<_>>();
    let mut conn = crate::db::connect_readonly(&ctx.database_url)?;
    let visible_paths = visibility::visible_graph_paths(&mut conn, ctx, &path_list)?;
    let machine = local_machine_id();
    let mut owners = HashSet::new();
    for edge in candidates {
        if visible_paths.contains(&edge.owner_path) {
            owners.insert(VisibleOwnerKey {
                path: edge.owner_path,
                content_hash: edge.owner_hash,
                machine_id: machine.clone(),
            });
        }
    }
    Ok(VisibleFileMap {
        owners,
        overlay_shadowed_paths: HashSet::new(),
    })
}

fn user_limit(limit: Option<usize>) -> usize {
    limit.unwrap_or(MAX_DECLARED_EDGE_LIMIT)
}

pub(super) fn run(
    ctx: &Context,
    args: &GraphViewArgs,
    symbol: &ResolvedGraphSymbol,
    format: Format,
) -> anyhow::Result<()> {
    let facts = CodewikiFacts::from_context(ctx.clone());
    let hint = hint_for_availability(ctx, &facts.graph_availability());
    let seed = ViewSeed {
        id: symbol.id.clone(),
        name: symbol.display_name.clone(),
        kind: "symbol".to_string(),
        file: None,
    };
    if !matches!(facts.graph_availability(), GraphAvailability::Available) {
        return print_view(&super::empty_view_payload(ctx, args, seed, hint)?, format);
    }

    let seed_endpoint = CandidateEndpoint {
        kind: CandidateEndpointKind::Symbol,
        id: symbol.id.clone(),
        name: Some(symbol.display_name.clone()),
        file: None,
        content_hash: None,
        machine_id: None,
    };
    let incoming_limit = user_limit(args.incoming_limit);
    let outgoing_limit = user_limit(args.outgoing_limit);
    let walk = walk_fcg(
        seed_endpoint,
        args.effective_depth(),
        incoming_limit,
        outgoing_limit,
        |edges| visible_map_for_candidates(ctx, edges.iter().cloned()),
        |frontier, exclude| fetch_fcg_hop(&facts, frontier, exclude),
    )?;
    let payload = build_view_payload(
        ctx.project_id.clone(),
        ctx.project_root.display().to_string(),
        args.view,
        seed,
        args.effective_depth(),
        walk.incoming_truncated,
        walk.outgoing_truncated,
        hint,
        walk.nodes,
        walk.edges,
        Vec::new(),
    )
    .context("build fcg view payload")?;
    print_view(&payload, format)
}

#[cfg(test)]
#[path = "fcg/tests.rs"]
mod tests;
