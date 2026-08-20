//! Production MCG hop fetch, visibility, and command entry.

use std::collections::HashSet;

use anyhow::Context as _;

use crate::cli::GraphViewArgs;
use crate::codewiki_facts::{
    CodewikiFacts, GraphAvailability, GraphBounds, GraphEdge, GraphEdgeKind, GraphOutcome,
    GraphScopeMode, MAX_DECLARED_EDGE_LIMIT, PublicEdge, ScopeSelector,
};
use crate::config::Context;
use crate::index::import_resolution::build_import_resolution_context;
use crate::output::Format;
use crate::visibility;

use super::super::render::{ViewSeed, build_view_payload, print_view};
use super::super::{
    CandidateEndpoint, CandidateEndpointKind, ViewEdgeCandidate, VisibleFileMap, VisibleOwnerKey,
    hint_for_availability,
};
use super::identity::{McgIdentity, close_endpoint, resolve_mcg_seed};
use super::{McgHopFetch, assign_leiden_communities, walk_mcg};

fn local_machine_id() -> String {
    visibility::local_machine_uuid_or_invisible()
        .map(|id| id.to_string())
        .unwrap_or_default()
}

fn import_edge_to_candidate(edge: &GraphEdge, machine_id: &str) -> ViewEdgeCandidate {
    ViewEdgeCandidate {
        source: CandidateEndpoint {
            kind: CandidateEndpointKind::File,
            id: edge.source.clone(),
            name: Some(edge.source_name.clone()),
            file: non_empty(&edge.source_file),
            content_hash: None,
            machine_id: None,
        },
        target: CandidateEndpoint {
            kind: CandidateEndpointKind::Module,
            id: edge.target.clone(),
            name: Some(edge.target_name.clone()),
            file: non_empty(&edge.target_file).filter(|path| path != &edge.target),
            content_hash: None,
            machine_id: None,
        },
        rel: edge.rel.clone(),
        owner_path: edge.owner_path.clone(),
        owner_hash: edge.owner_hash.clone(),
        owner_machine: machine_id.to_string(),
        overlay_shadowed: false,
        hop: 1,
    }
}

fn non_empty(value: &str) -> Option<String> {
    if value.is_empty() {
        None
    } else {
        Some(value.to_string())
    }
}

fn fetch_mcg_hop(
    facts: &CodewikiFacts,
    files: &[CandidateEndpoint],
    modules: &[CandidateEndpoint],
    exclude: &HashSet<PublicEdge>,
) -> anyhow::Result<McgHopFetch> {
    let file_ids = files
        .iter()
        .map(|endpoint| endpoint.id.clone())
        .collect::<Vec<_>>();
    let module_ids = modules
        .iter()
        .map(|endpoint| endpoint.id.clone())
        .collect::<Vec<_>>();
    let file_set = file_ids.iter().cloned().collect::<HashSet<_>>();
    let module_set = module_ids.iter().cloned().collect::<HashSet<_>>();
    let machine_id = local_machine_id();
    let scoped = facts.scoped_edges(
        &ScopeSelector::endpoints(file_ids, module_ids),
        GraphEdgeKind::Import,
        GraphBounds::symmetric(MAX_DECLARED_EDGE_LIMIT),
        GraphScopeMode::Incident,
        Some(exclude),
    )?;
    let edges = match scoped.outcome {
        GraphOutcome::Unavailable { reason } => anyhow::bail!(reason),
        GraphOutcome::Empty => Vec::new(),
        GraphOutcome::Available(edges) | GraphOutcome::Truncated(edges) => edges,
    };
    let mut incoming = Vec::new();
    let mut outgoing = Vec::new();
    for edge in edges {
        let candidate = import_edge_to_candidate(&edge, &machine_id);
        if module_set.contains(&edge.target) {
            incoming.push(candidate.clone());
        }
        if file_set.contains(&edge.source) {
            outgoing.push(candidate);
        }
    }
    Ok(McgHopFetch {
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

fn load_identity(ctx: &Context) -> anyhow::Result<McgIdentity> {
    let mut conn = crate::db::connect_readonly(&ctx.database_url)?;
    let visible = visibility::visible_tree(&mut conn, ctx)?
        .into_iter()
        .map(|file| file.file_path)
        .collect::<HashSet<_>>();
    let imports = crate::db::read_active_imports(&mut conn, &ctx.project_id)?
        .into_iter()
        .map(|row| (row.file_path, row.module_name))
        .collect::<Vec<_>>();
    let candidates = visible
        .iter()
        .map(|path| ctx.project_root.join(path))
        .collect::<Vec<_>>();
    let resolver = build_import_resolution_context(&ctx.project_root, &candidates);
    Ok(McgIdentity::from_resolution(&visible, &resolver, &imports))
}

pub(crate) fn run(ctx: &Context, args: &GraphViewArgs, format: Format) -> anyhow::Result<()> {
    let facts = CodewikiFacts::from_context(ctx.clone());
    let hint = hint_for_availability(ctx, &facts.graph_availability());
    if !matches!(facts.graph_availability(), GraphAvailability::Available) {
        let seed = ViewSeed {
            id: args.seed.clone(),
            name: args.seed.clone(),
            kind: "file".to_string(),
            file: Some(args.seed.clone()),
        };
        return print_view(
            &super::super::empty_view_payload(ctx, args, seed, hint)?,
            format,
        );
    }

    let identity = load_identity(ctx)?;
    let resolved = resolve_mcg_seed(&args.seed, &identity)?;
    let seed = ViewSeed {
        id: resolved.input.clone(),
        name: resolved.input.clone(),
        kind: resolved.kind.clone(),
        file: resolved.file.clone(),
    };
    let incoming_limit = user_limit(args.incoming_limit);
    let outgoing_limit = user_limit(args.outgoing_limit);
    let walk = walk_mcg(
        resolved.files.into_iter().chain(resolved.modules).collect(),
        args.effective_depth(),
        incoming_limit,
        outgoing_limit,
        |edges| visible_map_for_candidates(ctx, edges.iter().cloned()),
        |files, modules, exclude| fetch_mcg_hop(&facts, files, modules, exclude),
        |endpoint| Ok(close_endpoint(endpoint, &identity)),
    )?;
    let (nodes, communities) = assign_leiden_communities(walk.nodes, &walk.edges);
    let payload = build_view_payload(
        ctx.project_id.clone(),
        ctx.project_root.display().to_string(),
        args.view,
        seed,
        args.effective_depth(),
        walk.incoming_truncated,
        walk.outgoing_truncated,
        hint,
        nodes,
        walk.edges,
        communities,
    )
    .context("build mcg view payload")?;
    print_view(&payload, format)
}
