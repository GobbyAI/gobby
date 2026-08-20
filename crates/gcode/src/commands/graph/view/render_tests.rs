use super::*;
use gobby_core::vault::mermaid::is_valid_mermaid;

fn sample_seed() -> ViewSeed {
    ViewSeed {
        id: "11111111-1111-1111-1111-111111111111".to_string(),
        name: "Derived".to_string(),
        kind: "class".to_string(),
        file: Some("src/a.py".to_string()),
    }
}

fn payload_for(
    view: GraphViewKind,
    nodes: Vec<ViewNodeInput>,
    edges: Vec<ViewEdgeInput>,
) -> ViewPayload {
    build_view_payload(
        "proj-1",
        "/abs/project",
        view,
        sample_seed(),
        8,
        false,
        false,
        None,
        nodes,
        edges,
        Vec::new(),
    )
    .expect("payload builds")
}

#[test]
fn view_render_is_deterministic_and_escapes_hostile_labels() {
    let hostile = ViewNodeInput {
        key: NodeKey::file("src/a\"b[c].py"),
        name: "a\"b[c]\nfile".to_string(),
        kind: "file".to_string(),
        file: Some("src/a\"b[c].py".to_string()),
        community: None,
    };
    let module = ViewNodeInput {
        key: NodeKey::module("src/a\"b[c].py"),
        name: "mod|name".to_string(),
        kind: "module".to_string(),
        file: None,
        community: None,
    };
    let uuid_like = ViewNodeInput {
        key: NodeKey::symbol("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        name: "名前".to_string(),
        kind: "class".to_string(),
        file: Some("src/a.py".to_string()),
        community: None,
    };
    let colliding_a = ViewNodeInput {
        key: NodeKey::file("a b"),
        name: "file a b".to_string(),
        kind: "file".to_string(),
        file: Some("a b".to_string()),
        community: None,
    };
    let colliding_b = ViewNodeInput {
        key: NodeKey::file("a_b"),
        name: "file a_b".to_string(),
        kind: "file".to_string(),
        file: Some("a_b".to_string()),
        community: None,
    };
    let edge = ViewEdgeInput {
        source: NodeKey::symbol("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        target: NodeKey::file("src/a\"b[c].py"),
        rel: "CALLS".to_string(),
    };
    let mut shuffled_nodes = vec![
        colliding_b.clone(),
        uuid_like.clone(),
        module.clone(),
        hostile.clone(),
        colliding_a.clone(),
    ];
    shuffled_nodes.reverse();
    let first = payload_for(GraphViewKind::Fcg, shuffled_nodes, vec![edge.clone()]);
    let second = payload_for(
        GraphViewKind::Fcg,
        vec![hostile, module, uuid_like, colliding_a, colliding_b],
        vec![edge],
    );
    let first_json = format_view_output(&first).expect("json");
    let second_json = format_view_output(&second).expect("json");
    assert_eq!(first_json, second_json);
    assert!(is_valid_mermaid(&first.mermaid));
    assert!(first.mermaid.starts_with("```mermaid\n"));
    assert!(first.mermaid.trim_end().ends_with("```"));
    assert!(first.mermaid.contains("n0[\""));
    assert!(first.mermaid.contains("n1[\""));
    assert!(!first.mermaid.contains("src/a\"b"));
    assert!(first.mermaid.contains("#quot;"));
    assert!(first.mermaid.contains("#91;"));
    let tokens: Vec<_> = first
        .mermaid
        .lines()
        .filter(|line| line.trim_start().starts_with('n') && line.contains("[\""))
        .collect();
    assert!(tokens.len() >= 4);
    assert_eq!(first.nodes.len(), 5);
}

#[test]
fn view_render_does_not_clip_above_historical_budget() {
    for view in [
        GraphViewKind::Fcg,
        GraphViewKind::Mcg,
        GraphViewKind::ClassHierarchy,
    ] {
        let mut nodes = Vec::new();
        let mut edges = Vec::new();
        for index in 0..80 {
            let name = format!("Node_{index}_{}", "x".repeat(40));
            nodes.push(ViewNodeInput {
                key: NodeKey::symbol(format!("00000000-0000-0000-0000-{index:012}")),
                name: name.clone(),
                kind: "class".to_string(),
                file: Some(format!("src/file_{index}.py")),
                community: None,
            });
            if index > 0 {
                edges.push(ViewEdgeInput {
                    source: NodeKey::symbol(format!("00000000-0000-0000-0000-{index:012}")),
                    target: NodeKey::symbol(format!("00000000-0000-0000-0000-{:012}", index - 1)),
                    rel: match view {
                        GraphViewKind::Fcg => "CALLS".to_string(),
                        GraphViewKind::Mcg => "IMPORTS".to_string(),
                        GraphViewKind::ClassHierarchy => "EXTENDS".to_string(),
                    },
                });
            }
        }
        let payload = payload_for(view, nodes, edges);
        let printed = format_view_output(&payload).expect("complete payload");
        assert!(
            printed.len() > 10_000,
            "{} payload should exceed historical clip threshold, got {}",
            view.as_str(),
            printed.len()
        );
        let parsed: serde_json::Value = serde_json::from_str(&printed).expect("complete JSON");
        assert_eq!(parsed["view"], view.as_str());
        assert_eq!(parsed["nodes"].as_array().expect("nodes").len(), 80);
        assert_eq!(parsed["edges"].as_array().expect("edges").len(), 79);
        let mermaid = parsed["mermaid"].as_str().expect("mermaid field");
        assert!(is_valid_mermaid(mermaid));
        assert!(mermaid.starts_with("```mermaid\n"));
        assert!(mermaid.contains("n0[\""));
        assert!(mermaid.contains("n79[\""));
        assert!(!printed.contains('\u{0}'));
    }
}

#[test]
fn view_payload_includes_project_identity() {
    let payload = payload_for(GraphViewKind::ClassHierarchy, Vec::new(), Vec::new());
    let printed = format_view_output(&payload).expect("json");
    let parsed: serde_json::Value = serde_json::from_str(&printed).expect("json");
    assert_eq!(parsed["project_id"], "proj-1");
    assert_eq!(parsed["project_root"], "/abs/project");
    assert_eq!(payload.project_id, "proj-1");
    assert_eq!(payload.project_root, "/abs/project");
}

#[test]
fn view_node_file_nullability_by_kind() {
    assert_eq!(
        node_file_for_kind(NodeKind::File, Some("src/a.py".into()), &[]),
        Some("src/a.py".into())
    );
    assert_eq!(
        node_file_for_kind(NodeKind::Symbol, Some("src/a.py".into()), &[]),
        Some("src/a.py".into())
    );
    assert_eq!(
        node_file_for_kind(NodeKind::Module, None, &["src/mod.py".into()]),
        Some("src/mod.py".into())
    );
    assert_eq!(node_file_for_kind(NodeKind::Module, None, &[]), None);
    assert_eq!(
        node_file_for_kind(
            NodeKind::Module,
            None,
            &["src/a.py".into(), "src/b.py".into()]
        ),
        None
    );
    assert_eq!(
        node_file_for_kind(NodeKind::External, Some("src/a.py".into()), &[]),
        None
    );
    assert_eq!(
        node_file_for_kind(NodeKind::Unresolved, Some("src/a.py".into()), &[]),
        None
    );

    let payload = payload_for(
        GraphViewKind::Mcg,
        vec![
            ViewNodeInput {
                key: NodeKey::file("src/a.py"),
                name: "a.py".into(),
                kind: "file".into(),
                file: node_file_for_kind(NodeKind::File, Some("src/a.py".into()), &[]),
                community: None,
            },
            ViewNodeInput {
                key: NodeKey::external("ext.Type"),
                name: "Type".into(),
                kind: "external".into(),
                file: node_file_for_kind(NodeKind::External, Some("src/a.py".into()), &[]),
                community: None,
            },
            ViewNodeInput {
                key: NodeKey::unresolved("Missing"),
                name: "Missing".into(),
                kind: "unresolved".into(),
                file: node_file_for_kind(NodeKind::Unresolved, Some("src/a.py".into()), &[]),
                community: None,
            },
            ViewNodeInput {
                key: NodeKey::module("pkg.mod"),
                name: "pkg.mod".into(),
                kind: "module".into(),
                file: node_file_for_kind(NodeKind::Module, None, &["src/mod.py".into()]),
                community: None,
            },
        ],
        Vec::new(),
    );
    let by_id = payload
        .nodes
        .iter()
        .map(|node| (node.id.as_str(), node.file.clone()))
        .collect::<std::collections::BTreeMap<_, _>>();
    assert_eq!(by_id["file:src/a.py"], Some("src/a.py".into()));
    assert_eq!(by_id["external:ext.Type"], None);
    assert_eq!(by_id["unresolved:Missing"], None);
    assert_eq!(by_id["module:pkg.mod"], Some("src/mod.py".into()));
}
