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

use super::render::{ViewEdgeInput, ViewNodeInput, build_view_payload, print_view};
use super::{
    CandidateEndpoint, CandidateEndpointKind, ViewEdgeCandidate, VisibleFileMap,
    endpoint_kind_from_label, hint_for_availability, local_machine_id, non_empty, symbol_seed,
    take_visible_before_bound, visible_map_for_candidates,
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
    let mut nodes = BTreeMap::from([(seed.id.clone(), seed.node())]);
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
                .or_insert_with(|| edge.source.node());
            nodes
                .entry(edge.target.id.clone())
                .or_insert_with(|| edge.target.node());
            edges.push(ViewEdgeInput {
                source: edge.source.key(),
                target: edge.target.key(),
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

fn push_frontier(
    frontier: &mut Vec<CandidateEndpoint>,
    visited: &mut HashSet<String>,
    endpoint: &CandidateEndpoint,
) {
    if expandable(endpoint.kind) && visited.insert(endpoint.id.clone()) {
        frontier.push(endpoint.clone());
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
        // An edge joining two frontier symbols is one outgoing edge, not one of each.
        if frontier_ids.contains(&edge.source) {
            outgoing.push(candidate);
        } else if frontier_ids.contains(&edge.target) {
            incoming.push(candidate);
        }
    }
    Ok(FcgHopFetch {
        incoming,
        outgoing,
        incoming_truncated: scoped.incoming_truncated,
        outgoing_truncated: scoped.outgoing_truncated,
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
    let (seed, seed_endpoint) = symbol_seed(symbol);
    if !matches!(facts.graph_availability(), GraphAvailability::Available) {
        return print_view(&super::empty_view_payload(ctx, args, seed, hint)?, format);
    }

    let incoming_limit = user_limit(args.incoming_limit);
    let outgoing_limit = user_limit(args.outgoing_limit);
    let walk = walk_fcg(
        seed_endpoint,
        args.effective_depth(),
        incoming_limit,
        outgoing_limit,
        |edges| visible_map_for_candidates(ctx, edges),
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
mod tests {
    use std::collections::HashSet;

    use crate::cli::GraphViewKind;
    use crate::codewiki_facts::PublicEdge;
    use crate::commands::graph::view::render::build_view_payload;
    use crate::commands::graph::view::{
        CandidateEndpoint, CandidateEndpointKind, ViewEdgeCandidate, VisibleFileMap,
        VisibleOwnerKey, visible_map_from,
    };

    use super::{FcgHopFetch, walk_fcg};

    const MACHINE: &str = "machine-1";
    const HASH: &str = "hash-a";
    const PATH: &str = "src/a.py";

    fn owner() -> VisibleOwnerKey {
        VisibleOwnerKey {
            path: PATH.to_string(),
            content_hash: HASH.to_string(),
            machine_id: MACHINE.to_string(),
        }
    }

    fn visible_all() -> VisibleFileMap {
        VisibleFileMap {
            owners: HashSet::from([owner()]),
            visible_paths: HashSet::new(),
            overlay_shadowed_paths: HashSet::new(),
        }
    }

    fn endpoint(kind: CandidateEndpointKind, id: &str) -> CandidateEndpoint {
        CandidateEndpoint {
            kind,
            id: id.to_string(),
            name: Some(id.to_string()),
            file: Some(PATH.to_string()),
            content_hash: Some(HASH.to_string()),
            machine_id: Some(MACHINE.to_string()),
        }
    }

    fn symbol(id: &str) -> CandidateEndpoint {
        endpoint(CandidateEndpointKind::Symbol, id)
    }

    fn candidate(source: CandidateEndpoint, target: CandidateEndpoint) -> ViewEdgeCandidate {
        ViewEdgeCandidate {
            source,
            target,
            rel: "CALLS".to_string(),
            owner_path: PATH.to_string(),
            owner_hash: HASH.to_string(),
            owner_machine: MACHINE.to_string(),
            overlay_shadowed: false,
            hop: 1,
        }
    }

    fn scoped_fetch(
        catalog: &[ViewEdgeCandidate],
        frontier: &[CandidateEndpoint],
        exclude: &HashSet<PublicEdge>,
        incoming_limit: usize,
        outgoing_limit: usize,
    ) -> FcgHopFetch {
        let frontier_ids = frontier
            .iter()
            .map(|endpoint| endpoint.id.as_str())
            .collect::<HashSet<_>>();
        let mut incoming = catalog
            .iter()
            .filter(|edge| frontier_ids.contains(edge.target.id.as_str()))
            .cloned()
            .collect::<Vec<_>>();
        let mut outgoing = catalog
            .iter()
            .filter(|edge| frontier_ids.contains(edge.source.id.as_str()))
            .cloned()
            .collect::<Vec<_>>();
        incoming.sort_by(|left, right| {
            left.source
                .id
                .cmp(&right.source.id)
                .then_with(|| left.target.id.cmp(&right.target.id))
        });
        outgoing.sort_by(|left, right| {
            left.source
                .id
                .cmp(&right.source.id)
                .then_with(|| left.target.id.cmp(&right.target.id))
        });
        incoming.retain(|edge| {
            !exclude.contains(&PublicEdge::new(
                &edge.source.id,
                &edge.target.id,
                &edge.rel,
            ))
        });
        outgoing.retain(|edge| {
            !exclude.contains(&PublicEdge::new(
                &edge.source.id,
                &edge.target.id,
                &edge.rel,
            ))
        });
        let incoming_truncated = incoming.len() > incoming_limit;
        let outgoing_truncated = outgoing.len() > outgoing_limit;
        incoming.truncate(incoming_limit);
        outgoing.truncate(outgoing_limit);
        FcgHopFetch {
            incoming,
            outgoing,
            incoming_truncated,
            outgoing_truncated,
        }
    }

    fn walk(
        seed: &str,
        depth: u32,
        incoming_limit: usize,
        outgoing_limit: usize,
        visible: VisibleFileMap,
        catalog: Vec<ViewEdgeCandidate>,
    ) -> super::FcgWalk {
        walk_fcg(
            symbol(seed),
            depth,
            incoming_limit,
            outgoing_limit,
            |_| Ok(visible.clone()),
            |frontier, exclude| {
                Ok(scoped_fetch(
                    &catalog,
                    frontier,
                    exclude,
                    incoming_limit.max(64),
                    outgoing_limit.max(64),
                ))
            },
        )
        .expect("fcg walk")
    }

    fn node_ids(walk: &super::FcgWalk) -> Vec<String> {
        let mut ids = walk
            .nodes
            .iter()
            .map(|node| node.key.canonical())
            .collect::<Vec<_>>();
        ids.sort();
        ids
    }

    fn edge_pairs(walk: &super::FcgWalk) -> Vec<(String, String)> {
        let mut pairs = walk
            .edges
            .iter()
            .map(|edge| (edge.source.canonical(), edge.target.canonical()))
            .collect::<Vec<_>>();
        pairs.sort();
        pairs
    }

    #[test]
    fn fcg_or_aggregates_asymmetric_truncation() {
        let catalog = vec![
            candidate(symbol("aaa-caller"), symbol("seed")),
            candidate(symbol("bbb-caller"), symbol("seed")),
            candidate(symbol("seed"), symbol("aaa-callee")),
            candidate(symbol("aaa-caller"), symbol("ccc-caller")),
            candidate(symbol("aaa-callee"), symbol("ccc-callee")),
            candidate(symbol("aaa-callee"), symbol("ddd-callee")),
        ];
        let walked = walk("seed", 2, 1, 1, visible_all(), catalog);
        assert!(walked.incoming_truncated);
        assert!(walked.outgoing_truncated);
        assert_eq!(
            edge_pairs(&walked),
            vec![
                ("symbol:aaa-callee".into(), "symbol:ccc-callee".into()),
                ("symbol:aaa-caller".into(), "symbol:seed".into()),
                ("symbol:seed".into(), "symbol:aaa-callee".into()),
            ]
        );
    }

    #[test]
    fn fcg_includes_external_and_unresolved_targets() {
        let catalog = vec![
            candidate(
                symbol("seed"),
                endpoint(CandidateEndpointKind::External, "ext-1"),
            ),
            candidate(
                symbol("seed"),
                endpoint(CandidateEndpointKind::Unresolved, "miss-1"),
            ),
            candidate(symbol("seed"), symbol("local-1")),
        ];
        let walked = walk("seed", 1, 8, 8, visible_all(), catalog);
        let ids = node_ids(&walked);
        assert!(ids.contains(&"symbol:seed".into()));
        assert!(ids.contains(&"external:ext-1".into()));
        assert!(ids.contains(&"unresolved:miss-1".into()));
        assert!(ids.contains(&"symbol:local-1".into()));
        assert!(walked.edges.iter().all(|edge| edge.rel == "CALLS"));
        let payload = build_view_payload(
            "proj",
            "/abs",
            GraphViewKind::Fcg,
            crate::commands::graph::view::render::ViewSeed {
                id: "seed".into(),
                name: "seed".into(),
                kind: "symbol".into(),
                file: None,
            },
            1,
            false,
            false,
            None,
            walked.nodes,
            walked.edges,
            Vec::new(),
        )
        .expect("payload");
        assert!(payload.mermaid.contains("CALLS"));
    }

    #[test]
    fn fcg_applies_visible_owner_set_before_limits() {
        let mut catalog = Vec::new();
        for index in 0..5 {
            let mut edge = candidate(symbol(&format!("inv-{index}")), symbol("seed"));
            edge.owner_hash = "hash-old".to_string();
            catalog.push(edge);
        }
        catalog.push(candidate(symbol("keep-caller"), symbol("seed")));
        catalog.push(candidate(symbol("keep-caller-2"), symbol("seed")));
        let walked = walk("seed", 1, 1, 1, visible_all(), catalog);
        assert_eq!(
            edge_pairs(&walked),
            vec![("symbol:keep-caller".into(), "symbol:seed".into())]
        );
        assert!(walked.incoming_truncated);
    }

    #[test]
    fn fcg_cycle_emits_unique_nodes_and_edges() {
        let catalog = vec![
            candidate(symbol("seed"), symbol("b")),
            candidate(symbol("b"), symbol("c")),
            candidate(symbol("c"), symbol("seed")),
        ];
        let first = walk("seed", 4, 8, 8, visible_all(), catalog.clone());
        let second = walk("seed", 4, 8, 8, visible_all(), catalog);
        assert_eq!(node_ids(&first), node_ids(&second));
        assert_eq!(edge_pairs(&first), edge_pairs(&second));
        assert_eq!(first.nodes.len(), 3);
        assert_eq!(first.edges.len(), 3);
        let mut seen = HashSet::new();
        for edge in &first.edges {
            assert!(seen.insert((
                edge.source.canonical(),
                edge.target.canonical(),
                edge.rel.clone()
            )));
        }
    }

    #[test]
    fn fcg_prior_edge_does_not_consume_next_hop_quota() {
        let catalog = vec![
            candidate(symbol("seed"), symbol("mid")),
            candidate(symbol("zzz-in"), symbol("mid")),
        ];
        let walked = walk("seed", 2, 1, 8, visible_all(), catalog);
        let pairs = edge_pairs(&walked);
        assert!(pairs.contains(&("symbol:seed".into(), "symbol:mid".into())));
        assert!(pairs.contains(&("symbol:zzz-in".into(), "symbol:mid".into())));
        assert!(!walked.incoming_truncated);
    }

    #[test]
    fn fcg_prior_outgoing_edge_does_not_consume_next_hop_quota() {
        let catalog = vec![
            candidate(symbol("mid"), symbol("seed")),
            candidate(symbol("mid"), symbol("zzz-out")),
        ];
        let walked = walk("seed", 2, 8, 1, visible_all(), catalog);
        let pairs = edge_pairs(&walked);
        assert!(pairs.contains(&("symbol:mid".into(), "symbol:seed".into())));
        assert!(pairs.contains(&("symbol:mid".into(), "symbol:zzz-out".into())));
        assert!(!walked.outgoing_truncated);
    }

    #[test]
    fn fcg_walk_records_stable_membership_across_limits() {
        let catalog = vec![
            candidate(symbol("aaa"), symbol("seed")),
            candidate(symbol("bbb"), symbol("seed")),
        ];
        let walked = walk("seed", 1, 1, 1, visible_all(), catalog);
        assert_eq!(
            edge_pairs(&walked),
            vec![("symbol:aaa".into(), "symbol:seed".into())]
        );
    }

    fn endpoint_in(id: &str, file: &str) -> CandidateEndpoint {
        CandidateEndpoint {
            kind: CandidateEndpointKind::Symbol,
            id: id.to_string(),
            name: Some(id.to_string()),
            file: Some(file.to_string()),
            content_hash: None,
            machine_id: None,
        }
    }

    #[test]
    fn fcg_keeps_cross_file_callee_and_caller_through_production_visibility() {
        let seed = endpoint_in("seed", "src/a.py");
        let mut callee_edge = candidate(seed.clone(), endpoint_in("callee", "src/b.py"));
        callee_edge.owner_path = "src/a.py".to_string();
        let mut caller_edge = candidate(endpoint_in("caller", "src/c.py"), seed.clone());
        caller_edge.owner_path = "src/c.py".to_string();
        let catalog = vec![callee_edge, caller_edge];
        let paths = ["src/a.py", "src/b.py", "src/c.py"]
            .into_iter()
            .map(String::from)
            .collect::<HashSet<_>>();
        let walked = walk_fcg(
            seed,
            1,
            8,
            8,
            |edges| Ok(visible_map_from(edges, paths.clone(), MACHINE)),
            |frontier, exclude| Ok(scoped_fetch(&catalog, frontier, exclude, 64, 64)),
        )
        .expect("fcg walk");
        assert_eq!(
            edge_pairs(&walked),
            vec![
                ("symbol:caller".into(), "symbol:seed".into()),
                ("symbol:seed".into(), "symbol:callee".into()),
            ]
        );
        assert!(!walked.incoming_truncated);
        assert!(!walked.outgoing_truncated);
        let files = walked
            .nodes
            .iter()
            .map(|node| (node.key.canonical(), node.file.clone()))
            .collect::<std::collections::BTreeMap<_, _>>();
        assert_eq!(files["symbol:callee"].as_deref(), Some("src/b.py"));
        assert_eq!(files["symbol:caller"].as_deref(), Some("src/c.py"));
        assert_eq!(files["symbol:seed"].as_deref(), Some("src/a.py"));
    }
}
