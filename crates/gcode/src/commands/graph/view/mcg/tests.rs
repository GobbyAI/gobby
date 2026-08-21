use std::collections::{HashMap, HashSet};

use crate::cli::GraphViewKind;
use crate::codewiki_facts::PublicEdge;
use crate::commands::graph::view::render::{
    NodeKey, ViewEdgeInput, ViewNodeInput, build_view_payload,
};
use crate::commands::graph::view::{
    CandidateEndpoint, CandidateEndpointKind, ViewEdgeCandidate, VisibleFileMap, VisibleOwnerKey,
};

use crate::index::import_resolution::ImportResolutionContext;

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
        visible_paths: HashSet::new(),
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
        edge_pairs(&from_file).contains(&("file:src/consumer.py".into(), "module:provider".into()))
    );
    assert!(edge_pairs(&from_file).contains(&("file:src/provider.py".into(), "module:dep".into())));
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
            edge_pairs(walked).contains(&("file:src/consumer.py".into(), "module:provider".into()))
        );
        assert!(edge_pairs(walked).contains(&("file:src/provider.py".into(), "module:dep".into())));
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

fn identity_from(visible: &[&str], rows: &[(&str, &str)]) -> McgIdentity {
    let visible = visible
        .iter()
        .map(|path| (*path).to_string())
        .collect::<HashSet<_>>();
    let imports = rows
        .iter()
        .map(|(source, module)| ((*source).to_string(), (*module).to_string()))
        .collect::<Vec<_>>();
    McgIdentity::from_resolution(&visible, &ImportResolutionContext::default(), &imports)
}

#[test]
fn mcg_identity_resolves_relative_specifier_through_row_context() {
    let identity = identity_from(
        &[
            "src/pkg/utils.py",
            "src/pkg/a.py",
            "utils.py",
            "src/main.py",
        ],
        &[("src/pkg/a.py", ".utils"), ("src/main.py", "utils")],
    );
    assert_eq!(identity.providers[".utils"], vec!["src/pkg/utils.py"]);
    assert_eq!(identity.providers["utils"], vec!["utils.py"]);
    let pkg_utils = &identity.aliases["src/pkg/utils.py"];
    for alias in [".utils", "pkg.utils", "src.pkg.utils"] {
        assert!(pkg_utils.contains(&alias.to_string()), "{pkg_utils:?}");
    }
    assert_eq!(identity.aliases["utils.py"], vec!["utils"]);
    assert_eq!(
        identity.unique_provider(".utils").as_deref(),
        Some("src/pkg/utils.py")
    );
    let seed = resolve_mcg_seed(".utils", &identity).expect("relative seed");
    assert_eq!(seed.file.as_deref(), Some("src/pkg/utils.py"));
}

#[test]
fn mcg_identity_marks_colliding_relative_specifier_ambiguous() {
    let identity = identity_from(
        &[
            "src/pkg/utils.py",
            "src/pkg/a.py",
            "src/other/utils.py",
            "src/other/b.py",
            "utils.py",
            "src/main.py",
        ],
        &[
            ("src/pkg/a.py", ".utils"),
            ("src/other/b.py", ".utils"),
            ("src/main.py", "utils"),
        ],
    );
    assert_eq!(
        identity.providers[".utils"],
        vec!["src/other/utils.py", "src/pkg/utils.py"]
    );
    assert_eq!(identity.unique_provider(".utils"), None);
    assert!(!identity.aliases["src/pkg/utils.py"].contains(&".utils".to_string()));
    assert!(!identity.aliases["src/other/utils.py"].contains(&".utils".to_string()));
    assert!(identity.aliases["src/pkg/utils.py"].contains(&"pkg.utils".to_string()));
    assert_eq!(identity.aliases["utils.py"], vec!["utils"]);
    match resolve_mcg_seed(".utils", &identity) {
        Err(McgSeedError::Ambiguous { input, providers }) => {
            assert_eq!(input, ".utils");
            assert_eq!(providers.len(), 2);
        }
        other => panic!("expected ambiguous seed, got {other:?}"),
    }
}

#[test]
fn mcg_identity_build_handles_twenty_thousand_rows() {
    const FILES: usize = 2_000;
    let visible = (0..FILES)
        .map(|index| format!("src/m{index}.py"))
        .collect::<HashSet<_>>();
    let mut imports = Vec::with_capacity(FILES * 10);
    for index in 0..FILES {
        for offset in 1..=10 {
            imports.push((
                format!("src/m{index}.py"),
                format!("m{}", (index + offset) % FILES),
            ));
        }
    }
    let started = std::time::Instant::now();
    let identity =
        McgIdentity::from_resolution(&visible, &ImportResolutionContext::default(), &imports);
    let elapsed = started.elapsed();
    assert_eq!(identity.aliases["src/m0.py"], vec!["m0", "src.m0"]);
    assert_eq!(identity.providers["m1999"], vec!["src/m1999.py"]);
    assert_eq!(identity.aliases.len(), FILES);
    assert!(
        elapsed < std::time::Duration::from_secs(30),
        "identity build took {elapsed:?}; the one-pass build must stay linear"
    );
}

#[test]
fn mcg_discovered_module_node_carries_unique_provider_file() {
    let catalog = vec![
        candidate(file("src/consumer.py"), module("p", None)),
        candidate(file("src/consumer.py"), module("dep", None)),
    ];
    let walked = walk(
        "src/consumer.py",
        1,
        8,
        8,
        visible(&["src/consumer.py", "src/p.py"]),
        catalog,
    );
    let files = walked
        .nodes
        .iter()
        .map(|node| (node.key.canonical(), node.file.clone()))
        .collect::<HashMap<_, _>>();
    assert_eq!(files["module:p"].as_deref(), Some("src/p.py"));
    assert_eq!(files["module:dep"], None);
    assert_eq!(
        files["file:src/consumer.py"].as_deref(),
        Some("src/consumer.py")
    );
}
