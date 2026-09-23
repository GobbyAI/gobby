use super::*;
use crate::cli::{GraphViewArgs, GraphViewKind};
use crate::communities::LabelSource;
use crate::config::{CodeVectorSettings, ProjectIndexScope};
use gobby_core::config::IndexingConfig;
use gobby_core::mermaid::is_valid_mermaid;
use std::path::PathBuf;
use std::time::SystemTime;

fn ctx() -> Context {
    Context {
        database_url: "postgresql://localhost/unused".into(),
        project_root: PathBuf::from("/repo"),
        project_id: "project-1".into(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: CodeVectorSettings::default(),
        runtime_config_capture_degraded: false,
        indexing: IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: ProjectIndexScope::Single,
    }
}

fn args(min_size: Option<usize>) -> GraphViewArgs {
    GraphViewArgs {
        view: GraphViewKind::Communities,
        seed: None,
        depth: None,
        incoming_limit: None,
        outgoing_limit: None,
        min_size,
    }
}

fn community(
    id: i32,
    label: &str,
    deterministic: &str,
    members: &[&str],
    boundary: &[(i32, usize)],
) -> StoredCommunity {
    StoredCommunity {
        machine_id: "machine-1".into(),
        project_id: "project-1".into(),
        community_id: id,
        member_count: members.len(),
        members: members.iter().map(|member| (*member).to_string()).collect(),
        representatives: members
            .iter()
            .take(5)
            .map(|member| (*member).to_string())
            .collect(),
        internal_edges: members.len().saturating_sub(1),
        cohesion: 0.75,
        boundary: boundary.to_vec(),
        member_signature: format!("signature-{id}"),
        label_deterministic: deterministic.into(),
        label: label.into(),
        label_source: LabelSource::Model,
        label_confidence: Some(0.9),
        label_model: Some("test-model".into()),
        label_candidates: Vec::new(),
        labeled_signature: Some(format!("signature-{id}")),
        labeled_at: Some(SystemTime::UNIX_EPOCH),
        label_attempted_at: Some(SystemTime::UNIX_EPOCH),
        refreshed_at: SystemTime::UNIX_EPOCH,
        label_stale: false,
    }
}

fn typed_usage(error: &anyhow::Error, selector: &str) {
    let typed = error
        .downcast_ref::<crate::cli_error::CliError>()
        .expect("selector failures are typed CLI errors");
    assert_eq!(typed.exit_status, 2);
    assert!(typed.message.contains(selector), "{}", typed.message);
}

#[test]
fn min_size_hides_singletons_by_default() {
    let rows = vec![
        community(1, "solo", "solo", &["solo.rs"], &[]),
        community(2, "pair", "pair", &["a.rs", "b.rs"], &[]),
    ];
    let payload = build_list_payload(&ctx(), &args(None), &rows).expect("list payload");
    assert_eq!(payload.nodes.len(), 1);
    assert_eq!(payload.nodes[0].id, "community:2");
    assert_eq!(payload.communities.len(), 1);
    assert!(payload.communities[0].nodes.is_empty());
}

#[test]
fn list_edges_are_between_listed_communities_only() {
    let rows = vec![
        community(1, "one", "one", &["a", "b"], &[(2, 7), (3, 11)]),
        community(2, "two", "two", &["c", "d"], &[(1, 7), (3, 13)]),
        community(3, "hidden", "hidden", &["e"], &[(1, 11), (2, 13)]),
    ];
    let payload = build_list_payload(&ctx(), &args(None), &rows).expect("list payload");
    assert_eq!(payload.edges.len(), 1);
    assert_eq!(payload.edges[0].source, "community:1");
    assert_eq!(payload.edges[0].target, "community:2");
    assert_eq!(payload.edges[0].count, Some(7));
}

#[test]
fn list_orders_communities_by_size_then_first_member() {
    let rows = vec![
        community(1, "small", "small", &["lib/a.rs", "lib/b.rs"], &[]),
        community(
            2,
            "later",
            "later",
            &["src/c.rs", "src/d.rs", "src/e.rs"],
            &[],
        ),
        community(
            10,
            "earlier",
            "earlier",
            &["src/a.rs", "src/b.rs", "src/f.rs"],
            &[],
        ),
    ];
    let payload = build_list_payload(&ctx(), &args(None), &rows).expect("list payload");
    let order = payload
        .communities
        .iter()
        .map(|community| community.id.as_str())
        .collect::<Vec<_>>();
    assert_eq!(order, ["community:10", "community:2", "community:1"]);
}

#[test]
fn detail_resolves_id_label_and_unique_substring() {
    let rows = vec![
        community(7, "Memory Core", "memory-core", &["memory.rs"], &[]),
        community(8, "Renderer", "rendering", &["render.rs"], &[]),
    ];
    assert_eq!(resolve_selector(&rows, "7").expect("id").community_id, 7);
    assert_eq!(
        resolve_selector(&rows, "MEMORY CORE")
            .expect("case-insensitive label")
            .community_id,
        7
    );
    assert_eq!(
        resolve_selector(&rows, "rendering")
            .expect("deterministic label")
            .community_id,
        8
    );
    assert_eq!(
        resolve_selector(&rows, "memor")
            .expect("unique substring")
            .community_id,
        7
    );
}

#[test]
fn detail_resolves_member_path() {
    let rows = vec![community(
        4,
        "CLI",
        "cli",
        &["crates/gcode/src/lib.rs", "crates/gcode/src/main.rs"],
        &[],
    )];
    assert_eq!(
        resolve_selector(&rows, "crates/gcode/src/lib.rs")
            .expect("member path")
            .community_id,
        4
    );
}

#[test]
fn detail_unknown_path_is_typed_error() {
    let rows = vec![community(1, "CLI", "cli", &["src/lib.rs"], &[])];
    let error = resolve_selector(&rows, "src/missing.rs").expect_err("unknown path fails");
    assert_eq!(
        error
            .downcast_ref::<crate::cli_error::CliError>()
            .map(|typed| typed.exit_status),
        Some(2)
    );
    typed_usage(&error, "src/missing.rs");
}

#[test]
fn detail_ambiguous_substring_is_typed_error() {
    let rows = (1..=7)
        .map(|id| {
            community(
                id,
                &format!("shared-{id}"),
                &format!("shared-{id}"),
                &["x"],
                &[],
            )
        })
        .collect::<Vec<_>>();
    let error = resolve_selector(&rows, "shared").expect_err("ambiguous substring fails");
    typed_usage(&error, "shared");
    let typed = error
        .downcast_ref::<crate::cli_error::CliError>()
        .expect("typed error");
    assert_eq!(typed.message.matches("community:").count(), 5);
}

#[test]
fn detail_no_match_and_multi_match_are_typed_errors_at_every_rung() {
    let rows = vec![
        community(1, "Duplicate", "alpha-shared", &["a.rs"], &[]),
        community(2, "Duplicate", "beta-shared", &["b.rs"], &[]),
    ];
    for selector in ["99", "missing", "Duplicate", "shared"] {
        let error = resolve_selector(&rows, selector).expect_err("selector must fail");
        assert_eq!(
            error
                .downcast_ref::<crate::cli_error::CliError>()
                .map(|typed| typed.exit_status),
            Some(2)
        );
        typed_usage(&error, selector);
    }
}

#[test]
fn detail_truncates_members_and_neighbors_with_flags() {
    let members = (0..55)
        .map(|index| format!("src/{index:02}.rs"))
        .collect::<Vec<_>>();
    let member_refs = members.iter().map(String::as_str).collect::<Vec<_>>();
    let boundary = (2..=15).map(|id| (id, id as usize)).collect::<Vec<_>>();
    let mut rows = vec![community(1, "root", "root", &member_refs, &boundary)];
    rows.extend((2..=15).map(|id| {
        community(
            id,
            &format!("neighbor-{id}"),
            &format!("neighbor-{id}"),
            &["neighbor.rs"],
            &[(1, id as usize)],
        )
    }));
    let payload = build_detail_payload(&ctx(), &args(None), "1", &rows).expect("detail");
    let file_nodes = payload
        .nodes
        .iter()
        .filter(|node| node.kind == "file")
        .count();
    let community_nodes = payload
        .nodes
        .iter()
        .filter(|node| node.kind == "community")
        .count();
    assert_eq!(file_nodes, 50);
    assert_eq!(community_nodes, 13);
    assert!(payload.outgoing_truncated);
    assert!(payload.incoming_truncated);
}

#[test]
fn list_without_rows_hints() {
    let payload = build_list_payload(&ctx(), &args(None), &[]).expect("empty list");
    assert!(payload.nodes.is_empty());
    assert_eq!(payload.hint.as_deref(), Some(MISSING_PARTITION_HINT));
}

#[test]
fn mermaid_validates_for_list_and_detail() {
    let rows = vec![
        community(1, "one", "one", &["a.rs", "b.rs"], &[(2, 3)]),
        community(2, "two", "two", &["c.rs", "d.rs"], &[(1, 3)]),
    ];
    let list = build_list_payload(&ctx(), &args(None), &rows).expect("list");
    let detail = build_detail_payload(&ctx(), &args(None), "1", &rows).expect("detail");
    assert!(is_valid_mermaid(&list.mermaid));
    assert!(is_valid_mermaid(&detail.mermaid));
}
