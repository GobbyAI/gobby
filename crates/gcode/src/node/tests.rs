//! Pins the node wire contract and the hub read entry points.
//!
//! The status and content goldens were captured from the pre-split CLI output.

use std::path::{Path, PathBuf};

use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::json;

use crate::bm25_health;
use crate::commands::grep::db_tests::{
    insert_chunk_version, insert_file_version, insert_machine_state,
};
use crate::commands::scope::path_exists_in_current_project;
use crate::commands::search::content_page::{ContentQuery, content_candidates, page_content};
use crate::commands::search::output::render_content_results;
use crate::commands::status::current::{collect, render_json, render_text};
use crate::config::{CodeVectorSettings, Context, ProjectIndexScope};
use crate::index::api::{self, IndexWriteMode};
use crate::models::{CODE_INDEX_UUID_NAMESPACE, ContentSearchHit, IndexedProject};
use crate::node::hub::{HubScope, search_content as hub_search_content, status as hub_status};
use crate::node::{
    ContentCandidates, INDEX_APPLY_ROUTE, INDEX_PLAN_ROUTE, NODE_OPERATION, NODE_ROUTE_PREFIX,
    NodeScope, ROUTE_PATHS, RouteError, SEARCH_CONTENT_ROUTE, STATUS_ROUTE, SearchContentRequest,
    StatusReport, StatusRequest,
};

/// Deletes the project rows a test wrote, so reruns start clean.
struct ProjectCleanup {
    database_url: String,
    project_id: uuid::Uuid,
}

impl Drop for ProjectCleanup {
    fn drop(&mut self) {
        let Ok(mut conn) = gobby_core::postgres::connect_readwrite(&self.database_url) else {
            return;
        };
        let _ = conn.execute(
            "DELETE FROM code_indexed_file_states WHERE project_id = $1",
            &[&self.project_id],
        );
        let _ = conn.execute(
            "DELETE FROM code_indexed_project_states WHERE project_id = $1",
            &[&self.project_id],
        );
        let _ = conn.execute(
            "DELETE FROM code_indexed_projects WHERE id = $1",
            &[&self.project_id],
        );
        let _ = conn.execute(
            "DELETE FROM project_checkouts WHERE project_id = $1",
            &[&self.project_id],
        );
        let _ = conn.execute("DELETE FROM projects WHERE id = $1", &[&self.project_id]);
    }
}

/// Names are keyed on a fresh temp path, so reruns never collide on primary keys.
fn uuid_for(name: &str) -> String {
    uuid::Uuid::new_v5(&CODE_INDEX_UUID_NAMESPACE, name.as_bytes()).to_string()
}

fn local_context(database_url: &str, root: &Path, project_id: &str, machine_id: &str) -> Context {
    Context {
        machine_id: machine_id.to_string(),
        database_url: database_url.to_string(),
        project_root: root.to_path_buf(),
        project_id: project_id.to_string(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: CodeVectorSettings::default(),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: ProjectIndexScope::Single,
    }
}

fn route_error(error: RouteError) -> anyhow::Error {
    anyhow::anyhow!("{error:?}")
}

fn stats_for(project_id: &str, root_path: &str, total_files: usize) -> IndexedProject {
    IndexedProject {
        id: project_id.to_string(),
        root_path: root_path.to_string(),
        total_files,
        total_symbols: 3,
        last_indexed_at: String::new(),
        index_duration_ms: 1500,
        total_eligible_files: None,
        indexer_version: Some("1.9.70".to_string()),
    }
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires the PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
fn status_render_matches_the_cli_output() -> anyhow::Result<()> {
    let database_url = crate::test_env::postgres_test_database_url("node status render");
    let mut conn = gobby_core::postgres::connect_readwrite(&database_url)?;
    let machine_id = gobby_core::machine::read_local_machine_id()?;
    let root = tempfile::tempdir()?;
    let root_path = root.path().to_string_lossy().into_owned();
    let name = root
        .path()
        .file_name()
        .expect("temp dir has a name")
        .to_string_lossy()
        .into_owned();
    let project_id = uuid_for(&format!("{root_path}-status"));
    let unindexed_id = uuid_for(&format!("{root_path}-status-unindexed"));
    let _cleanup = ProjectCleanup {
        database_url: database_url.clone(),
        project_id: crate::db::id_param(&project_id)?,
    };
    api::upsert_project_stats(
        &mut conn,
        &machine_id,
        &stats_for(&project_id, &root_path, 2),
        IndexWriteMode::Overlay,
        false,
    )?;

    let health = bm25_health::verify(&mut conn);
    // The CLI renders code_index through a Value, so its keys come out sorted.
    let health_json = serde_json::to_string(&serde_json::to_value(&health)?)?;
    let health_text = health.render_text();
    let short_id = &project_id[..8];

    let indexed_ctx = local_context(&database_url, root.path(), &project_id, &machine_id);
    let indexed = collect(&mut conn, &indexed_ctx)?;
    let expected_json = format!(
        r#"{{"code_index":{health_json},"id":"{project_id}","index_duration_ms":1500,"indexer_version":"1.9.70","root_path":"{root_path}","total_files":2,"total_symbols":3}}"#
    );
    assert_eq!(render_json(&indexed)?, expected_json);
    let expected_text = format!(
        "{name} ({short_id})\n  Root:     {root_path}\n  Files:    2\n  Symbols:  3\n  Indexed:  never\n  Duration: 1500ms\n{health_text}"
    );
    assert_eq!(render_text(&indexed), expected_text);

    let unindexed_ctx = local_context(&database_url, root.path(), &unindexed_id, &machine_id);
    let unindexed = collect(&mut conn, &unindexed_ctx)?;
    let expected_unindexed_json =
        format!(r#"{{"code_index":{health_json},"indexed":false,"project_id":"{unindexed_id}"}}"#);
    assert_eq!(render_json(&unindexed)?, expected_unindexed_json);
    assert_eq!(render_text(&unindexed), health_text);
    Ok(())
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires the PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
fn hub_reads_run_as_the_principal_machine() -> anyhow::Result<()> {
    let database_url = crate::test_env::postgres_test_database_url("node hub reads");
    let mut conn = gobby_core::postgres::connect_readwrite(&database_url)?;
    let scratch = tempfile::tempdir()?;
    let scratch_name = scratch.path().to_string_lossy().into_owned();
    // A machine other than this one, whose checkout roots do not exist on this disk.
    let machine_id = uuid_for(&format!("{scratch_name}-machine"));
    crate::test_env::seed_test_machine(&mut conn, &machine_id).map_err(anyhow::Error::msg)?;
    let project_id = uuid_for(&format!("{scratch_name}-hub"));
    let foreign_root = format!("/nonexistent/node-{project_id}");
    let _cleanup = ProjectCleanup {
        database_url: database_url.clone(),
        project_id: crate::db::id_param(&project_id)?,
    };

    api::upsert_project_stats(
        &mut conn,
        &machine_id,
        &stats_for(&project_id, &foreign_root, 1),
        IndexWriteMode::Overlay,
        false,
    )?;
    let machine = crate::db::id_param(&machine_id)?;
    let project = crate::db::id_param(&project_id)?;
    insert_file_version(&mut conn, &project, "src/lib.rs", "hash-hub");
    insert_machine_state(
        &mut conn,
        &machine,
        &project,
        &foreign_root,
        "src/lib.rs",
        "hash-hub",
    );
    insert_chunk_version(
        &mut conn,
        &project,
        "src/lib.rs",
        "fn hubprobemarker() {}",
        "hash-hub",
    );

    let hub = HubScope {
        database_url: database_url.clone(),
        machine_id: machine_id.clone(),
        project_id: project_id.clone(),
        overlay_project_id: None,
    };
    let node_scope = NodeScope {
        root: foreign_root.clone(),
        parent_root: None,
    };

    let report = hub_status(
        &hub,
        StatusRequest {
            scope: node_scope.clone(),
        },
    )
    .map_err(route_error)?;
    let status_json = render_json(&report)?;
    assert!(
        status_json.contains(&format!(r#""root_path":"{foreign_root}""#)),
        "{status_json}"
    );
    assert!(status_json.contains(r#""total_files":1"#), "{status_json}");

    let candidates = hub_search_content(
        &hub,
        SearchContentRequest {
            scope: node_scope.clone(),
            query: "hubprobemarker".to_string(),
            language: None,
            paths: Vec::new(),
            limit: 10,
            offset: 0,
        },
    )
    .map_err(route_error)?;
    assert_eq!(candidates.db_total, Some(1));
    assert_eq!(candidates.results.len(), 1);
    assert_eq!(candidates.results[0].file_path, "src/lib.rs");

    let overlay_claim = NodeScope {
        root: foreign_root.clone(),
        parent_root: Some("/nonexistent/parent".to_string()),
    };
    let error = hub_status(
        &hub,
        StatusRequest {
            scope: overlay_claim,
        },
    )
    .expect_err("an overlay request against a single-project principal must be refused");
    assert_eq!(error.code, "claims_mismatch");

    let overlay_hub = HubScope {
        overlay_project_id: Some(uuid_for(&format!("{scratch_name}-overlay"))),
        ..hub
    };
    let error = hub_status(&overlay_hub, StatusRequest { scope: node_scope })
        .expect_err("a single-project request against an overlay principal must be refused");
    assert_eq!(error.code, "claims_mismatch");
    Ok(())
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires the PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
fn content_search_split_matches_the_cli_output() -> anyhow::Result<()> {
    let database_url = crate::test_env::postgres_test_database_url("node content split");
    let mut conn = gobby_core::postgres::connect_readwrite(&database_url)?;
    let machine_id = gobby_core::machine::read_local_machine_id()?;
    let machine = crate::db::id_param(&machine_id)?;
    let root = tempfile::tempdir()?;
    let root_path = root.path().to_string_lossy().into_owned();
    let project_id = uuid_for(&format!("{root_path}-content"));
    let _cleanup = ProjectCleanup {
        database_url: database_url.clone(),
        project_id: crate::db::id_param(&project_id)?,
    };

    std::fs::create_dir_all(root.path().join("src"))?;
    std::fs::write(
        root.path().join("src/lib.rs"),
        "fn searchprobemarker() {}\n",
    )?;
    std::fs::write(
        root.path().join("src/gone.rs"),
        "fn searchprobemarker() {}\n",
    )?;
    api::upsert_project_stats(
        &mut conn,
        &machine_id,
        &stats_for(&project_id, &root_path, 2),
        IndexWriteMode::Overlay,
        false,
    )?;
    let project = crate::db::id_param(&project_id)?;
    for file_path in ["src/lib.rs", "src/gone.rs"] {
        let hash = format!("hash-{file_path}");
        insert_file_version(&mut conn, &project, file_path, &hash);
        insert_machine_state(&mut conn, &machine, &project, &root_path, file_path, &hash);
        insert_chunk_version(
            &mut conn,
            &project,
            file_path,
            "fn searchprobemarker() {}",
            &hash,
        );
    }
    // Indexed, then deleted on disk: the database still counts it.
    std::fs::remove_file(root.path().join("src/gone.rs"))?;

    let ctx = local_context(&database_url, root.path(), &project_id, &machine_id);
    let paths: Vec<String> = Vec::new();
    let candidates = content_candidates(
        &mut conn,
        &ctx,
        "searchprobemarker",
        ContentQuery {
            language: None,
            paths: &paths,
            limit: 10,
            offset: 0,
        },
    )?;
    assert_eq!(candidates.db_total, Some(2));

    let page = page_content(candidates, 0, 10, |file_path| {
        path_exists_in_current_project(&ctx, file_path)
    });
    assert_eq!(page.total, 2);
    assert_eq!(page.next_offset, Some(1));
    assert_eq!(
        serde_json::to_string(&page.results)?,
        r#"[{"file_path":"src/lib.rs","line_start":1,"line_end":1,"snippet":"fn searchprobemarker() {}","language":"rust"}]"#
    );
    assert_eq!(
        render_content_results(&page.results),
        "src/lib.rs:1-1 fn searchprobemarker() {}"
    );

    // The second page starts past the only present hit. It is empty, so it has no next page.
    let candidates = content_candidates(
        &mut conn,
        &ctx,
        "searchprobemarker",
        ContentQuery {
            language: None,
            paths: &paths,
            limit: 10,
            offset: 1,
        },
    )?;
    let page = page_content(candidates, 1, 10, |file_path| {
        path_exists_in_current_project(&ctx, file_path)
    });
    assert!(page.results.is_empty());
    assert_eq!(page.total, 2);
    assert_eq!(page.next_offset, None);

    // A wire offset near the top of usize must page to nothing, not overflow the fetch cap.
    let candidates = content_candidates(
        &mut conn,
        &ctx,
        "searchprobemarker",
        ContentQuery {
            language: None,
            paths: &paths,
            limit: 10,
            offset: usize::MAX,
        },
    )?;
    let page = page_content(candidates, usize::MAX, 10, |file_path| {
        path_exists_in_current_project(&ctx, file_path)
    });
    assert!(page.results.is_empty());
    assert_eq!(page.next_offset, None);
    Ok(())
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires the PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
fn status_render_matches_the_cli_output_for_an_overlay() -> anyhow::Result<()> {
    let database_url = crate::test_env::postgres_test_database_url("node status overlay");
    let mut conn = gobby_core::postgres::connect_readwrite(&database_url)?;
    let machine_id = gobby_core::machine::read_local_machine_id()?;
    let parent_dir = tempfile::tempdir()?;
    let overlay_dir = tempfile::tempdir()?;
    // The primary parent is fenced against its registered checkout, which is canonical.
    let parent_root = parent_dir
        .path()
        .canonicalize()?
        .to_string_lossy()
        .into_owned();
    let overlay_root = overlay_dir.path().to_string_lossy().into_owned();
    let name = overlay_dir
        .path()
        .file_name()
        .expect("temp dir has a name")
        .to_string_lossy()
        .into_owned();
    let parent_id = uuid_for(&format!("{parent_root}-status-overlay-parent"));
    let overlay_id = uuid_for(&format!("{overlay_root}-status-overlay"));
    let _parent_cleanup = ProjectCleanup {
        database_url: database_url.clone(),
        project_id: crate::db::id_param(&parent_id)?,
    };
    let _overlay_cleanup = ProjectCleanup {
        database_url: database_url.clone(),
        project_id: crate::db::id_param(&overlay_id)?,
    };
    crate::test_env::seed_test_checkout(&mut conn, &parent_id, parent_dir.path())
        .map_err(anyhow::Error::msg)?;
    api::upsert_project_stats(
        &mut conn,
        &machine_id,
        &stats_for(&parent_id, &parent_root, 2),
        IndexWriteMode::Primary,
        false,
    )?;
    api::upsert_project_stats(
        &mut conn,
        &machine_id,
        &stats_for(&overlay_id, &overlay_root, 1),
        IndexWriteMode::Overlay,
        false,
    )?;
    // The parent counts as indexed only while it holds file state on this machine.
    let machine = crate::db::id_param(&machine_id)?;
    let parent_uuid = crate::db::id_param(&parent_id)?;
    insert_file_version(&mut conn, &parent_uuid, "src/lib.rs", "hash-parent");
    insert_machine_state(
        &mut conn,
        &machine,
        &parent_uuid,
        &parent_root,
        "src/lib.rs",
        "hash-parent",
    );

    let mut ctx = local_context(&database_url, overlay_dir.path(), &overlay_id, &machine_id);
    ctx.index_scope = ProjectIndexScope::Overlay {
        overlay_project_id: overlay_id.clone(),
        overlay_root: overlay_dir.path().to_path_buf(),
        parent_project_id: parent_id.clone(),
        parent_root: std::path::PathBuf::from(&parent_root),
    };
    let report = collect(&mut conn, &ctx)?;

    let health = bm25_health::verify(&mut conn);
    // Zero tombstones are omitted, so the overlay block carries only its four identities.
    let health_json = serde_json::to_string(&serde_json::to_value(&health)?)?;
    let health_text = health.render_text();
    let expected_json = format!(
        r#"{{"code_index":{health_json},"id":"{overlay_id}","index_duration_ms":1500,"indexer_version":"1.9.70","overlay":{{"overlay_project_id":"{overlay_id}","overlay_root":"{overlay_root}","parent_project_id":"{parent_id}","parent_root":"{parent_root}"}},"root_path":"{overlay_root}","total_files":1,"total_symbols":3}}"#
    );
    assert_eq!(render_json(&report)?, expected_json);
    let expected_text = format!(
        "{name} ({overlay_short})\n  Root:     {overlay_root}\n  Files:    1\n  Symbols:  3\n  Indexed:  never\n  Duration: 1500ms\n  Overlay:  parent {parent_root} ({parent_short})\n{health_text}",
        overlay_short = &overlay_id[..8],
        parent_short = &parent_id[..8],
    );
    assert_eq!(render_text(&report), expected_text);
    Ok(())
}

fn assert_round_trip<T: Serialize + DeserializeOwned>(value: &T) -> anyhow::Result<()> {
    let encoded = serde_json::to_value(value)?;
    let decoded: T = serde_json::from_value(encoded.clone())?;
    assert_eq!(serde_json::to_value(&decoded)?, encoded);
    Ok(())
}

#[test]
fn wire_types_round_trip() -> anyhow::Result<()> {
    assert_eq!(NODE_OPERATION, "node_code_index");
    assert_eq!(NODE_ROUTE_PREFIX, "/api/code-index/node");
    assert_eq!(STATUS_ROUTE, "/api/code-index/node/status");
    assert_eq!(SEARCH_CONTENT_ROUTE, "/api/code-index/node/search-content");
    assert_eq!(INDEX_PLAN_ROUTE, "/api/code-index/node/index/plan");
    assert_eq!(INDEX_APPLY_ROUTE, "/api/code-index/node/index/apply");
    assert_eq!(
        ROUTE_PATHS,
        &[
            STATUS_ROUTE,
            SEARCH_CONTENT_ROUTE,
            INDEX_PLAN_ROUTE,
            INDEX_APPLY_ROUTE
        ]
    );

    // parent_root is absent, not null, outside an overlay.
    let plain = NodeScope {
        root: "/work/repo".to_string(),
        parent_root: None,
    };
    assert_eq!(serde_json::to_value(&plain)?, json!({"root": "/work/repo"}));
    let overlay = NodeScope {
        root: "/work/repo-wt".to_string(),
        parent_root: Some("/work/repo".to_string()),
    };
    assert_round_trip(&plain)?;
    assert_round_trip(&overlay)?;
    assert_round_trip(&StatusRequest {
        scope: overlay.clone(),
    })?;
    assert_round_trip(&SearchContentRequest {
        scope: overlay.clone(),
        query: "fn main".to_string(),
        language: Some("rust".to_string()),
        paths: vec!["src/**".to_string()],
        limit: 10,
        offset: 20,
    })?;
    assert_round_trip(&ContentCandidates {
        results: vec![ContentSearchHit {
            file_path: "src/lib.rs".to_string(),
            line_start: 1,
            line_end: 3,
            snippet: "fn main() {}".to_string(),
            language: Some("rust".to_string()),
        }],
        db_total: Some(2),
        hint: Some("narrow the query".to_string()),
    })?;

    let error = RouteError {
        code: "claims_mismatch".to_string(),
        error: "claims mismatch".to_string(),
        message: "the overlay shape disagrees with the principal".to_string(),
    };
    assert_round_trip(&error)?;
    assert_eq!(
        serde_json::to_value(&error)?,
        json!({
            "code": "claims_mismatch",
            "error": "claims mismatch",
            "message": "the overlay shape disagrees with the principal",
        })
    );

    // Both status shapes decode from their JSON and encode back unchanged.
    let code_index = json!({
        "healthy": true,
        "repair_command": "gobby postgres repair-code-index",
        "indexes": [],
    });
    let indexed: StatusReport = serde_json::from_value(json!({
        "code_index": code_index.clone(),
        "id": "p1",
        "index_duration_ms": 1500,
        "root_path": "/work/repo-wt",
        "total_files": 2,
        "total_symbols": 3,
        "overlay": {
            "overlay_project_id": "o1",
            "overlay_root": "/work/repo-wt",
            "parent_project_id": "p1",
            "parent_root": "/work/repo",
            "tombstones": 4,
        },
    }))?;
    assert_round_trip(&indexed)?;
    let unindexed: StatusReport = serde_json::from_value(json!({
        "code_index": code_index,
        "indexed": false,
        "project_id": "p1",
    }))?;
    assert_round_trip(&unindexed)?;
    Ok(())
}

/// A parent and an overlay on a foreign machine. Their roots do not exist on this disk.
struct OverlayPair {
    database_url: String,
    machine_id: String,
    parent_id: String,
    overlay_id: String,
    parent_root: String,
    overlay_root: String,
    _cleanup: (ProjectCleanup, ProjectCleanup),
}

fn seed_overlay_pair(
    conn: &mut postgres::Client,
    database_url: &str,
    label: &str,
    parent_files: usize,
    overlay_files: usize,
) -> anyhow::Result<OverlayPair> {
    let machine_id = uuid_for(&format!("{label}-machine"));
    crate::test_env::seed_test_machine(conn, &machine_id).map_err(anyhow::Error::msg)?;
    let parent_root = format!("/nonexistent/node-{label}-parent");
    let overlay_root = format!("/nonexistent/node-{label}-overlay");
    let parent_id = uuid_for(&format!("{label}-parent"));
    let overlay_id = uuid_for(&format!("{label}-overlay"));
    let cleanup = (
        ProjectCleanup {
            database_url: database_url.to_string(),
            project_id: crate::db::id_param(&parent_id)?,
        },
        ProjectCleanup {
            database_url: database_url.to_string(),
            project_id: crate::db::id_param(&overlay_id)?,
        },
    );
    api::upsert_project_stats(
        conn,
        &machine_id,
        &stats_for(&parent_id, &parent_root, parent_files),
        IndexWriteMode::Overlay,
        false,
    )?;
    api::upsert_project_stats(
        conn,
        &machine_id,
        &stats_for(&overlay_id, &overlay_root, overlay_files),
        IndexWriteMode::Overlay,
        false,
    )?;
    Ok(OverlayPair {
        database_url: database_url.to_string(),
        machine_id,
        parent_id,
        overlay_id,
        parent_root,
        overlay_root,
        _cleanup: cleanup,
    })
}

/// Writes one file's version, machine state and chunk for a project on this machine.
fn seed_file(
    conn: &mut postgres::Client,
    machine_id: &str,
    project_id: &str,
    root: &str,
    file_path: &str,
    content: &str,
) -> anyhow::Result<()> {
    let machine = crate::db::id_param(machine_id)?;
    let project = crate::db::id_param(project_id)?;
    let hash = format!("hash-{project_id}-{file_path}");
    insert_file_version(conn, &project, file_path, &hash);
    insert_machine_state(conn, &machine, &project, root, file_path, &hash);
    insert_chunk_version(conn, &project, file_path, content, &hash);
    Ok(())
}

fn overlay_hub(pair: &OverlayPair) -> HubScope {
    HubScope {
        database_url: pair.database_url.clone(),
        machine_id: pair.machine_id.clone(),
        project_id: pair.parent_id.clone(),
        overlay_project_id: Some(pair.overlay_id.clone()),
    }
}

fn overlay_node_scope(pair: &OverlayPair) -> NodeScope {
    NodeScope {
        root: pair.overlay_root.clone(),
        parent_root: Some(pair.parent_root.clone()),
    }
}

/// The local context the CLI builds for the overlay checkout.
fn overlay_local_context(pair: &OverlayPair) -> Context {
    let mut ctx = local_context(
        &pair.database_url,
        Path::new(&pair.overlay_root),
        &pair.overlay_id,
        &pair.machine_id,
    );
    ctx.index_scope = ProjectIndexScope::Overlay {
        overlay_project_id: pair.overlay_id.clone(),
        overlay_root: PathBuf::from(&pair.overlay_root),
        parent_project_id: pair.parent_id.clone(),
        parent_root: PathBuf::from(&pair.parent_root),
    };
    ctx
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires the PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
fn hub_overlay_status_reports_the_overlay_identity() -> anyhow::Result<()> {
    let database_url = crate::test_env::postgres_test_database_url("node hub overlay status");
    let mut conn = gobby_core::postgres::connect_readwrite(&database_url)?;
    let pair = seed_overlay_pair(&mut conn, &database_url, "hub-overlay-status", 2, 1)?;
    // The parent counts as indexed only while it holds file state on this machine.
    seed_file(
        &mut conn,
        &pair.machine_id,
        &pair.parent_id,
        &pair.parent_root,
        "src/parent.rs",
        "fn parentstatusprobe() {}",
    )?;

    let report = hub_status(
        &overlay_hub(&pair),
        StatusRequest {
            scope: overlay_node_scope(&pair),
        },
    )
    .map_err(route_error)?;
    let hub_json = render_json(&report)?;
    let local_json = render_json(&collect(&mut conn, &overlay_local_context(&pair))?)?;

    assert_eq!(hub_json, local_json);
    assert!(
        hub_json.contains(&format!(r#""id":"{}""#, pair.overlay_id)),
        "{hub_json}"
    );
    assert!(hub_json.contains(r#""total_files":1"#), "{hub_json}");
    Ok(())
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires the PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
fn hub_overlay_content_matches_the_local_candidates() -> anyhow::Result<()> {
    let database_url = crate::test_env::postgres_test_database_url("node hub overlay content");
    let mut conn = gobby_core::postgres::connect_readwrite(&database_url)?;
    let pair = seed_overlay_pair(&mut conn, &database_url, "hub-overlay-content", 1, 1)?;
    seed_file(
        &mut conn,
        &pair.machine_id,
        &pair.parent_id,
        &pair.parent_root,
        "src/parent.rs",
        "fn parentcontentprobe() {}",
    )?;
    seed_file(
        &mut conn,
        &pair.machine_id,
        &pair.overlay_id,
        &pair.overlay_root,
        "src/overlay.rs",
        "fn overlaycontentprobe() {}",
    )?;
    let local = overlay_local_context(&pair);
    let no_paths: Vec<String> = Vec::new();

    for query in ["overlaycontentprobe", "parentcontentprobe"] {
        let hub = hub_search_content(
            &overlay_hub(&pair),
            SearchContentRequest {
                scope: overlay_node_scope(&pair),
                query: query.to_string(),
                language: None,
                paths: Vec::new(),
                limit: 10,
                offset: 0,
            },
        )
        .map_err(route_error)?;
        let local_candidates = content_candidates(
            &mut conn,
            &local,
            query,
            ContentQuery {
                language: None,
                paths: &no_paths,
                limit: 10,
                offset: 0,
            },
        )?;
        let hub_files: Vec<&str> = hub.results.iter().map(|h| h.file_path.as_str()).collect();
        let local_files: Vec<&str> = local_candidates
            .results
            .iter()
            .map(|h| h.file_path.as_str())
            .collect();
        assert_eq!(hub_files, local_files, "query {query}");
        assert_eq!(hub.db_total, local_candidates.db_total, "query {query}");
    }

    // The overlay's own file is visible to the overlay checkout, so the hub must return it too.
    let overlay_hits = content_candidates(
        &mut conn,
        &local,
        "overlaycontentprobe",
        ContentQuery {
            language: None,
            paths: &no_paths,
            limit: 10,
            offset: 0,
        },
    )?;
    assert_eq!(
        overlay_hits
            .results
            .iter()
            .map(|h| h.file_path.as_str())
            .collect::<Vec<_>>(),
        vec!["src/overlay.rs"]
    );
    Ok(())
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires the PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
fn hub_overlay_search_refuses_a_missing_parent_index() -> anyhow::Result<()> {
    let database_url = crate::test_env::postgres_test_database_url("node hub overlay parent");
    let mut conn = gobby_core::postgres::connect_readwrite(&database_url)?;
    // The parent has no file state on this machine, so the parent index does not exist.
    let pair = seed_overlay_pair(&mut conn, &database_url, "hub-overlay-parent", 2, 1)?;

    let error = hub_search_content(
        &overlay_hub(&pair),
        SearchContentRequest {
            scope: overlay_node_scope(&pair),
            query: "anything".to_string(),
            language: None,
            paths: Vec::new(),
            limit: 10,
            offset: 0,
        },
    )
    .expect_err("an overlay whose parent has no index must be refused");
    assert_eq!(error.code, "parent_index_missing");
    Ok(())
}
