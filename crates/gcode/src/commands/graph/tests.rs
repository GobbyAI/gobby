use super::lifecycle::{
    GRAPH_SYNC_CONTRACT_EXIT_CODE, GraphSyncContractError, format_success_text, has_no_graph_facts,
    skipped_missing_indexed_file_payload, skipped_no_graph_facts_payload,
};
use super::payload::format_report_text;
use super::reads::{format_grouped_graph_results, format_symbol_path_text};
use super::{imports, report};
use crate::config::Context;
use crate::graph::code_graph::{self, GraphLifecycleAction, GraphLifecycleOutput};
use crate::models::{
    GraphPathStep, GraphResult, PagedResponse, ProjectionMetadata, ProjectionProvenance,
};
use crate::output::Format;
use serde_json::json;
use std::cell::RefCell;
use std::path::PathBuf;

fn make_ctx_no_falkordb() -> Context {
    Context {
        database_url: "postgresql://localhost/nonexistent".to_string(),
        project_root: PathBuf::from("/nonexistent"),
        project_id: "test-project".to_string(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: crate::config::CodeVectorSettings::default(),
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        index_scope: crate::config::ProjectIndexScope::Single,
    }
}

#[test]
fn graph_reads_degrade_when_falkor_missing() {
    let ctx = make_ctx_no_falkordb();

    let result = imports(&ctx, "src/lib.rs", Format::Text);

    assert!(result.is_ok(), "imports should degrade cleanly: {result:?}");
}

#[test]
fn report_text_uses_markdown_output() {
    let report = crate::graph::report::empty_report("project-123");

    let text = format_report_text(&report);

    assert!(text.contains("# Project Graph Report"));
    assert!(text.contains("Project: `project-123`"));
    assert!(text.trim_start().starts_with('#'));
}

#[test]
fn graph_text_groups_by_file_and_sorts_entries() {
    let results = vec![
        GraphResult {
            id: "b".to_string(),
            name: "beta".to_string(),
            file_path: "src/b.rs".to_string(),
            line: 9,
            relation: Some("CALLS".to_string()),
            distance: None,
            metadata: None,
        },
        GraphResult {
            id: "a2".to_string(),
            name: "zeta".to_string(),
            file_path: "src/a.rs".to_string(),
            line: 8,
            relation: Some("CALLS".to_string()),
            distance: None,
            metadata: None,
        },
        GraphResult {
            id: "a1".to_string(),
            name: "alpha".to_string(),
            file_path: "src/a.rs".to_string(),
            line: 3,
            relation: Some("CALLS".to_string()),
            distance: None,
            metadata: None,
        },
    ];

    let text = format_grouped_graph_results(&results, |result| {
        format!("{} {}", result.line, result.name)
    });

    assert_eq!(text, "src/a.rs\n3 alpha\n8 zeta\nsrc/b.rs\n9 beta");
}

#[test]
fn graph_path_text_prints_ordered_chain_with_locations() {
    let path = vec![
        GraphPathStep {
            position: 0,
            id: "a".to_string(),
            name: "alpha".to_string(),
            file_path: "src/a.rs".to_string(),
            line: 3,
        },
        GraphPathStep {
            position: 1,
            id: "b".to_string(),
            name: "beta".to_string(),
            file_path: "src/b.rs".to_string(),
            line: 9,
        },
    ];

    let text = format_symbol_path_text("alpha", "beta", &path, 8);

    assert_eq!(
        text,
        "Shortest path from 'alpha' to 'beta' (1 hop(s)):\n1. alpha (src/a.rs:3)\n2. beta (src/b.rs:9)"
    );
}

#[test]
fn graph_path_text_reports_bounded_no_path() {
    let text = format_symbol_path_text("alpha", "beta", &[], 8);

    assert_eq!(
        text,
        "No path found from 'alpha' to 'beta' within 8 CALLS hop(s)."
    );
}

#[test]
fn report_requires_graph_service() {
    let ctx = make_ctx_no_falkordb();

    let err = report(&ctx, 10, Format::Json).expect_err("report must fail");

    assert!(matches!(
        err.downcast_ref::<crate::graph::report::ProjectGraphReportError>(),
        Some(crate::graph::report::ProjectGraphReportError::GraphServiceNotConfigured)
    ));
    assert!(
        err.to_string()
            .contains("project graph report requires FalkorDB"),
        "unexpected error: {err}"
    );
}

#[derive(Default)]
struct SpyLifecycleBackend {
    actions: RefCell<Vec<GraphLifecycleAction>>,
}

impl super::lifecycle::LifecycleBackend for SpyLifecycleBackend {
    fn run(
        &self,
        ctx: &Context,
        action: GraphLifecycleAction,
    ) -> anyhow::Result<GraphLifecycleOutput> {
        self.actions.borrow_mut().push(action);
        Ok(GraphLifecycleOutput {
            project_id: ctx.project_id.clone(),
            action,
            summary: "spy lifecycle".to_string(),
            payload: json!({
                "success": true,
                "project_id": ctx.project_id,
                "action": format!("{action:?}"),
                "summary": "spy lifecycle",
            }),
        })
    }
}

#[test]
fn graph_lifecycle_commands_dispatch_to_core_backend() {
    let ctx = make_ctx_no_falkordb();
    let backend = SpyLifecycleBackend::default();

    super::lifecycle::run_lifecycle_action_with_backend(
        &ctx,
        GraphLifecycleAction::Clear,
        Format::Json,
        &backend,
    )
    .expect("clear dispatch succeeds");
    super::lifecycle::run_lifecycle_action_with_backend(
        &ctx,
        GraphLifecycleAction::Rebuild,
        Format::Json,
        &backend,
    )
    .expect("rebuild dispatch succeeds");

    assert_eq!(
        backend.actions.into_inner(),
        vec![GraphLifecycleAction::Clear, GraphLifecycleAction::Rebuild]
    );
}

#[test]
fn missing_project_sync_error_has_typed_payload() {
    let ctx = make_ctx_no_falkordb();
    let error = GraphSyncContractError::project_not_indexed(&ctx, "src/lib.rs");

    assert_eq!(error.exit_code(), GRAPH_SYNC_CONTRACT_EXIT_CODE);
    assert_eq!(error.payload()["project_id"], "test-project");
    assert_eq!(error.payload()["file_path"], "src/lib.rs");
    assert_eq!(error.payload()["status"], "error");
    assert_eq!(error.payload()["reason"], "project_not_indexed");
}

#[test]
fn missing_file_sync_error_and_skip_payloads_are_typed() {
    let ctx = make_ctx_no_falkordb();
    let error = GraphSyncContractError::indexed_file_not_found(&ctx, "src/missing.rs");
    let skipped = skipped_missing_indexed_file_payload(&ctx, "src/missing.rs");

    assert_eq!(error.exit_code(), GRAPH_SYNC_CONTRACT_EXIT_CODE);
    assert_eq!(error.payload()["reason"], "indexed_file_not_found");
    assert_eq!(
        skipped,
        json!({
            "project_id": "test-project",
            "file_path": "src/missing.rs",
            "status": "skipped",
            "reason": "indexed_file_not_found",
        })
    );
}

#[test]
fn no_graph_facts_skip_payload_is_terminal_success_shape() {
    let ctx = make_ctx_no_falkordb();
    let skipped = skipped_no_graph_facts_payload(&ctx, "docs/generated.json");

    assert_eq!(
        skipped,
        json!({
            "success": true,
            "project_id": "test-project",
            "file_path": "docs/generated.json",
            "status": "skipped",
            "reason": "no_graph_facts",
            "synced_files": 1,
            "synced_symbols": 0,
            "relationships_written": 0,
            "summary": "skipped graph sync for docs/generated.json: no graph facts",
        })
    );
}

#[test]
fn no_graph_facts_requires_empty_imports_definitions_and_calls() {
    let empty: &[()] = &[];
    let present: &[()] = &[()];

    assert!(has_no_graph_facts(empty, empty, empty));
    assert!(!has_no_graph_facts(present, empty, empty));
    assert!(!has_no_graph_facts(empty, present, empty));
    assert!(!has_no_graph_facts(empty, empty, present));
}

#[test]
fn test_build_lifecycle_url_clear_uses_project_id_query() {
    let url = code_graph::build_lifecycle_url(
        "http://localhost:60887/",
        GraphLifecycleAction::Clear,
        "project-123",
    )
    .expect("url builds");

    assert_eq!(
        url.as_str(),
        "http://localhost:60887/api/code-index/graph/clear?project_id=project-123"
    );
}

#[test]
fn test_build_lifecycle_url_rebuild_uses_project_id_query() {
    let url = code_graph::build_lifecycle_url(
        "http://localhost:60887",
        GraphLifecycleAction::Rebuild,
        "project-123",
    )
    .expect("url builds");

    assert_eq!(
        url.as_str(),
        "http://localhost:60887/api/code-index/graph/rebuild?project_id=project-123"
    );
}

#[test]
fn test_require_daemon_url_errors_when_missing() {
    let err =
        code_graph::require_daemon_url(None, GraphLifecycleAction::Clear).expect_err("must fail");

    assert!(
        err.to_string()
            .contains("Gobby daemon URL is not configured"),
        "unexpected error: {err}"
    );
    assert!(
        err.to_string().contains("gcode graph clear"),
        "unexpected error: {err}"
    );
}

#[test]
fn test_format_http_error_includes_status_and_body() {
    let url = reqwest::Url::parse("http://localhost:60887/api/code-index/graph/clear")
        .expect("valid url");
    let message = code_graph::format_http_error(
        GraphLifecycleAction::Clear,
        &url,
        reqwest::StatusCode::BAD_GATEWAY,
        "daemon upstream unavailable",
    );

    assert!(message.contains("HTTP 502"), "unexpected error: {message}");
    assert!(
        message.contains("daemon upstream unavailable"),
        "unexpected error: {message}"
    );
}

#[test]
fn test_parse_success_payload_fails_on_invalid_json() {
    let err = code_graph::parse_success_payload(
        GraphLifecycleAction::Rebuild,
        reqwest::StatusCode::OK,
        "not json",
    )
    .expect_err("invalid json must fail");

    assert!(
        err.to_string().contains("invalid JSON"),
        "unexpected error: {err}"
    );
    assert!(
        err.to_string().contains("HTTP 200 OK"),
        "unexpected error: {err}"
    );
}

#[test]
fn test_format_success_text_prefers_message_field() {
    let payload = json!({
        "message": "cleared 12 graph nodes",
        "removed_nodes": 12
    });
    let output = GraphLifecycleOutput {
        project_id: "project-123".to_string(),
        action: GraphLifecycleAction::Clear,
        summary: "cleared 12 graph nodes".to_string(),
        payload,
    };
    let text = format_success_text(&output);

    assert_eq!(
        text,
        "Cleared code-index graph for project project-123: cleared 12 graph nodes"
    );
}

#[test]
fn test_format_success_text_falls_back_to_compact_json() {
    let payload = json!({
        "replayed": 18,
        "synced": 18
    });
    let output = GraphLifecycleOutput {
        project_id: "project-123".to_string(),
        action: GraphLifecycleAction::Rebuild,
        summary: payload.to_string(),
        payload,
    };
    let text = format_success_text(&output);

    assert_eq!(
        text,
        "Rebuilt code-index graph for project project-123: {\"replayed\":18,\"synced\":18}"
    );
}

#[test]
fn top_level_read_commands_preserve_json_shape() {
    let response = PagedResponse {
        project_id: "project-123".to_string(),
        total: 1,
        offset: 0,
        limit: 10,
        results: vec![GraphResult {
            id: "sym-1".to_string(),
            name: "run".to_string(),
            file_path: "src/lib.rs".to_string(),
            line: 12,
            relation: Some("CALLS".to_string()),
            distance: Some(1),
            metadata: None,
        }],
        hint: None,
    };

    let value = serde_json::to_value(&response).expect("serialize response");

    assert_eq!(value["project_id"], "project-123");
    assert_eq!(value["total"], 1);
    assert_eq!(value["offset"], 0);
    assert_eq!(value["limit"], 10);
    assert_eq!(value["results"][0]["id"], "sym-1");
    assert_eq!(value["results"][0]["name"], "run");
    assert_eq!(value["results"][0]["file_path"], "src/lib.rs");
    assert_eq!(value["results"][0]["line"], 12);
    assert_eq!(value["results"][0]["relation"], "CALLS");
    assert_eq!(value["results"][0]["distance"], 1);
    assert!(value["hint"].is_null());
    assert!(value["results"][0].get("metadata").is_none());

    let response = PagedResponse {
        project_id: "project-123".to_string(),
        total: 1,
        offset: 0,
        limit: 10,
        results: vec![GraphResult {
            id: "sym-1".to_string(),
            name: "run".to_string(),
            file_path: "src/lib.rs".to_string(),
            line: 12,
            relation: Some("CALLS".to_string()),
            distance: Some(1),
            metadata: Some(
                ProjectionMetadata::new(ProjectionProvenance::Extracted, "gcode")
                    .with_source_file_path("src/lib.rs"),
            ),
        }],
        hint: None,
    };
    let value = serde_json::to_value(&response).expect("serialize metadata response");

    assert_eq!(
        value["results"][0]["metadata"]["source_file_path"],
        "src/lib.rs"
    );
}
