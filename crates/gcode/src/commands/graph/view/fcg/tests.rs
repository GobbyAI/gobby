use std::collections::HashSet;

use crate::cli::GraphViewKind;
use crate::codewiki_facts::PublicEdge;
use crate::commands::graph::view::render::build_view_payload;
use crate::commands::graph::view::{
    CandidateEndpoint, CandidateEndpointKind, ViewEdgeCandidate, VisibleFileMap, VisibleOwnerKey,
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
