use super::*;
use crate::cli::{GraphViewArgs, GraphViewKind};
use crate::codewiki_facts::GraphAvailability;
use crate::config::Context;
use crate::graph::code_graph::GraphReadError;
use gobby_core::vault::mermaid::is_valid_mermaid;
use render::{NodeKey, ViewNodeInput, analytics_graph_from_payload, build_view_payload};
use std::path::PathBuf;

fn ctx_without_falkor() -> Context {
    Context {
        database_url: "postgresql://localhost/nonexistent".to_string(),
        project_root: PathBuf::from("/abs/project"),
        project_id: "proj-1".to_string(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: crate::config::CodeVectorSettings::default(),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: crate::config::ProjectIndexScope::Single,
    }
}

fn owner(path: &str, hash: &str, machine: &str) -> VisibleOwnerKey {
    VisibleOwnerKey {
        path: path.to_string(),
        content_hash: hash.to_string(),
        machine_id: machine.to_string(),
    }
}

fn endpoint(
    kind: CandidateEndpointKind,
    id: &str,
    file: Option<&str>,
    hash: Option<&str>,
    machine: Option<&str>,
) -> CandidateEndpoint {
    CandidateEndpoint {
        kind,
        id: id.to_string(),
        name: Some(id.to_string()),
        file: file.map(ToString::to_string),
        content_hash: hash.map(ToString::to_string),
        machine_id: machine.map(ToString::to_string),
    }
}

fn candidate(
    source: CandidateEndpoint,
    target: CandidateEndpoint,
    owner_path: &str,
    owner_hash: &str,
    owner_machine: &str,
    overlay_shadowed: bool,
    hop: usize,
) -> ViewEdgeCandidate {
    ViewEdgeCandidate {
        source,
        target,
        rel: "CALLS".to_string(),
        owner_path: owner_path.to_string(),
        owner_hash: owner_hash.to_string(),
        owner_machine: owner_machine.to_string(),
        overlay_shadowed,
        hop,
    }
}

#[test]
fn view_typed_ids_keep_file_and_module_collision_distinct() {
    let payload = build_view_payload(
        "proj-1",
        "/abs/project",
        GraphViewKind::Mcg,
        ViewSeed {
            id: "seed".into(),
            name: "seed".into(),
            kind: "module".into(),
            file: None,
        },
        1,
        false,
        false,
        None,
        vec![
            ViewNodeInput {
                key: NodeKey::file("shared"),
                name: "shared-file".into(),
                kind: "file".into(),
                file: Some("shared".into()),
                community: None,
            },
            ViewNodeInput {
                key: NodeKey::module("shared"),
                name: "shared-module".into(),
                kind: "module".into(),
                file: None,
                community: None,
            },
        ],
        vec![render::ViewEdgeInput {
            source: NodeKey::file("shared"),
            target: NodeKey::module("shared"),
            rel: "IMPORTS".into(),
        }],
        Vec::new(),
    )
    .expect("payload");
    assert_eq!(payload.nodes.len(), 2);
    let ids = payload
        .nodes
        .iter()
        .map(|node| node.id.as_str())
        .collect::<Vec<_>>();
    assert_eq!(ids, ["file:shared", "module:shared"]);
    assert!(payload.mermaid.contains("n0[\""));
    assert!(payload.mermaid.contains("n1[\""));
    let graph = analytics_graph_from_payload(&payload);
    assert_eq!(graph.nodes.len(), 2);
    let graph_ids = graph
        .nodes
        .iter()
        .map(|node| node.id.as_str())
        .collect::<HashSet<_>>();
    assert!(graph_ids.contains("file:shared"));
    assert!(graph_ids.contains("module:shared"));
    assert_eq!(graph.edges.len(), 1);
}

#[test]
fn graph_view_respects_active_visible_file_map() {
    let visible = VisibleFileMap {
        owners: HashSet::from([
            owner("src/a.py", "hash-a", "machine-1"),
            owner("src/b.py", "hash-b", "machine-1"),
        ]),
        visible_paths: HashSet::new(),
        overlay_shadowed_paths: HashSet::from(["src/parent.py".to_string()]),
    };
    let keep = candidate(
        endpoint(
            CandidateEndpointKind::File,
            "keep-file",
            Some("src/a.py"),
            Some("hash-a"),
            Some("machine-1"),
        ),
        endpoint(
            CandidateEndpointKind::Module,
            "keep-mod",
            Some("src/b.py"),
            Some("hash-b"),
            Some("machine-1"),
        ),
        "src/a.py",
        "hash-a",
        "machine-1",
        false,
        1,
    );
    let stale_hash = candidate(
        endpoint(
            CandidateEndpointKind::Symbol,
            "stale",
            Some("src/a.py"),
            Some("hash-old"),
            Some("machine-1"),
        ),
        endpoint(
            CandidateEndpointKind::Symbol,
            "other",
            Some("src/b.py"),
            Some("hash-b"),
            Some("machine-1"),
        ),
        "src/a.py",
        "hash-old",
        "machine-1",
        false,
        1,
    );
    let other_machine = candidate(
        endpoint(
            CandidateEndpointKind::Symbol,
            "foreign",
            Some("src/a.py"),
            Some("hash-a"),
            Some("machine-2"),
        ),
        endpoint(
            CandidateEndpointKind::Symbol,
            "other",
            Some("src/b.py"),
            Some("hash-b"),
            Some("machine-1"),
        ),
        "src/a.py",
        "hash-a",
        "machine-2",
        false,
        1,
    );
    let shadowed = candidate(
        endpoint(
            CandidateEndpointKind::Symbol,
            "parent",
            Some("src/parent.py"),
            Some("hash-p"),
            Some("machine-1"),
        ),
        endpoint(
            CandidateEndpointKind::Symbol,
            "child",
            Some("src/b.py"),
            Some("hash-b"),
            Some("machine-1"),
        ),
        "src/parent.py",
        "hash-p",
        "machine-1",
        true,
        1,
    );
    let terminal_on_invisible = candidate(
        endpoint(CandidateEndpointKind::External, "ext", None, None, None),
        endpoint(CandidateEndpointKind::Unresolved, "miss", None, None, None),
        "src/a.py",
        "hash-old",
        "machine-1",
        false,
        1,
    );
    let bound = take_visible_before_bound(
        vec![
            keep.clone(),
            stale_hash,
            other_machine,
            shadowed,
            terminal_on_invisible,
        ],
        &visible,
        Some(10),
        None,
    );
    assert_eq!(bound.edges.len(), 1);
    assert_eq!(bound.edges[0].source.id, "keep-file");
    assert!(!bound.truncated);
}

#[test]
fn graph_view_invisible_rows_do_not_consume_edge_or_hop_budget() {
    let visible = VisibleFileMap {
        owners: HashSet::from([owner("src/a.py", "hash-a", "machine-1")]),
        visible_paths: HashSet::new(),
        overlay_shadowed_paths: HashSet::from(["src/shadow.py".to_string()]),
    };
    let mut rows = Vec::new();
    for index in 0..5 {
        rows.push(candidate(
            endpoint(
                CandidateEndpointKind::Symbol,
                &format!("invisible-{index}"),
                Some("src/a.py"),
                Some("hash-old"),
                Some("machine-1"),
            ),
            endpoint(CandidateEndpointKind::Unresolved, "t", None, None, None),
            "src/a.py",
            "hash-old",
            "machine-1",
            false,
            1,
        ));
    }
    rows.push(candidate(
        endpoint(
            CandidateEndpointKind::Symbol,
            "shadowed",
            Some("src/shadow.py"),
            Some("hash-s"),
            Some("machine-1"),
        ),
        endpoint(CandidateEndpointKind::Unresolved, "t", None, None, None),
        "src/shadow.py",
        "hash-s",
        "machine-1",
        true,
        1,
    ));
    rows.push(candidate(
        endpoint(
            CandidateEndpointKind::Symbol,
            "keep-1",
            Some("src/a.py"),
            Some("hash-a"),
            Some("machine-1"),
        ),
        endpoint(CandidateEndpointKind::Unresolved, "t1", None, None, None),
        "src/a.py",
        "hash-a",
        "machine-1",
        false,
        1,
    ));
    rows.push(candidate(
        endpoint(
            CandidateEndpointKind::Symbol,
            "keep-2",
            Some("src/a.py"),
            Some("hash-a"),
            Some("machine-1"),
        ),
        endpoint(CandidateEndpointKind::Unresolved, "t2", None, None, None),
        "src/a.py",
        "hash-a",
        "machine-1",
        false,
        2,
    ));
    let bound = take_visible_before_bound(rows, &visible, Some(2), Some(2));
    assert_eq!(bound.edges.len(), 2);
    assert_eq!(bound.edges[0].source.id, "keep-1");
    assert_eq!(bound.edges[1].source.id, "keep-2");
    assert!(!bound.truncated);
    assert!(!bound.hop_cut);
}

#[test]
fn graph_view_unavailable_differs_from_empty() {
    let ctx = ctx_without_falkor();
    let unconfigured = hint_for_availability(
        &ctx,
        &GraphAvailability::Unavailable {
            reason: "FalkorDB is not configured".to_string(),
        },
    );
    assert!(unconfigured.is_some());
    let unreachable = hint_for_availability(
        &ctx,
        &GraphAvailability::Unavailable {
            reason: "connection refused".to_string(),
        },
    );
    assert!(unreachable.is_some());
    assert_ne!(unconfigured, unreachable);
    let available = hint_for_availability(&ctx, &GraphAvailability::Available);
    assert_eq!(available, None);

    let args = GraphViewArgs {
        view: GraphViewKind::Fcg,
        seed: "Derived".into(),
        depth: None,
        incoming_limit: None,
        outgoing_limit: None,
    };
    let seed = ViewSeed {
        id: "seed".into(),
        name: "Derived".into(),
        kind: "class".into(),
        file: Some("src/a.py".into()),
    };
    let unavailable = empty_view_payload(&ctx, &args, seed.clone(), unconfigured).expect("payload");
    assert!(unavailable.nodes.is_empty());
    assert!(unavailable.edges.is_empty());
    assert!(unavailable.hint.is_some());
    assert!(is_valid_mermaid(&unavailable.mermaid));
    let empty = empty_view_payload(&ctx, &args, seed, None).expect("payload");
    assert!(empty.nodes.is_empty());
    assert!(empty.edges.is_empty());
    assert_eq!(empty.hint, None);
    assert!(is_valid_mermaid(&empty.mermaid));
}

#[test]
fn graph_view_seed_resolution_propagates_database_errors() {
    let infra = classify_seed_lookup("Derived", Err(anyhow::anyhow!("connection refused")));
    assert!(matches!(infra, Err(SeedLookupError::Infrastructure(_))));
    let missing = classify_seed_lookup("Derived", Ok((None, Vec::new())));
    assert!(matches!(
        missing,
        Err(SeedLookupError::Missing { suggestions, .. }) if suggestions.is_empty()
    ));
    let ambiguous = classify_seed_lookup(
        "Derived",
        Ok((None, vec!["DerivedA".into(), "DerivedB".into()])),
    );
    assert!(matches!(
        ambiguous,
        Err(SeedLookupError::Missing { suggestions, .. }) if suggestions.len() == 2
    ));
    let found = classify_seed_lookup(
        "Derived",
        Ok((
            Some(ResolvedGraphSymbol {
                id: "sym-1".into(),
                display_name: "Derived".into(),
                file_path: None,
            }),
            Vec::new(),
        )),
    )
    .expect("found");
    assert_eq!(found.id, "sym-1");
    for allow_stale in [false, true] {
        let _ = allow_stale;
        let err = classify_seed_lookup("Derived", Err(anyhow::anyhow!("ssl failure")));
        assert!(
            matches!(err, Err(SeedLookupError::Infrastructure(_))),
            "allow-stale must not collapse a database error into a missing seed"
        );
        assert!(!matches!(
            classify_seed_lookup("Derived", Err(anyhow::anyhow!("ssl failure"))),
            Err(SeedLookupError::Missing { .. })
        ));
    }
    let _ = GraphReadError::NotConfigured;
}

#[test]
fn visible_map_from_records_visible_endpoint_paths() {
    let edge = candidate(
        endpoint(
            CandidateEndpointKind::Symbol,
            "caller",
            Some("src/b.py"),
            None,
            None,
        ),
        endpoint(
            CandidateEndpointKind::Symbol,
            "callee",
            Some("src/c.py"),
            None,
            None,
        ),
        "src/a.py",
        "hash-a",
        "machine-1",
        false,
        1,
    );
    let mut paths = candidate_paths(std::slice::from_ref(&edge));
    paths.sort();
    assert_eq!(paths, ["src/a.py", "src/b.py", "src/c.py"]);

    let all = paths.iter().cloned().collect::<HashSet<_>>();
    let visible = visible_map_from(std::slice::from_ref(&edge), all, "machine-1");
    assert_eq!(
        visible.owners,
        HashSet::from([owner("src/a.py", "hash-a", "machine-1")])
    );
    assert!(visible.visible_paths.contains("src/c.py"));
    let kept = take_visible_before_bound(vec![edge.clone()], &visible, None, None);
    assert_eq!(kept.edges.len(), 1);

    let without_b = HashSet::from(["src/a.py".to_string(), "src/c.py".to_string()]);
    let visible = visible_map_from(std::slice::from_ref(&edge), without_b, "machine-1");
    let kept = take_visible_before_bound(vec![edge], &visible, None, None);
    assert!(kept.edges.is_empty());
}

#[test]
fn symbol_seed_carries_resolved_file_path() {
    let symbol = ResolvedGraphSymbol {
        id: "sym-1".into(),
        display_name: "Derived".into(),
        file_path: Some("src/a.py".into()),
    };
    let (seed, endpoint) = symbol_seed(&symbol);
    assert_eq!(seed.id, "sym-1");
    assert_eq!(seed.kind, "symbol");
    assert_eq!(seed.file.as_deref(), Some("src/a.py"));
    assert_eq!(endpoint.kind, CandidateEndpointKind::Symbol);
    assert_eq!(endpoint.file.as_deref(), Some("src/a.py"));
    let node = endpoint.node();
    assert_eq!(node.key, NodeKey::symbol("sym-1"));
    assert_eq!(node.file.as_deref(), Some("src/a.py"));

    let external = ResolvedGraphSymbol {
        id: "ext-1".into(),
        display_name: "os.path.join".into(),
        file_path: None,
    };
    let (seed, endpoint) = symbol_seed(&external);
    assert_eq!(seed.file, None);
    assert_eq!(endpoint.node().file, None);
}

#[test]
fn node_file_is_null_for_external_and_unresolved_even_when_raw_file_is_id() {
    for (kind, label) in [
        (CandidateEndpointKind::External, "external"),
        (CandidateEndpointKind::Unresolved, "unresolved"),
    ] {
        let node = endpoint(kind, "ext-1", Some("ext-1"), None, None).node();
        assert_eq!(node.kind, label);
        assert_eq!(node.file, None);
        assert_eq!(node.key.canonical(), format!("{label}:ext-1"));
    }
    let module = endpoint(
        CandidateEndpointKind::Module,
        "pkg",
        Some("src/pkg.py"),
        None,
        None,
    )
    .node();
    assert_eq!(module.kind, "module");
    assert_eq!(module.file.as_deref(), Some("src/pkg.py"));
    let unprovided = endpoint(CandidateEndpointKind::Module, "pkg", None, None, None).node();
    assert_eq!(unprovided.file, None);
    let file = endpoint(
        CandidateEndpointKind::File,
        "src/a.py",
        Some("src/a.py"),
        None,
        None,
    )
    .node();
    assert_eq!(file.kind, "file");
    assert_eq!(file.file.as_deref(), Some("src/a.py"));
}
