use super::lifecycle::{
    GRAPH_SYNC_CONTRACT_EXIT_CODE, GraphSyncContractError, format_success_text,
    skipped_missing_indexed_file_payload,
};
use super::payload::format_report_text;
use super::reads::format_grouped_graph_results;
use super::{imports, report};
use crate::config::Context;
use crate::graph::code_graph::{self, GraphLifecycleAction, GraphLifecycleOutput};
use crate::models::{GraphResult, PagedResponse, ProjectionMetadata, ProjectionProvenance};
use crate::output::Format;
use serde_json::json;
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
fn report_text_structured_output() {
    let report = crate::graph::report::empty_report("project-123");

    let text = format_report_text(&report).expect("format report text");
    let value: serde_json::Value = serde_json::from_str(&text).expect("structured JSON text");

    assert_eq!(value["project_id"], "project-123");
    assert_eq!(value["summary"]["node_count"], 0);
    assert!(
        value["markdown"]
            .as_str()
            .expect("markdown field")
            .contains("# Project Graph Report")
    );
    assert!(!text.trim_start().starts_with('#'));
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

#[test]
fn graph_lifecycle_commands_call_core_directly() {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let source = std::fs::read_to_string(manifest_dir.join("src/commands/graph/lifecycle.rs"))
        .expect("read commands/graph/lifecycle.rs");
    let clear_project = ["code_graph", "::clear_project(ctx)"].concat();
    let sync_file_graph = ["code_graph", "::sync_file_graph("].concat();
    let lifecycle_request = ["GraphLifecycleRequest", "::from_context"].concat();
    let daemon_lifecycle = ["code_graph", "::run_lifecycle_action"].concat();

    assert!(source.contains(&clear_project));
    assert!(source.contains(&sync_file_graph));
    assert!(!source.contains(&lifecycle_request));
    assert!(!source.contains(&daemon_lifecycle));
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
