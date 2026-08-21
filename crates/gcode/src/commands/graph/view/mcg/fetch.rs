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
    CandidateEndpoint, CandidateEndpointKind, ViewEdgeCandidate, hint_for_availability,
    local_machine_id, non_empty, visible_map_for_candidates,
};
use super::identity::{McgIdentity, close_endpoint, resolve_mcg_seed};
use super::{McgHopFetch, assign_leiden_communities, walk_mcg};

/// Import edge → walk candidate. The module endpoint's `file` is the unique
/// provider from the identity map (the raw `target_file` column is the module
/// name for `IMPORTS` rows, never a path).
fn import_edge_to_candidate(
    edge: &GraphEdge,
    machine_id: &str,
    identity: &McgIdentity,
) -> ViewEdgeCandidate {
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
            file: identity.unique_provider(&edge.target),
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

fn fetch_mcg_hop(
    facts: &CodewikiFacts,
    identity: &McgIdentity,
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
        let candidate = import_edge_to_candidate(&edge, &machine_id, identity);
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
        |edges| visible_map_for_candidates(ctx, edges),
        |files, modules, exclude| fetch_mcg_hop(&facts, &identity, files, modules, exclude),
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

#[cfg(test)]
mod tests {
    use std::collections::{HashMap, HashSet};

    use crate::codewiki_facts::{GraphEdge, GraphEdgeKind};

    use super::super::identity::McgIdentity;
    use super::import_edge_to_candidate;

    fn import_edge(target: &str) -> GraphEdge {
        GraphEdge {
            source: "src/consumer.py".into(),
            target: target.into(),
            kind: GraphEdgeKind::Import,
            rel: "IMPORTS".into(),
            source_kind: "file".into(),
            target_kind: "module".into(),
            source_name: "src/consumer.py".into(),
            target_name: target.into(),
            source_file: "src/consumer.py".into(),
            target_file: target.into(),
            owner_path: "src/consumer.py".into(),
            owner_hash: "hash-a".into(),
        }
    }

    #[test]
    fn mcg_import_edge_candidate_uses_identity_provider_not_target_file() {
        let identity = McgIdentity {
            visible_files: HashSet::from(["src/consumer.py".into(), "src/p.py".into()]),
            providers: HashMap::from([
                ("p".into(), vec!["src/p.py".into()]),
                ("shared".into(), vec!["src/a.py".into(), "src/b.py".into()]),
            ]),
            aliases: HashMap::from([("src/p.py".into(), vec!["p".into()])]),
        };
        let unique = import_edge_to_candidate(&import_edge("p"), "machine-1", &identity);
        assert_eq!(unique.target.file.as_deref(), Some("src/p.py"));
        assert_eq!(unique.source.file.as_deref(), Some("src/consumer.py"));
        assert_eq!(unique.owner_machine, "machine-1");
        let ambiguous = import_edge_to_candidate(&import_edge("shared"), "machine-1", &identity);
        assert_eq!(ambiguous.target.file, None);
        let unknown = import_edge_to_candidate(&import_edge("dep"), "machine-1", &identity);
        assert_eq!(unknown.target.file, None);
    }
}
