use super::*;
use crate::config::{CodeVectorSettings, ProjectIndexScope};
use postgres::NoTls;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

const OVERLAY_VISIBILITY_CHILD_TABLES: &[&str] = &[
    "code_calls",
    "code_imports",
    "code_symbols",
    "code_content_chunks",
    "code_indexed_files",
];
const OVERLAY_VISIBILITY_PROJECT_TABLE: &str = "code_indexed_projects";

#[test]
fn sanitize_pg_search_query_matches_gobby_rules() {
    assert_eq!(
        sanitize_pg_search_query("foo::bar baz-qux _id + \"drop\""),
        "foo bar baz-qux _id drop"
    );
}

#[test]
fn sanitize_pg_search_query_drops_empty_queries() {
    assert_eq!(sanitize_pg_search_query(":: + ()"), "");
}

#[test]
fn glob_to_like_prefix_escapes_like_wildcards() {
    assert_eq!(
        glob_to_like_prefix("src/foo_bar/*.rs").as_deref(),
        Some("src/foo\\_bar/%")
    );
}

#[test]
fn expand_paths_trims_skips_empty_and_expands_bare_paths() {
    let paths = vec![
        " src/gobby ".to_string(),
        "".to_string(),
        "crates/**/*.rs".to_string(),
        "src/gobby/".to_string(),
    ];

    assert_eq!(
        expand_paths(&paths),
        vec!["src/gobby", "src/gobby/**", "crates/**/*.rs"]
    );
}

#[test]
fn compile_patterns_reports_invalid_glob() {
    let err = compile_patterns(&["src/[".to_string()])
        .expect_err("invalid glob should fail")
        .to_string();

    assert!(err.contains("invalid path glob `src/[`"));
}

#[test]
fn path_like_prefixes_escape_and_require_all_patterns() {
    let paths = vec![
        "src/foo_bar".to_string(),
        "src/foo_bar/**".to_string(),
        "src/100%/**".to_string(),
    ];
    assert_eq!(
        path_like_prefixes(&paths).expect("prefixes"),
        vec!["src/foo\\_bar%", "src/foo\\_bar/%", "src/100\\%/%"]
    );

    let mixed = vec!["src/**".to_string(), "*.rs".to_string()];
    assert!(path_like_prefixes(&mixed).is_none());
    assert!(path_filter_falls_back(&mixed));
    assert!(!path_filter_falls_back(&paths));
}

#[test]
fn snippet_centers_first_matching_token() {
    let content = "before ".repeat(20) + "target call here";
    let snippet = make_snippet(&content, "target");

    assert!(snippet.contains("target call here"));
    assert!(snippet.len() <= 180);
}

#[test]
fn snippet_handles_unicode_before_match() {
    let content = "é".repeat(80) + " target call here";
    let snippet = make_snippet(&content, "target");

    assert!(snippet.contains("target call here"));
    assert!(snippet.chars().count() <= 180);

    let content = "\u{0130}".repeat(80) + " target call here";
    let snippet = make_snippet(&content, "target");

    assert!(snippet.contains("target call here"));
    assert!(snippet.chars().count() <= 180);
}

#[test]
fn overlay_visibility_counts_and_kinds_use_database_predicates() {
    let Some(mut conn) = connect_overlay_visibility_test_db() else {
        return;
    };

    let ids = OverlayFixtureIds::new();
    cleanup_overlay_visibility_fixture(&mut conn, &ids);
    let _cleanup = OverlayFixtureCleanup {
        database_url: ids.database_url.clone(),
        parent_project_id: ids.parent_project_id.clone(),
        overlay_project_id: ids.overlay_project_id.clone(),
    };

    seed_overlay_visibility_fixture(&mut conn, &ids);
    let ctx = overlay_visibility_context(&ids);

    assert_eq!(
        crate::visibility::visible_kinds(&mut conn, &ctx).expect("visible kinds"),
        vec!["overlay_kind", "overlay_shadow_kind", "parent_kind"]
    );
    assert_eq!(
        count_text_visible(&mut conn, "parentonly", &ctx, None, &[]),
        1
    );
    assert_eq!(count_text_visible(&mut conn, "++", &ctx, None, &[]), 3);
    assert_eq!(
        count_content_visible(&mut conn, "parentonly", &ctx, None, &[]),
        1
    );
    assert_eq!(count_content_visible(&mut conn, "++", &ctx, None, &[]), 3);
}

fn connect_overlay_visibility_test_db() -> Option<Client> {
    let explicit_url = std::env::var("GCODE_POSTGRES_TEST_DATABASE_URL").ok();
    let database_url = explicit_url
        .clone()
        .or_else(|| crate::db::resolve_database_url().ok())?;
    match Client::connect(&database_url, NoTls) {
        Ok(mut conn) => {
            if let Err(err) = crate::schema::validate_runtime_schema(&mut conn) {
                if explicit_url.is_some() {
                    panic!("test PostgreSQL hub schema is invalid: {err}");
                }
                return None;
            }
            Some(conn)
        }
        Err(err) => {
            if explicit_url.is_some() {
                panic!("failed to connect test PostgreSQL hub: {err}");
            }
            None
        }
    }
}

struct OverlayFixtureIds {
    database_url: String,
    parent_project_id: String,
    overlay_project_id: String,
}

impl OverlayFixtureIds {
    fn new() -> Self {
        let database_url = std::env::var("GCODE_POSTGRES_TEST_DATABASE_URL")
            .ok()
            .or_else(|| crate::db::resolve_database_url().ok())
            .expect("database URL already resolved");
        let suffix = format!(
            "{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("system time after epoch")
                .as_nanos()
        );
        Self {
            database_url,
            parent_project_id: format!("gcode-overlay-test-parent-{suffix}"),
            overlay_project_id: format!("gcode-overlay-test-overlay-{suffix}"),
        }
    }
}

struct OverlayFixtureCleanup {
    database_url: String,
    parent_project_id: String,
    overlay_project_id: String,
}

impl Drop for OverlayFixtureCleanup {
    fn drop(&mut self) {
        let mut conn = match Client::connect(&self.database_url, NoTls) {
            Ok(conn) => conn,
            Err(err) => {
                eprintln!(
                    "failed to connect to cleanup overlay visibility fixture at {}: {err}",
                    self.database_url
                );
                return;
            }
        };
        if let Err(err) = cleanup_overlay_visibility_projects(
            &mut conn,
            &self.parent_project_id,
            &self.overlay_project_id,
        ) {
            eprintln!(
                "failed to cleanup overlay visibility fixture at {}: {err}",
                self.database_url
            );
        }
    }
}

fn cleanup_overlay_visibility_fixture(conn: &mut Client, ids: &OverlayFixtureIds) {
    let _ =
        cleanup_overlay_visibility_projects(conn, &ids.parent_project_id, &ids.overlay_project_id);
}

fn cleanup_overlay_visibility_projects(
    conn: &mut Client,
    parent_project_id: &str,
    overlay_project_id: &str,
) -> Result<(), postgres::Error> {
    for table in OVERLAY_VISIBILITY_CHILD_TABLES {
        let sql = format!("DELETE FROM {table} WHERE project_id = $1 OR project_id = $2");
        conn.execute(&sql, &[&parent_project_id, &overlay_project_id])?;
    }
    let sql = format!("DELETE FROM {OVERLAY_VISIBILITY_PROJECT_TABLE} WHERE id = $1 OR id = $2");
    conn.execute(&sql, &[&parent_project_id, &overlay_project_id])?;
    Ok(())
}

fn seed_overlay_visibility_fixture(conn: &mut Client, ids: &OverlayFixtureIds) {
    insert_project(conn, &ids.parent_project_id, "/tmp/gcode-overlay-parent");
    insert_project(conn, &ids.overlay_project_id, "/tmp/gcode-overlay");

    insert_file(conn, &ids.parent_project_id, "src/parent.rs", "rust", 1);
    insert_file(conn, &ids.parent_project_id, "src/shadowed.rs", "rust", 1);
    insert_file(conn, &ids.parent_project_id, "src/deleted.rs", "rust", 1);
    insert_file(conn, &ids.overlay_project_id, "src/overlay.rs", "rust", 1);
    insert_file(conn, &ids.overlay_project_id, "src/shadowed.rs", "rust", 1);
    insert_file(
        conn,
        &ids.overlay_project_id,
        "src/deleted.rs",
        crate::visibility::TOMBSTONE_LANGUAGE,
        0,
    );

    insert_symbol(
        conn,
        &ids.parent_project_id,
        "src/parent.rs",
        "parentonly_marker_visible++",
        "parent_kind",
    );
    insert_symbol(
        conn,
        &ids.parent_project_id,
        "src/shadowed.rs",
        "parentonly_marker_shadowed++",
        "parent_shadow_kind",
    );
    insert_symbol(
        conn,
        &ids.parent_project_id,
        "src/deleted.rs",
        "parentonly_marker_deleted++",
        "parent_deleted_kind",
    );
    insert_symbol(
        conn,
        &ids.overlay_project_id,
        "src/overlay.rs",
        "overlay_marker_visible++",
        "overlay_kind",
    );
    insert_symbol(
        conn,
        &ids.overlay_project_id,
        "src/shadowed.rs",
        "overlay_marker_shadowed++",
        "overlay_shadow_kind",
    );

    insert_chunk(
        conn,
        &ids.parent_project_id,
        "src/parent.rs",
        0,
        "marker parentonly visible++",
    );
    insert_chunk(
        conn,
        &ids.parent_project_id,
        "src/shadowed.rs",
        0,
        "marker parentonly shadowed++",
    );
    insert_chunk(
        conn,
        &ids.parent_project_id,
        "src/deleted.rs",
        0,
        "marker parentonly deleted++",
    );
    insert_chunk(
        conn,
        &ids.overlay_project_id,
        "src/overlay.rs",
        0,
        "marker overlay visible++",
    );
    insert_chunk(
        conn,
        &ids.overlay_project_id,
        "src/shadowed.rs",
        0,
        "marker overlay shadowed++",
    );
}

fn insert_project(conn: &mut Client, project_id: &str, root_path: &str) {
    conn.execute(
        "INSERT INTO code_indexed_projects
                (id, root_path, total_files, total_symbols, last_indexed_at, index_duration_ms)
             VALUES ($1, $2, 0, 0, NOW(), 0)",
        &[&project_id, &root_path],
    )
    .expect("insert project");
}

fn insert_file(
    conn: &mut Client,
    project_id: &str,
    file_path: &str,
    language: &str,
    symbol_count: i32,
) {
    let id = format!("{project_id}:{file_path}");
    conn.execute(
        "INSERT INTO code_indexed_files
                (id, project_id, file_path, language, content_hash, symbol_count, byte_size,
                 graph_synced, vectors_synced, graph_sync_attempted_at, indexed_at)
             VALUES ($1, $2, $3, $4, 'hash', $5, 1, false, false, NULL, NOW())",
        &[&id, &project_id, &file_path, &language, &symbol_count],
    )
    .expect("insert indexed file");
}

fn insert_symbol(conn: &mut Client, project_id: &str, file_path: &str, name: &str, kind: &str) {
    let id = format!("{project_id}:{file_path}:{name}");
    conn.execute(
        "INSERT INTO code_symbols
                (id, project_id, file_path, name, qualified_name, kind, language, byte_start,
                 byte_end, line_start, line_end, signature, docstring, parent_symbol_id,
                 content_hash, summary, created_at, updated_at)
             VALUES ($1, $2, $3, $4, $4, $5, 'rust', 0, 1, 1, 1, $4, NULL, NULL,
                     'hash', NULL, NOW(), NOW())",
        &[&id, &project_id, &file_path, &name, &kind],
    )
    .expect("insert symbol");
}

fn insert_chunk(
    conn: &mut Client,
    project_id: &str,
    file_path: &str,
    chunk_index: i32,
    content: &str,
) {
    let id = format!("{project_id}:{file_path}:{chunk_index}");
    conn.execute(
        "INSERT INTO code_content_chunks
                (id, project_id, file_path, chunk_index, line_start, line_end, content, language,
                 created_at)
             VALUES ($1, $2, $3, $4, 1, 1, $5, 'rust', NOW())",
        &[&id, &project_id, &file_path, &chunk_index, &content],
    )
    .expect("insert content chunk");
}

fn overlay_visibility_context(ids: &OverlayFixtureIds) -> Context {
    Context {
        database_url: ids.database_url.clone(),
        project_root: PathBuf::from("/tmp/gcode-overlay"),
        project_id: ids.overlay_project_id.clone(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: CodeVectorSettings::default(),
        daemon_url: None,
        index_scope: ProjectIndexScope::Overlay {
            overlay_project_id: ids.overlay_project_id.clone(),
            overlay_root: PathBuf::from("/tmp/gcode-overlay"),
            parent_project_id: ids.parent_project_id.clone(),
            parent_root: PathBuf::from("/tmp/gcode-overlay-parent"),
        },
    }
}
