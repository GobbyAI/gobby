use super::lifecycle::{
    GRAPH_SYNC_CONTRACT_EXIT_CODE, GraphFileSyncOutcome, GraphSyncContractError,
    complete_graph_sync, format_success_text, has_no_graph_facts,
    skipped_missing_indexed_file_payload, skipped_no_graph_facts_payload,
    skipped_superseded_payload,
};
use super::payload::format_report_text;
use super::reads::{
    format_blast_radius_result_line, format_callee_result_line, format_caller_result_line,
    format_grouped_graph_results, format_symbol_path_text, format_usage_result_line,
};
use super::{imports, report};
use crate::commands::token_budget;
use crate::config::Context;
use crate::graph::code_graph::{self, GraphLifecycleAction, GraphLifecycleOutput};
use crate::models::{
    GraphPathStep, GraphResult, PagedResponse, ProjectionMetadata, ProjectionProvenance,
};
use crate::output::Format;
use clap::Parser;
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
        machine_id: gobby_core::machine::read_local_machine_id().expect("read machine id"),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: crate::config::ProjectIndexScope::Single,
    }
}

#[test]
fn graph_reads_degrade_when_falkor_missing() {
    let ctx = make_ctx_no_falkordb();

    let result = imports(&ctx, "src/lib.rs", None, 0, None, Format::Text);

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
            confidence: ProjectionProvenance::Extracted,
            relation: Some("CALLS".to_string()),
            distance: None,
            metadata: None,
            node_kind: None,
        },
        GraphResult {
            id: "a2".to_string(),
            name: "zeta".to_string(),
            file_path: "src/a.rs".to_string(),
            line: 8,
            confidence: ProjectionProvenance::Extracted,
            relation: Some("CALLS".to_string()),
            distance: None,
            metadata: None,
            node_kind: None,
        },
        GraphResult {
            id: "a1".to_string(),
            name: "alpha".to_string(),
            file_path: "src/a.rs".to_string(),
            line: 3,
            confidence: ProjectionProvenance::Extracted,
            relation: Some("CALLS".to_string()),
            distance: None,
            metadata: None,
            node_kind: None,
        },
    ];

    let text = format_grouped_graph_results(&results, |result| {
        format!("{} {}", result.line, result.name)
    });

    assert_eq!(text, "src/a.rs\n3 alpha\n8 zeta\nsrc/b.rs\n9 beta");
}

#[test]
fn graph_read_text_lines_surface_confidence_labels() {
    let result = GraphResult {
        id: "sym-1".to_string(),
        name: "run".to_string(),
        file_path: "src/lib.rs".to_string(),
        line: 12,
        confidence: ProjectionProvenance::Inferred,
        relation: Some("CALLS".to_string()),
        distance: Some(2),
        metadata: None,
        node_kind: None,
    };

    assert_eq!(
        format_caller_result_line(&result, "main"),
        "12 [INFERRED] run -> main"
    );
    assert_eq!(
        format_usage_result_line(&result, "main"),
        "12 [INFERRED] [CALLS] run -> main"
    );
    assert_eq!(
        format_blast_radius_result_line(&result),
        "12 [INFERRED] [distance=2] run"
    );
}

#[test]
fn graph_read_token_budget_uses_rendered_rows() {
    let first = GraphResult {
        id: "sym-1".to_string(),
        name: "run".to_string(),
        file_path: "src/lib.rs".to_string(),
        line: 12,
        confidence: ProjectionProvenance::Extracted,
        relation: Some("CALLS".to_string()),
        distance: Some(1),
        metadata: None,
        node_kind: None,
    };
    let second = GraphResult {
        id: "sym-2".to_string(),
        name: "run_more".repeat(100),
        file_path: "src/lib.rs".to_string(),
        line: 18,
        confidence: ProjectionProvenance::Inferred,
        relation: Some("CALLS".to_string()),
        distance: Some(2),
        metadata: None,
        node_kind: None,
    };
    let render_usages = |rows: &[GraphResult], next_offset, _| {
        token_budget::render_text_page(
            &format_grouped_graph_results(rows, |result| format_usage_result_line(result, "main")),
            next_offset,
        )
    };
    let budget =
        token_budget::estimate_tokens(&render_usages(std::slice::from_ref(&first), Some(1), false));
    let page = token_budget::paginate_results(
        vec![first.clone(), second.clone()],
        0,
        false,
        Some(budget),
        render_usages,
    );

    assert_eq!(page.results.len(), 1);
    assert_eq!(page.results[0].id, first.id);
    assert_eq!(page.next_offset, Some(1));

    let render_blast = |rows: &[GraphResult], next_offset, _| {
        token_budget::render_text_page(
            &format_grouped_graph_results(rows, format_blast_radius_result_line),
            next_offset,
        )
    };
    let blast_budget =
        token_budget::estimate_tokens(&render_blast(std::slice::from_ref(&first), Some(1), false));
    let blast_page = token_budget::paginate_results(
        vec![first.clone(), second],
        0,
        false,
        Some(blast_budget),
        render_blast,
    );

    assert_eq!(blast_page.results.len(), 1);
    assert_eq!(blast_page.results[0].id, first.id);
    assert_eq!(blast_page.next_offset, Some(1));
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
    let skipped = skipped_missing_indexed_file_payload(&ctx, "src/missing.rs", &[]);

    assert_eq!(error.exit_code(), GRAPH_SYNC_CONTRACT_EXIT_CODE);
    assert_eq!(error.payload()["reason"], "indexed_file_not_found");
    assert_eq!(
        skipped,
        json!({
            "success": true,
            "project_id": "test-project",
            "file_path": "src/missing.rs",
            "status": "skipped",
            "reason": "indexed_file_not_found",
            "synced_files": 0,
            "synced_symbols": 0,
            "skipped_files": 1,
            "failed_files": 0,
            "relationships_written": 0,
            "degraded": false,
            "error": null,
            "summary": "skipped graph sync for src/missing.rs: indexed file not found",
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
            "skipped_files": 1,
            "failed_files": 0,
            "relationships_written": 0,
            "degraded": false,
            "error": null,
            "summary": "skipped graph sync for docs/generated.json: no graph facts",
        })
    );
}

#[test]
fn superseded_skip_payload_is_terminal_success_shape() {
    let ctx = make_ctx_no_falkordb();
    let skipped = skipped_superseded_payload(&ctx, "src/lib.rs");

    assert_eq!(
        skipped,
        json!({
            "success": true,
            "project_id": "test-project",
            "file_path": "src/lib.rs",
            "status": "skipped",
            "reason": "sync_superseded",
            "synced_files": 0,
            "synced_symbols": 0,
            "skipped_files": 1,
            "failed_files": 0,
            "relationships_written": 0,
            "degraded": false,
            "error": null,
            "summary": "skipped graph sync for src/lib.rs: superseded by a newer index",
        })
    );
}

mod serial_db {
    use super::*;
    use crate::db;
    use crate::index::api;
    use crate::models::{CODE_INDEX_UUID_NAMESPACE, IndexedFile, IndexedProject};
    use std::path::Path;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn graph_completion_reports_superseded_after_state_moves() {
        let (mut conn, project_id, _cleanup) = seeded_file("gcode-graph-completion", "hash-a");
        let old = db::mark_graph_sync_attempted(&mut conn, &project_id, "src/lib.rs")
            .expect("attempt hash-a")
            .expect("live row");
        assert_eq!(old.content_hash, "hash-a");

        upsert_content_version(&mut conn, &project_id, "hash-b");
        point_file_state(&mut conn, &project_id, "hash-b");

        let superseded = complete_graph_sync(
            &mut conn,
            &project_id,
            "src/lib.rs",
            &old,
            GraphFileSyncOutcome::Synced {
                relationships_written: 3,
                symbols_synced: 2,
            },
        )
        .expect("complete superseded attempt");
        assert!(
            matches!(superseded, GraphFileSyncOutcome::SkippedSuperseded),
            "completing a superseded attempt must report SkippedSuperseded"
        );
        assert_eq!(
            graph_sync_flags(&mut conn, &project_id, "hash-b"),
            (false, true),
            "superseded completion must leave the current row unsynced with no attempt"
        );

        let current = db::mark_graph_sync_attempted(&mut conn, &project_id, "src/lib.rs")
            .expect("attempt hash-b")
            .expect("live row");
        let completed = complete_graph_sync(
            &mut conn,
            &project_id,
            "src/lib.rs",
            &current,
            GraphFileSyncOutcome::SkippedNoGraphFacts,
        )
        .expect("complete current attempt");
        assert!(
            matches!(completed, GraphFileSyncOutcome::SkippedNoGraphFacts),
            "completing the current attempt must return the given outcome"
        );
        assert_eq!(
            graph_sync_flags(&mut conn, &project_id, "hash-b"),
            (true, false),
            "completing the current attempt must mark the row synced"
        );
    }

    fn seeded_file(prefix: &str, hash: &str) -> (postgres::Client, String, ProjectCleanup) {
        let database_url = crate::test_env::postgres_test_database_url("graph completion tests");
        let mut conn = db::connect_readwrite(&database_url).expect("connect");
        let project_id = unique_id(prefix);
        cleanup_project(&mut conn, &project_id).expect("pre-clean");
        let cleanup = ProjectCleanup {
            database_url,
            project_id: project_id.clone(),
        };
        let machine_id = gobby_core::machine::read_local_machine_id().expect("machine");
        api::upsert_project_stats(
            &mut conn,
            &machine_id,
            &IndexedProject {
                id: project_id.clone(),
                root_path: format!("/nonexistent/{project_id}"),
                total_files: 1,
                total_symbols: 0,
                last_indexed_at: String::new(),
                index_duration_ms: 0,
                total_eligible_files: None,
                indexer_version: None,
            },
            api::IndexWriteMode::Overlay,
            true,
        )
        .expect("seed project");
        upsert_content_version(&mut conn, &project_id, hash);
        point_file_state(&mut conn, &project_id, hash);
        (conn, project_id, cleanup)
    }

    fn upsert_content_version(conn: &mut postgres::Client, project_id: &str, hash: &str) {
        api::upsert_file(
            conn,
            &IndexedFile {
                id: IndexedFile::make_id(project_id, "src/lib.rs", hash),
                project_id: project_id.to_string(),
                file_path: "src/lib.rs".to_string(),
                language: "rust".to_string(),
                content_hash: hash.to_string(),
                symbol_count: 0,
                byte_size: 16,
                indexed_at: String::new(),
            },
        )
        .expect("upsert content version");
    }

    fn point_file_state(conn: &mut postgres::Client, project_id: &str, hash: &str) {
        let machine_id = gobby_core::machine::read_local_machine_id().expect("machine");
        api::file_state::upsert_file_state(
            conn,
            &machine_id,
            &IndexedFile {
                id: IndexedFile::make_id(project_id, "src/lib.rs", hash),
                project_id: project_id.to_string(),
                file_path: "src/lib.rs".to_string(),
                language: "rust".to_string(),
                content_hash: hash.to_string(),
                symbol_count: 0,
                byte_size: 16,
                indexed_at: String::new(),
            },
            Path::new("/nonexistent/graph-completion"),
            api::IndexWriteMode::Overlay,
        )
        .expect("point file state");
    }

    fn graph_sync_flags(conn: &mut postgres::Client, project_id: &str, hash: &str) -> (bool, bool) {
        let project_uuid = db::id_param(project_id).expect("uuid");
        let row = conn
            .query_one(
                "SELECT graph_synced, graph_sync_attempted_at IS NULL
                 FROM code_indexed_files
                 WHERE project_id = $1 AND file_path = 'src/lib.rs' AND content_hash = $2",
                &[&project_uuid, &hash],
            )
            .expect("load flags");
        (row.get(0), row.get(1))
    }

    fn cleanup_project(conn: &mut postgres::Client, project_id: &str) -> anyhow::Result<()> {
        let project_id = db::id_param(project_id)?;
        conn.execute(
            "DELETE FROM code_indexed_file_states WHERE project_id = $1",
            &[&project_id],
        )?;
        conn.execute(
            "DELETE FROM code_indexed_project_states WHERE project_id = $1",
            &[&project_id],
        )?;
        conn.execute(
            "DELETE FROM code_indexed_files WHERE project_id = $1",
            &[&project_id],
        )?;
        conn.execute(
            "DELETE FROM code_indexed_projects WHERE id = $1",
            &[&project_id],
        )?;
        Ok(())
    }

    fn unique_id(prefix: &str) -> String {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("epoch")
            .as_nanos();
        uuid::Uuid::new_v5(
            &CODE_INDEX_UUID_NAMESPACE,
            format!("{prefix}-{}-{nanos}", std::process::id()).as_bytes(),
        )
        .to_string()
    }

    struct ProjectCleanup {
        database_url: String,
        project_id: String,
    }

    impl Drop for ProjectCleanup {
        fn drop(&mut self) {
            if let Ok(mut conn) = db::connect_readwrite(&self.database_url) {
                let _ = cleanup_project(&mut conn, &self.project_id);
            }
        }
    }
}

#[test]
fn no_graph_facts_requires_empty_imports_definitions_calls_and_inheritance() {
    let empty: &[()] = &[];
    let present: &[()] = &[()];

    assert!(has_no_graph_facts(empty, empty, empty, empty));
    assert!(!has_no_graph_facts(present, empty, empty, empty));
    assert!(!has_no_graph_facts(empty, present, empty, empty));
    assert!(!has_no_graph_facts(empty, empty, present, empty));
    assert!(!has_no_graph_facts(empty, empty, empty, present));
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
            confidence: ProjectionProvenance::Extracted,
            relation: Some("CALLS".to_string()),
            distance: Some(1),
            metadata: None,
            node_kind: None,
        }],
        next_offset: None,
        budget_exceeded: false,
        hint: None,
        warnings: Vec::new(),
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
    assert_eq!(value["results"][0]["confidence"], "EXTRACTED");
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
            confidence: ProjectionProvenance::Inferred,
            relation: Some("CALLS".to_string()),
            distance: Some(1),
            metadata: Some(
                ProjectionMetadata::new(ProjectionProvenance::Extracted, "gcode")
                    .with_source_file_path("src/lib.rs"),
            ),
            node_kind: None,
        }],
        next_offset: None,
        budget_exceeded: false,
        hint: None,
        warnings: Vec::new(),
    };
    let value = serde_json::to_value(&response).expect("serialize metadata response");

    assert_eq!(value["results"][0]["confidence"], "INFERRED");
    assert_eq!(
        value["results"][0]["metadata"]["source_file_path"],
        "src/lib.rs"
    );
}

fn graph_result(id: &str, name: &str, file_path: &str, line: usize) -> GraphResult {
    GraphResult {
        id: id.to_string(),
        name: name.to_string(),
        file_path: file_path.to_string(),
        line,
        confidence: ProjectionProvenance::Extracted,
        relation: Some("CALLS".to_string()),
        distance: None,
        metadata: None,
        node_kind: None,
    }
}

#[test]
fn callees_mirrors_callers_pagination() {
    let callers = crate::cli::Cli::try_parse_from([
        "gcode",
        "callers",
        "handleAuth",
        "--limit",
        "3",
        "--offset",
        "2",
    ])
    .expect("callers parses");
    let callees = crate::cli::Cli::try_parse_from([
        "gcode",
        "callees",
        "handleAuth",
        "--limit",
        "3",
        "--offset",
        "2",
    ])
    .expect("callees parses");
    match (callers.command, callees.command) {
        (
            crate::cli::Command::Callers {
                limit: callers_limit,
                offset: callers_offset,
                ..
            },
            crate::cli::Command::Callees {
                limit: callees_limit,
                offset: callees_offset,
                ..
            },
        ) => {
            assert_eq!(callers_limit, callees_limit);
            assert_eq!(callers_offset, callees_offset);
        }
        _ => panic!("expected callers and callees"),
    }

    let result = graph_result("sym-2", "callee", "src/lib.rs", 12);
    assert_eq!(
        format_callee_result_line(&result, "main"),
        "12 [EXTRACTED] main -> callee"
    );

    let page = PagedResponse {
        project_id: "project-123".to_string(),
        total: 5,
        offset: 2,
        limit: 3,
        results: vec![result],
        next_offset: Some(3),
        budget_exceeded: false,
        hint: None,
        warnings: Vec::new(),
    };
    let value = serde_json::to_value(&page).expect("serialize");
    assert_eq!(value["total"], 5);
    assert_eq!(value["offset"], 2);
    assert_eq!(value["limit"], 3);
    assert!(value.get("token_budget").is_none());

    let ctx = make_ctx_no_falkordb();
    super::callees(&ctx, "handleAuth", 10, 0, None, Format::Json).expect("callees degrades");
}

#[test]
fn callees_paginates_past_max_graph_limit() {
    let all = (0..102)
        .map(|index| {
            graph_result(
                &format!("callee-{index:03}"),
                &format!("callee-{index:03}"),
                "src/lib.rs",
                index,
            )
        })
        .collect::<Vec<_>>();
    let fetch_from = |all: Vec<GraphResult>| {
        move |offset: usize, limit: usize| {
            let end = (offset + limit).min(all.len());
            Ok(if offset >= all.len() {
                Vec::new()
            } else {
                all[offset..end].to_vec()
            })
        }
    };
    let first = code_graph::collect_paged_graph_results(
        0,
        50,
        code_graph::MAX_GRAPH_LIMIT,
        fetch_from(all.clone()),
        Ok,
    )
    .expect("first page");
    let second = code_graph::collect_paged_graph_results(
        50,
        50,
        code_graph::MAX_GRAPH_LIMIT,
        fetch_from(all.clone()),
        Ok,
    )
    .expect("second page");
    let third = code_graph::collect_paged_graph_results(
        100,
        50,
        code_graph::MAX_GRAPH_LIMIT,
        fetch_from(all),
        Ok,
    )
    .expect("third page");

    assert_eq!(first.len(), 50);
    assert_eq!(second.len(), 50);
    assert_eq!(third.len(), 2);
    assert_eq!(first[0].id, "callee-000");
    assert_eq!(second[0].id, "callee-050");
    assert_eq!(third[0].id, "callee-100");
    assert_eq!(third[1].id, "callee-101");
    let mut ids = first
        .iter()
        .chain(&second)
        .chain(&third)
        .map(|result| result.id.clone())
        .collect::<Vec<_>>();
    ids.sort();
    ids.dedup();
    assert_eq!(ids.len(), 102);
}

#[test]
fn callees_keeps_external_and_unresolved_targets() {
    use std::collections::HashSet;

    let local_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
    let external_id =
        crate::models::make_external_symbol_id("project-1", "print", Some("builtins"));
    let unresolved_id = crate::models::make_unresolved_callee_id("project-1", "missing_fn");
    let mut local = graph_result(local_id, "local", "src/lib.rs", 1);
    local.node_kind = Some("symbol".into());
    let mut external = graph_result(&external_id, "print", "src/lib.rs", 2);
    external.node_kind = Some("external".into());
    let mut unresolved = graph_result(&unresolved_id, "missing_fn", "src/lib.rs", 3);
    unresolved.node_kind = Some("unresolved".into());
    let mut hidden_external = graph_result(&external_id, "print", "src/hidden.rs", 4);
    hidden_external.node_kind = Some("external".into());

    let visible_ids = HashSet::from([local_id.to_string()]);
    let visible_paths = HashSet::from(["src/lib.rs".to_string()]);

    assert!(crate::visibility::graph::graph_result_is_visible(
        &local,
        &visible_ids,
        &visible_paths
    ));
    assert!(crate::visibility::graph::graph_result_is_visible(
        &external,
        &visible_ids,
        &visible_paths
    ));
    assert!(crate::visibility::graph::graph_result_is_visible(
        &unresolved,
        &visible_ids,
        &visible_paths
    ));
    assert!(!crate::visibility::graph::graph_result_is_visible(
        &hidden_external,
        &visible_ids,
        &visible_paths
    ));
    let mut hidden_local = local.clone();
    hidden_local.id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb".into();
    assert!(!crate::visibility::graph::graph_result_is_visible(
        &hidden_local,
        &visible_ids,
        &visible_paths
    ));
}
