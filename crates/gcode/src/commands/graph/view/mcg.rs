//! Module-coupling graph walker for `gcode graph view --view=mcg`.

mod fetch;
mod identity;

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
        let key = endpoint_key(&endpoint);
        nodes
            .entry(key.clone())
            .or_insert_with(|| node_from_endpoint(&endpoint));
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
                endpoint_key(&edge.source).canonical(),
                endpoint_key(&edge.target).canonical(),
                &edge.rel,
            );
            if !typed_emitted.insert(typed) {
                continue;
            }
            untyped_emitted.insert(PublicEdge::new(&edge.source.id, &edge.target.id, &edge.rel));
            nodes
                .entry(endpoint_key(&edge.source))
                .or_insert_with(|| node_from_endpoint(&edge.source));
            nodes
                .entry(endpoint_key(&edge.target))
                .or_insert_with(|| node_from_endpoint(&edge.target));
            edges.push(ViewEdgeInput {
                source: endpoint_key(&edge.source),
                target: endpoint_key(&edge.target),
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

fn endpoint_key(endpoint: &CandidateEndpoint) -> NodeKey {
    match endpoint.kind {
        CandidateEndpointKind::File => NodeKey::file(&endpoint.id),
        CandidateEndpointKind::Module => NodeKey::module(&endpoint.id),
        CandidateEndpointKind::Symbol => NodeKey::symbol(&endpoint.id),
        CandidateEndpointKind::External => NodeKey::external(&endpoint.id),
        CandidateEndpointKind::Unresolved => NodeKey::unresolved(&endpoint.id),
    }
}

fn node_from_endpoint(endpoint: &CandidateEndpoint) -> ViewNodeInput {
    ViewNodeInput {
        key: endpoint_key(endpoint),
        name: endpoint.name.clone().unwrap_or_else(|| endpoint.id.clone()),
        kind: match endpoint.kind {
            CandidateEndpointKind::File => "file".to_string(),
            CandidateEndpointKind::Module => "module".to_string(),
            CandidateEndpointKind::Symbol => "symbol".to_string(),
            CandidateEndpointKind::External => "external".to_string(),
            CandidateEndpointKind::Unresolved => "unresolved".to_string(),
        },
        file: endpoint.file.clone(),
        community: None,
    }
}

fn consider_frontier(
    frontier: &mut Vec<CandidateEndpoint>,
    visited: &mut HashSet<NodeKey>,
    nodes: &mut BTreeMap<NodeKey, ViewNodeInput>,
    endpoint: &CandidateEndpoint,
) {
    let key = endpoint_key(endpoint);
    nodes
        .entry(key.clone())
        .or_insert_with(|| node_from_endpoint(endpoint));
    if expandable(endpoint.kind) && visited.insert(key) {
        frontier.push(endpoint.clone());
    }
}

#[cfg(test)]
mod tests {
    use std::collections::{HashMap, HashSet};

    use crate::cli::GraphViewKind;
    use crate::codewiki_facts::PublicEdge;
    use crate::commands::graph::view::render::{
        NodeKey, ViewEdgeInput, ViewNodeInput, build_view_payload,
    };
    use crate::commands::graph::view::{
        CandidateEndpoint, CandidateEndpointKind, ViewEdgeCandidate, VisibleFileMap,
        VisibleOwnerKey,
    };

    use super::identity::{McgIdentity, McgSeedError, close_endpoint, resolve_mcg_seed};
    use super::{McgHopFetch, assign_leiden_communities, walk_mcg};

    const MACHINE: &str = "machine-1";
    const HASH: &str = "hash-a";

    fn owner(path: &str) -> VisibleOwnerKey {
        VisibleOwnerKey {
            path: path.to_string(),
            content_hash: HASH.to_string(),
            machine_id: MACHINE.to_string(),
        }
    }

    fn visible(paths: &[&str]) -> VisibleFileMap {
        VisibleFileMap {
            owners: paths.iter().copied().map(owner).collect(),
            overlay_shadowed_paths: HashSet::new(),
        }
    }

    fn file(id: &str) -> CandidateEndpoint {
        CandidateEndpoint {
            kind: CandidateEndpointKind::File,
            id: id.to_string(),
            name: Some(id.to_string()),
            file: Some(id.to_string()),
            content_hash: Some(HASH.to_string()),
            machine_id: Some(MACHINE.to_string()),
        }
    }

    fn module(id: &str, provider: Option<&str>) -> CandidateEndpoint {
        CandidateEndpoint {
            kind: CandidateEndpointKind::Module,
            id: id.to_string(),
            name: Some(id.to_string()),
            file: provider.map(str::to_string),
            content_hash: provider.map(|_| HASH.to_string()),
            machine_id: Some(MACHINE.to_string()),
        }
    }

    fn candidate(source: CandidateEndpoint, target: CandidateEndpoint) -> ViewEdgeCandidate {
        ViewEdgeCandidate {
            owner_path: source.id.clone(),
            owner_hash: HASH.to_string(),
            owner_machine: MACHINE.to_string(),
            source,
            target,
            rel: "IMPORTS".to_string(),
            overlay_shadowed: false,
            hop: 1,
        }
    }

    fn identity() -> McgIdentity {
        McgIdentity {
            visible_files: HashSet::from([
                "collision".into(),
                "src/a.py".into(),
                "src/b.py".into(),
                "src/consumer.py".into(),
                "src/other.py".into(),
                "src/p.py".into(),
                "src/provider.py".into(),
                "src/q.py".into(),
                "src/r.py".into(),
            ]),
            providers: HashMap::from([
                (
                    "ambiguous".into(),
                    vec!["src/a.py".into(), "src/b.py".into()],
                ),
                ("collision".into(), vec!["src/other.py".into()]),
                ("p".into(), vec!["src/p.py".into()]),
                (".p".into(), vec!["src/p.py".into()]),
                ("provider".into(), vec!["src/provider.py".into()]),
                ("q".into(), vec!["src/q.py".into()]),
                (".q".into(), vec!["src/q.py".into()]),
                ("r".into(), vec!["src/r.py".into()]),
                (".r".into(), vec!["src/r.py".into()]),
            ]),
            aliases: HashMap::from([
                ("src/p.py".into(), vec!["p".into(), ".p".into()]),
                ("src/provider.py".into(), vec!["provider".into()]),
                ("src/q.py".into(), vec!["q".into(), ".q".into()]),
                ("src/r.py".into(), vec!["r".into(), ".r".into()]),
                ("collision".into(), Vec::new()),
            ]),
        }
    }

    fn scoped_fetch(
        catalog: &[ViewEdgeCandidate],
        files: &[CandidateEndpoint],
        modules: &[CandidateEndpoint],
        exclude: &HashSet<PublicEdge>,
        incoming_limit: usize,
        outgoing_limit: usize,
    ) -> McgHopFetch {
        let file_ids = files
            .iter()
            .map(|endpoint| endpoint.id.as_str())
            .collect::<HashSet<_>>();
        let module_ids = modules
            .iter()
            .map(|endpoint| endpoint.id.as_str())
            .collect::<HashSet<_>>();
        let mut incoming = catalog
            .iter()
            .filter(|edge| {
                edge.target.kind == CandidateEndpointKind::Module
                    && module_ids.contains(edge.target.id.as_str())
                    && !file_ids.contains(edge.source.id.as_str())
            })
            .cloned()
            .collect::<Vec<_>>();
        let mut outgoing = catalog
            .iter()
            .filter(|edge| {
                edge.source.kind == CandidateEndpointKind::File
                    && file_ids.contains(edge.source.id.as_str())
            })
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
        McgHopFetch {
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
    ) -> super::McgWalk {
        let maps = identity();
        let resolved = resolve_mcg_seed(seed, &maps).expect("mcg seed");
        walk_mcg(
            resolved.files.into_iter().chain(resolved.modules).collect(),
            depth,
            incoming_limit,
            outgoing_limit,
            |_| Ok(visible.clone()),
            |files, modules, exclude| {
                Ok(scoped_fetch(
                    &catalog,
                    files,
                    modules,
                    exclude,
                    incoming_limit.max(64),
                    outgoing_limit.max(64),
                ))
            },
            |endpoint| Ok(close_endpoint(endpoint, &maps)),
        )
        .expect("mcg walk")
    }

    fn node_ids(walk: &super::McgWalk) -> Vec<String> {
        let mut ids = walk
            .nodes
            .iter()
            .map(|node| node.key.canonical())
            .collect::<Vec<_>>();
        ids.sort();
        ids
    }

    fn edge_pairs(walk: &super::McgWalk) -> Vec<(String, String)> {
        let mut pairs = walk
            .edges
            .iter()
            .map(|edge| (edge.source.canonical(), edge.target.canonical()))
            .collect::<Vec<_>>();
        pairs.sort();
        pairs
    }

    fn consumer_provider_dep() -> Vec<ViewEdgeCandidate> {
        vec![
            candidate(
                file("src/consumer.py"),
                module("provider", Some("src/provider.py")),
            ),
            candidate(file("src/provider.py"), module("dep", None)),
        ]
    }

    #[test]
    fn mcg_assigns_leiden_communities_on_scoped_imports() {
        let nodes = vec![
            ViewNodeInput {
                key: NodeKey::file("src/a.py"),
                name: "src/a.py".into(),
                kind: "file".into(),
                file: Some("src/a.py".into()),
                community: None,
            },
            ViewNodeInput {
                key: NodeKey::module("cluster-a"),
                name: "cluster-a".into(),
                kind: "module".into(),
                file: None,
                community: None,
            },
            ViewNodeInput {
                key: NodeKey::file("src/b.py"),
                name: "src/b.py".into(),
                kind: "file".into(),
                file: Some("src/b.py".into()),
                community: None,
            },
            ViewNodeInput {
                key: NodeKey::module("cluster-b"),
                name: "cluster-b".into(),
                kind: "module".into(),
                file: None,
                community: None,
            },
        ];
        let edges = vec![
            ViewEdgeInput {
                source: NodeKey::file("src/a.py"),
                target: NodeKey::module("cluster-a"),
                rel: "IMPORTS".into(),
            },
            ViewEdgeInput {
                source: NodeKey::file("src/b.py"),
                target: NodeKey::module("cluster-b"),
                rel: "IMPORTS".into(),
            },
        ];
        let (nodes, communities) = assign_leiden_communities(nodes, &edges);
        let ids = nodes
            .iter()
            .filter_map(|node| node.community.clone())
            .collect::<HashSet<_>>();
        assert_eq!(communities.len(), 2);
        assert_eq!(ids.len(), 2);
        let payload = build_view_payload(
            "proj",
            "/abs",
            GraphViewKind::Mcg,
            crate::commands::graph::view::render::ViewSeed {
                id: "src/a.py".into(),
                name: "src/a.py".into(),
                kind: "file".into(),
                file: Some("src/a.py".into()),
            },
            1,
            false,
            false,
            None,
            nodes,
            edges,
            communities,
        )
        .expect("payload");
        assert!(payload.nodes.iter().any(|node| node.community.is_some()));
        let community = payload
            .nodes
            .iter()
            .find_map(|node| node.community.as_deref())
            .expect("community id");
        assert!(payload.mermaid.contains(community));
    }

    #[test]
    fn mcg_file_path_and_module_name_resolve_same_scope() {
        let catalog = consumer_provider_dep();
        let visible = visible(&["src/consumer.py", "src/provider.py"]);
        let from_file = walk("src/provider.py", 1, 8, 8, visible.clone(), catalog.clone());
        let from_module = walk("provider", 1, 8, 8, visible, catalog);
        assert_eq!(node_ids(&from_file), node_ids(&from_module));
        assert_eq!(edge_pairs(&from_file), edge_pairs(&from_module));
        assert!(
            edge_pairs(&from_file)
                .contains(&("file:src/consumer.py".into(), "module:provider".into()))
        );
        assert!(
            edge_pairs(&from_file).contains(&("file:src/provider.py".into(), "module:dep".into()))
        );
    }

    #[test]
    fn mcg_module_seed_rejects_missing_and_ambiguous() {
        let maps = identity();
        match resolve_mcg_seed("missing", &maps) {
            Err(McgSeedError::Missing { input }) => assert_eq!(input, "missing"),
            other => panic!("expected missing seed, got {other:?}"),
        }
        match resolve_mcg_seed("ambiguous", &maps) {
            Err(McgSeedError::Ambiguous { input, providers }) => {
                assert_eq!(input, "ambiguous");
                assert_eq!(providers.len(), 2);
            }
            other => panic!("expected ambiguous seed, got {other:?}"),
        }
    }

    #[test]
    fn mcg_file_seed_does_not_admit_same_named_module_imports() {
        let catalog = vec![
            candidate(
                file("src/consumer.py"),
                module("collision", Some("src/other.py")),
            ),
            candidate(file("collision"), module("local-dep", None)),
        ];
        let walked = walk(
            "collision",
            1,
            8,
            8,
            visible(&["collision", "src/consumer.py", "src/other.py"]),
            catalog,
        );
        let ids = node_ids(&walked);
        assert!(ids.contains(&"file:collision".into()));
        assert!(ids.contains(&"module:local-dep".into()));
        assert!(!ids.contains(&"module:collision".into()));
        assert!(!ids.contains(&"file:src/consumer.py".into()));
        assert!(walked.edges.iter().all(|edge| edge.rel == "IMPORTS"));
    }

    #[test]
    fn mcg_applies_visible_owner_set_before_limits() {
        let mut catalog = Vec::new();
        for index in 0..5 {
            let mut edge = candidate(
                file(&format!("inv-{index}.py")),
                module("p", Some("src/p.py")),
            );
            edge.owner_hash = "hash-old".to_string();
            catalog.push(edge);
        }
        catalog.push(candidate(
            file("src/consumer.py"),
            module("p", Some("src/p.py")),
        ));
        catalog.push(candidate(
            file("src/other.py"),
            module("p", Some("src/p.py")),
        ));
        let walked = walk(
            "src/p.py",
            1,
            1,
            1,
            visible(&["src/p.py", "src/consumer.py", "src/other.py"]),
            catalog,
        );
        assert_eq!(
            edge_pairs(&walked),
            vec![("file:src/consumer.py".into(), "module:p".into())]
        );
        assert!(walked.incoming_truncated);
    }

    #[test]
    fn mcg_module_seed_uses_provider_not_unique_importer() {
        let catalog = consumer_provider_dep();
        let walked = walk(
            "provider",
            1,
            8,
            8,
            visible(&["src/consumer.py", "src/provider.py"]),
            catalog,
        );
        let ids = node_ids(&walked);
        assert!(ids.contains(&"file:src/provider.py".into()));
        assert!(
            !ids.iter()
                .all(|id| id.contains("consumer") || id.contains("provider") || id.contains("dep"))
                || ids.contains(&"file:src/provider.py".into())
        );
        assert!(ids.contains(&"file:src/consumer.py".into()));
        let seed = resolve_mcg_seed("provider", &identity()).expect("seed");
        assert_eq!(
            seed.files
                .iter()
                .map(|file| file.id.as_str())
                .collect::<Vec<_>>(),
            vec!["src/provider.py"]
        );
        assert!(!seed.files.iter().any(|file| file.id == "src/consumer.py"));
    }

    #[test]
    fn mcg_cycle_emits_unique_nodes_and_edges() {
        let catalog = vec![
            candidate(file("src/p.py"), module("q", Some("src/q.py"))),
            candidate(file("src/q.py"), module("r", Some("src/r.py"))),
            candidate(file("src/r.py"), module("p", Some("src/p.py"))),
        ];
        let visible = visible(&["src/p.py", "src/q.py", "src/r.py"]);
        let first = walk("src/p.py", 4, 8, 8, visible.clone(), catalog.clone());
        let second = walk("src/p.py", 4, 8, 8, visible, catalog);
        assert_eq!(node_ids(&first), node_ids(&second));
        assert_eq!(edge_pairs(&first), edge_pairs(&second));
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
    fn mcg_prior_edge_does_not_consume_next_hop_quota() {
        let catalog = vec![
            candidate(file("src/p.py"), module("q", Some("src/q.py"))),
            candidate(file("zzz-in.py"), module("q", Some("src/q.py"))),
        ];
        let walked = walk(
            "src/p.py",
            2,
            1,
            8,
            visible(&["src/p.py", "src/q.py", "zzz-in.py"]),
            catalog,
        );
        let pairs = edge_pairs(&walked);
        assert!(pairs.contains(&("file:src/p.py".into(), "module:q".into())));
        assert!(pairs.contains(&("file:zzz-in.py".into(), "module:q".into())));
        assert!(!walked.incoming_truncated);
    }

    #[test]
    fn mcg_prior_outgoing_edge_does_not_consume_next_hop_quota() {
        let catalog = vec![
            candidate(file("src/q.py"), module("p", Some("src/p.py"))),
            candidate(file("src/q.py"), module("zzz-out", None)),
        ];
        let walked = walk(
            "src/p.py",
            2,
            8,
            1,
            visible(&["src/p.py", "src/q.py"]),
            catalog,
        );
        let pairs = edge_pairs(&walked);
        assert!(pairs.contains(&("file:src/q.py".into(), "module:p".into())));
        assert!(pairs.contains(&("file:src/q.py".into(), "module:zzz-out".into())));
        assert!(!walked.outgoing_truncated);
    }

    #[test]
    fn mcg_or_aggregates_asymmetric_truncation() {
        let catalog = vec![
            candidate(file("aaa-in.py"), module("p", Some("src/p.py"))),
            candidate(file("bbb-in.py"), module("p", Some("src/p.py"))),
            candidate(file("src/p.py"), module("aaa-out", None)),
            candidate(file("aaa-in.py"), module("ccc-out", None)),
            candidate(file("aaa-in.py"), module("ddd-out", None)),
        ];
        let walked = walk(
            "src/p.py",
            2,
            1,
            1,
            visible(&["src/p.py", "aaa-in.py", "bbb-in.py"]),
            catalog,
        );
        assert!(walked.incoming_truncated);
        assert!(walked.outgoing_truncated);
    }

    #[test]
    fn mcg_module_and_provider_seeds_include_consumers_and_deps() {
        let catalog = consumer_provider_dep();
        let visible = visible(&["src/consumer.py", "src/provider.py"]);
        let from_file = walk("src/provider.py", 1, 8, 8, visible.clone(), catalog.clone());
        let from_module = walk("provider", 1, 8, 8, visible, catalog);
        for walked in [&from_file, &from_module] {
            assert!(
                edge_pairs(walked)
                    .contains(&("file:src/consumer.py".into(), "module:provider".into()))
            );
            assert!(
                edge_pairs(walked).contains(&("file:src/provider.py".into(), "module:dep".into()))
            );
        }
    }

    #[test]
    fn mcg_two_aliases_and_provider_file_share_equivalence_class() {
        let catalog = vec![
            candidate(file("src/consumer.py"), module("p", Some("src/p.py"))),
            candidate(file("src/importer.py"), module(".p", Some("src/p.py"))),
            candidate(file("src/p.py"), module("dep", None)),
        ];
        let visible = visible(&["src/consumer.py", "src/importer.py", "src/p.py"]);
        let from_file = walk("src/p.py", 1, 8, 8, visible.clone(), catalog.clone());
        let from_path = walk("p", 1, 8, 8, visible.clone(), catalog.clone());
        let from_relative = walk(".p", 1, 8, 8, visible, catalog);
        assert_eq!(node_ids(&from_file), node_ids(&from_path));
        assert_eq!(node_ids(&from_file), node_ids(&from_relative));
        assert_eq!(edge_pairs(&from_file), edge_pairs(&from_path));
        assert_eq!(edge_pairs(&from_file), edge_pairs(&from_relative));
        assert!(
            from_file.incoming_truncated == from_path.incoming_truncated
                && from_file.outgoing_truncated == from_relative.outgoing_truncated
        );
        let seed = resolve_mcg_seed("src/p.py", &identity()).expect("file seed");
        let mut modules = seed
            .modules
            .iter()
            .map(|module| module.id.as_str())
            .collect::<Vec<_>>();
        modules.sort();
        assert_eq!(modules, vec![".p", "p"]);
    }

    #[test]
    fn mcg_depth_two_closes_discovered_frontier_equivalence() {
        let catalog = vec![
            candidate(file("src/p.py"), module(".q", Some("src/q.py"))),
            candidate(file("src/q.py"), module(".r", Some("src/r.py"))),
        ];
        let visible = visible(&["src/p.py", "src/q.py", "src/r.py"]);
        let from_file = walk("src/p.py", 2, 8, 8, visible.clone(), catalog.clone());
        let from_alias = walk("p", 2, 8, 8, visible.clone(), catalog.clone());
        let from_relative = walk(".p", 2, 8, 8, visible, catalog);
        for walked in [&from_file, &from_alias, &from_relative] {
            let pairs = edge_pairs(walked);
            assert!(pairs.contains(&("file:src/p.py".into(), "module:.q".into())));
            assert!(pairs.contains(&("file:src/q.py".into(), "module:.r".into())));
            assert_eq!(walked.incoming_truncated, from_file.incoming_truncated);
            assert_eq!(walked.outgoing_truncated, from_file.outgoing_truncated);
        }
        assert_eq!(node_ids(&from_file), node_ids(&from_alias));
        assert_eq!(node_ids(&from_file), node_ids(&from_relative));
    }
}
