use super::*;
use crate::config::{CodeVectorSettings, Context, ProjectIndexScope};
use postgres::Client;
use postgres::types::ToSql;
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

fn unique_test_id(prefix: &str) -> String {
    fixture_uuid(&format!(
        "{prefix}-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system time after epoch")
            .as_nanos()
    ))
    .to_string()
}

/// Deterministic uuid for fixture ids derived from human-readable keys.
fn fixture_uuid(key: &str) -> uuid::Uuid {
    uuid::Uuid::new_v5(&crate::models::CODE_INDEX_UUID_NAMESPACE, key.as_bytes())
}

fn fixture_uuid_param(id: &str) -> uuid::Uuid {
    crate::db::id_param(id).expect("fixture id is a uuid")
}

#[test]
fn sanitize_pg_search_query_matches_gobby_rules() {
    assert_eq!(
        sanitize_pg_search_query("foo::bar baz-qux _id + \"drop\""),
        r#"foo\:\:bar baz-qux _id + "drop""#
    );
}

#[test]
fn sanitize_pg_search_query_escapes_leading_minus_per_token() {
    assert_eq!(
        sanitize_pg_search_query("-foo bar-baz -qux"),
        "\\-foo bar-baz \\-qux"
    );
    assert_eq!(sanitize_pg_search_query("foo-bar"), "foo-bar");
}

#[test]
fn sanitize_pg_search_query_preserves_dsl_punctuation() {
    assert_eq!(
        sanitize_pg_search_query(":: + compute (fence)"),
        r"\:\: + compute (fence)"
    );
    assert_eq!(
        sanitize_pg_search_query("_compute_fence_mask()"),
        r"_compute_fence_mask\(\)"
    );
    assert_eq!(
        sanitize_pg_search_query(r"_compute_fence_mask\(\)"),
        r"_compute_fence_mask\(\)"
    );
    assert_eq!(
        sanitize_pg_search_query(r#""_compute_fence_mask()""#),
        r#""_compute_fence_mask()""#
    );
    assert_eq!(
        sanitize_pg_search_query("compute (fence"),
        r"compute \(fence"
    );
    assert_eq!(sanitize_pg_search_query(r"\-foo -bar"), r"\-foo \-bar");
    assert_eq!(
        sanitize_pg_search_query("claude-opus-4-8[1m]"),
        r"claude-opus-4-8\[1m\]"
    );
    assert_eq!(
        sanitize_pg_search_query(r"claude-opus-4-8\[1m\]"),
        r"claude-opus-4-8\[1m\]"
    );
}

#[test]
fn sanitize_pg_search_query_neutralizes_boolean_operators_without_breaking_phrases() {
    assert_eq!(sanitize_pg_search_query("AND OR NOT"), "and or not");
    assert_eq!(
        sanitize_pg_search_query("salt AND pepper Or paprika nOt sugar"),
        "salt and pepper or paprika not sugar"
    );
    assert_eq!(
        sanitize_pg_search_query("(AND) OR-based _NOT_ CANDY ORACLE NOTICE"),
        "(and) or-based _NOT_ CANDY ORACLE NOTICE"
    );
    assert_eq!(
        sanitize_pg_search_query(r#""salt AND pepper" OR "NOT""#),
        r#""salt AND pepper" or "NOT""#
    );
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
    assert!(path_filter_requires_post_filter(&mixed));
    assert!(!path_filter_requires_post_filter(&paths));
}

#[test]
fn append_unique_symbols_respects_zero_limit() {
    let mut out = Vec::new();
    let mut seen = std::collections::HashSet::new();
    append_unique_symbols(
        &mut out,
        &mut seen,
        vec![crate::models::Symbol {
            id: "sym-1".to_string(),
            project_id: "project-1".to_string(),
            file_path: "src/lib.rs".to_string(),
            name: "run".to_string(),
            qualified_name: "run".to_string(),
            kind: "function".to_string(),
            language: "rust".to_string(),
            byte_start: 0,
            byte_end: 1,
            line_start: 1,
            line_end: 1,
            signature: None,
            docstring: None,
            parent_symbol_id: None,
            file_content_hash: "hash".to_string(),
            content_hash: "hash".to_string(),
            summary: None,
            created_at: String::new(),
            updated_at: String::new(),
        }],
        0,
    );

    assert!(out.is_empty());
    assert!(seen.is_empty());
}

#[test]
fn snippet_centers_first_matching_token() {
    let content = "before ".repeat(20) + "target call here";
    let snippet = make_snippet(&content, "target");

    assert!(snippet.contains("target call here"));
    assert!(snippet.len() <= 180);
}

#[test]
fn snippet_centers_earliest_matching_token_regardless_of_query_order() {
    let content = "early match ".to_string() + &"middle ".repeat(40) + "late match";
    let snippet = make_snippet(&content, "late early");

    assert!(snippet.contains("early match"));
    assert!(!snippet.contains("late match"));
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

mod serial_db {
    use super::*;

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn overlay_visibility_counts_and_kinds_use_database_predicates() {
        let (mut conn, database_url) = connect_overlay_visibility_test_db();

        let ids = OverlayFixtureIds::new(database_url);
        cleanup_overlay_visibility_fixture(&mut conn, &ids);
        let cleanup = OverlayFixtureCleanup {
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
            count_text_visible(&mut conn, "parentonly", &ctx, None, &[]).unwrap(),
            1
        );
        assert_eq!(
            count_text_visible(&mut conn, "marker", &ctx, None, &[]).unwrap(),
            3
        );
        assert_eq!(
            count_content_visible(&mut conn, "parentonly", &ctx, None, &[]).unwrap(),
            1
        );
        assert_eq!(
            count_content_visible(&mut conn, "marker", &ctx, None, &[]).unwrap(),
            3
        );

        cleanup
            .cleanup()
            .expect("cleanup overlay visibility fixture");
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn bm25_query_error_is_returned_instead_of_empty_results() {
        let (mut conn, _) = connect_overlay_visibility_test_db();
        conn.batch_execute("BEGIN").expect("begin transaction");
        assert!(conn.batch_execute("SELECT 1 / 0").is_err());

        let error = search_symbols_fts(
            &mut conn,
            "marker",
            &fixture_uuid("failed-bm25-query").to_string(),
            None,
            None,
            &[],
            10,
        )
        .expect_err("aborted transaction must fail the BM25 query");
        conn.batch_execute("ROLLBACK")
            .expect("rollback transaction");

        let message = error.to_string();
        assert!(message.contains("public.code_symbols_search_bm25"));
        assert!(message.contains("gcode status"));
        assert!(message.contains("gobby postgres repair-code-index"));
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn resolve_graph_symbol_by_id_resolves_exact_symbol() {
        let (mut conn, database_url) = connect_overlay_visibility_test_db();

        let project_id = unique_test_id("gcode-graph-symbol-by-id");
        cleanup_single_project(&mut conn, &project_id);
        insert_project(&mut conn, &project_id, "/tmp/gcode-graph-symbol-by-id");
        let _cleanup = SingleProjectCleanup {
            database_url,
            project_id: project_id.clone(),
        };
        insert_file(&mut conn, &project_id, "src/target.rs", "rust", 1);
        insert_symbol(
            &mut conn,
            &project_id,
            "src/target.rs",
            "target_symbol",
            "function",
        );
        let symbol_id =
            fixture_uuid(&format!("{project_id}:src/target.rs:target_symbol")).to_string();

        let resolved = resolve_graph_symbol_by_id(&mut conn, &symbol_id, &project_id)
            .expect("resolve symbol by id")
            .expect("symbol resolves");

        assert_eq!(resolved.id, symbol_id);
        assert_eq!(resolved.display_name, "target_symbol");
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn resolve_graph_symbol_by_id_returns_none_for_missing_uuid() {
        let (mut conn, _database_url) = connect_overlay_visibility_test_db();
        let project_id = unique_test_id("gcode-graph-symbol-missing");
        let missing_id = uuid::Uuid::new_v5(
            &crate::models::CODE_INDEX_UUID_NAMESPACE,
            project_id.as_bytes(),
        )
        .to_string();

        let resolved = resolve_graph_symbol_by_id(&mut conn, &missing_id, &project_id)
            .expect("resolve missing symbol id");

        assert!(resolved.is_none());
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn resolve_graph_symbol_by_id_returns_none_for_empty_id() {
        let (mut conn, _database_url) = connect_overlay_visibility_test_db();

        let resolved = resolve_graph_symbol_by_id(&mut conn, "", "gcode-empty-symbol-id")
            .expect("resolve empty symbol id");

        assert!(resolved.is_none());
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn resolve_graph_symbol_by_id_returns_none_for_malformed_id() {
        let (mut conn, _database_url) = connect_overlay_visibility_test_db();

        let resolved =
            resolve_graph_symbol_by_id(&mut conn, "not-a-symbol-id", "gcode-malformed-id")
                .expect("resolve malformed symbol id");

        assert!(resolved.is_none());
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn two_machine_divergence_scopes_graph_resolution_to_local_state() {
        let (mut conn, database_url) = connect_overlay_visibility_test_db();
        let project_id = unique_test_id("two-machine-resolve");
        cleanup_single_project(&mut conn, &project_id);
        let _cleanup = SingleProjectCleanup {
            database_url,
            project_id: project_id.clone(),
        };

        insert_project(&mut conn, &project_id, "/tmp/gcode-two-machine-resolve");
        let local_machine =
            gobby_core::machine::read_local_machine_id().expect("read local machine id");
        let foreign_machine = fixture_uuid(&format!("{project_id}:foreign-machine")).to_string();

        insert_file_version(&mut conn, &project_id, "src/lib.rs", "rust", "hash-local");
        insert_file_version(&mut conn, &project_id, "src/lib.rs", "rust", "hash-foreign");
        insert_machine_file_state(
            &mut conn,
            &local_machine,
            &project_id,
            "/tmp/gcode-two-machine-resolve",
            "src/lib.rs",
            "hash-local",
        );
        insert_machine_file_state(
            &mut conn,
            &foreign_machine,
            &project_id,
            "/tmp/gcode-two-machine-resolve",
            "src/lib.rs",
            "hash-foreign",
        );

        // The same name exists in both content versions with per-version ids.
        insert_symbol_version(
            &mut conn,
            &project_id,
            "src/lib.rs",
            "twomachine_resolver_target",
            "fn",
            "hash-local",
        );
        insert_symbol_version(
            &mut conn,
            &project_id,
            "src/lib.rs",
            "twomachine_resolver_target",
            "fn",
            "hash-foreign",
        );
        // This name only exists in the foreign machine's version.
        insert_symbol_version(
            &mut conn,
            &project_id,
            "src/lib.rs",
            "twomachine_foreign_resolver",
            "fn",
            "hash-foreign",
        );

        let (resolved, suggestions) =
            resolve_graph_symbol(&mut conn, "twomachine_resolver_target", &project_id)
                .expect("resolve divergent name");
        assert!(
            suggestions.is_empty(),
            "unexpected ambiguity: {suggestions:?}"
        );
        let resolved = resolved.expect("divergent name resolves to the local version");
        let local_id = fixture_uuid(&format!(
            "{project_id}:src/lib.rs:twomachine_resolver_target:hash-local"
        ))
        .to_string();
        assert_eq!(resolved.id, local_id);

        let (resolved, suggestions) =
            resolve_graph_symbol(&mut conn, "twomachine_foreign_resolver", &project_id)
                .expect("resolve foreign-only name");
        assert!(resolved.is_none(), "foreign-only name must not resolve");
        assert!(
            suggestions.is_empty(),
            "foreign-only name must not suggest: {suggestions:?}"
        );

        let foreign_id = fixture_uuid(&format!(
            "{project_id}:src/lib.rs:twomachine_resolver_target:hash-foreign"
        ))
        .to_string();
        let resolved = resolve_graph_symbol_by_id(&mut conn, &foreign_id, &project_id)
            .expect("resolve foreign version id");
        assert!(resolved.is_none(), "foreign-version id must be invisible");

        assert_eq!(
            crate::visibility::local_active_content_hash(&mut conn, &project_id, "src/lib.rs")
                .expect("local active hash"),
            Some("hash-local".to_string())
        );
        // A path indexed only by the foreign machine has no local active hash.
        insert_file_version(&mut conn, &project_id, "src/ghost.rs", "rust", "hash-ghost");
        insert_machine_file_state(
            &mut conn,
            &foreign_machine,
            &project_id,
            "/tmp/gcode-two-machine-resolve",
            "src/ghost.rs",
            "hash-ghost",
        );
        assert_eq!(
            crate::visibility::local_active_content_hash(&mut conn, &project_id, "src/ghost.rs")
                .expect("foreign-only hash"),
            None
        );
    }
}

fn connect_overlay_visibility_test_db() -> (Client, String) {
    let database_url = crate::test_env::postgres_test_database_url("FTS PostgreSQL tests");
    let mut conn = gobby_core::postgres::connect_readwrite(&database_url)
        .expect("connect FTS PostgreSQL test database");
    crate::schema::validate_runtime_schema(&mut conn).expect("FTS PostgreSQL test schema is valid");
    (conn, database_url)
}

struct OverlayFixtureIds {
    database_url: String,
    parent_project_id: String,
    overlay_project_id: String,
}

impl OverlayFixtureIds {
    fn new(database_url: String) -> Self {
        let suffix = unique_test_id("gcode-overlay-test");
        Self {
            database_url,
            parent_project_id: fixture_uuid(&format!("{suffix}-parent")).to_string(),
            overlay_project_id: fixture_uuid(&format!("{suffix}-overlay")).to_string(),
        }
    }
}

struct OverlayFixtureCleanup {
    database_url: String,
    parent_project_id: String,
    overlay_project_id: String,
}

impl OverlayFixtureCleanup {
    fn cleanup(&self) -> anyhow::Result<()> {
        let mut conn = gobby_core::postgres::connect_readwrite(&self.database_url)?;
        cleanup_overlay_visibility_projects(
            &mut conn,
            &self.parent_project_id,
            &self.overlay_project_id,
        )
    }
}

impl Drop for OverlayFixtureCleanup {
    fn drop(&mut self) {
        let _ = self.cleanup();
    }
}

struct SingleProjectCleanup {
    database_url: String,
    project_id: String,
}

impl Drop for SingleProjectCleanup {
    fn drop(&mut self) {
        if let Ok(mut conn) = gobby_core::postgres::connect_readwrite(&self.database_url) {
            cleanup_single_project(&mut conn, &self.project_id);
        }
    }
}

fn cleanup_overlay_visibility_fixture(conn: &mut Client, ids: &OverlayFixtureIds) {
    let _ =
        cleanup_overlay_visibility_projects(conn, &ids.parent_project_id, &ids.overlay_project_id);
}

fn cleanup_single_project(conn: &mut Client, project_id: &str) {
    let _ = cleanup_overlay_visibility_projects(conn, project_id, project_id);
}

fn cleanup_overlay_visibility_projects(
    conn: &mut Client,
    parent_project_id: &str,
    overlay_project_id: &str,
) -> anyhow::Result<()> {
    let parent_project_id = crate::db::id_param(parent_project_id)?;
    let overlay_project_id = crate::db::id_param(overlay_project_id)?;
    let mut tx = conn.transaction()?;
    tx.execute(
        "DELETE FROM code_indexed_file_states WHERE project_id = $1 OR project_id = $2",
        &[&parent_project_id, &overlay_project_id],
    )?;
    tx.execute(
        "DELETE FROM code_indexed_project_states WHERE project_id = $1 OR project_id = $2",
        &[&parent_project_id, &overlay_project_id],
    )?;
    for table in OVERLAY_VISIBILITY_CHILD_TABLES {
        let sql = format!("DELETE FROM {table} WHERE project_id = $1 OR project_id = $2");
        tx.execute(&sql, &[&parent_project_id, &overlay_project_id])?;
    }
    let sql = format!("DELETE FROM {OVERLAY_VISIBILITY_PROJECT_TABLE} WHERE id = $1 OR id = $2");
    tx.execute(&sql, &[&parent_project_id, &overlay_project_id])?;
    tx.commit()?;
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
    let project_id = fixture_uuid_param(project_id);
    let machine_id = fixture_uuid_param(
        &gobby_core::machine::read_local_machine_id().expect("read local machine id"),
    );
    conn.execute(
        "INSERT INTO code_indexed_projects (id) VALUES ($1)",
        &[&project_id],
    )
    .expect("insert project");
    conn.execute(
        "INSERT INTO code_indexed_project_states
                (machine_id, project_id, root_path, total_files, total_symbols,
                 last_indexed_at, index_duration_ms)
             VALUES ($1, $2, $3, 0, 0, NOW(), 0)",
        &[&machine_id, &project_id, &root_path],
    )
    .expect("insert project state");
}

fn insert_file(
    conn: &mut Client,
    project_id: &str,
    file_path: &str,
    language: &str,
    symbol_count: i32,
) {
    let id = fixture_uuid(&format!("{project_id}:{file_path}"));
    let project_id = fixture_uuid_param(project_id);
    let params: &[&(dyn ToSql + Sync)] = &[&id, &project_id, &file_path, &language, &symbol_count];
    conn.execute(
        "INSERT INTO code_indexed_files
                (id, project_id, file_path, language, content_hash, symbol_count, byte_size,
                 graph_synced, vectors_synced, graph_sync_attempted_at, indexed_at)
             VALUES ($1, $2, $3, $4, 'hash', $5, 1, false, false, NULL, NOW())",
        params,
    )
    .expect("insert indexed file");
    let machine_id = fixture_uuid_param(
        &gobby_core::machine::read_local_machine_id().expect("read local machine id"),
    );
    conn.execute(
        "INSERT INTO code_indexed_file_states
                (machine_id, project_id, file_path, content_hash)
             VALUES ($1, $2, $3, 'hash')",
        &[&machine_id, &project_id, &file_path],
    )
    .expect("insert indexed file state");
}

fn insert_symbol(conn: &mut Client, project_id: &str, file_path: &str, name: &str, kind: &str) {
    let id = fixture_uuid(&format!("{project_id}:{file_path}:{name}"));
    let project_id = fixture_uuid_param(project_id);
    let summary = name.replace('_', " ").replace('+', " plus ");
    let params: &[&(dyn ToSql + Sync)] = &[&id, &project_id, &file_path, &name, &kind, &summary];
    conn.execute(
        "INSERT INTO code_symbols
                (id, project_id, file_path, name, qualified_name, kind, language, byte_start,
                 byte_end, line_start, line_end, signature, docstring, parent_symbol_id,
                 file_content_hash, content_hash, summary, created_at, updated_at)
             VALUES ($1, $2, $3, $4, $4, $5, 'rust', 0, 1, 1, 1, $4, NULL, NULL,
                     'hash', 'hash', $6, NOW(), NOW())",
        params,
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
    let id = fixture_uuid(&format!("{project_id}:{file_path}:{chunk_index}"));
    let project_id = fixture_uuid_param(project_id);
    let params: &[&(dyn ToSql + Sync)] = &[&id, &project_id, &file_path, &chunk_index, &content];
    conn.execute(
        "INSERT INTO code_content_chunks
                (id, project_id, file_path, content_hash, chunk_index, line_start, line_end,
                 content, language, created_at)
             VALUES ($1, $2, $3, 'hash', $4, 1, 1, $5, 'rust', NOW())",
        params,
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
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: ProjectIndexScope::Overlay {
            overlay_project_id: ids.overlay_project_id.clone(),
            overlay_root: PathBuf::from("/tmp/gcode-overlay"),
            parent_project_id: ids.parent_project_id.clone(),
            parent_root: PathBuf::from("/tmp/gcode-overlay-parent"),
        },
    }
}

fn single_project_context(database_url: &str, project_id: &str) -> Context {
    Context {
        database_url: database_url.to_string(),
        project_root: PathBuf::from("/tmp/gcode-two-machine"),
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

fn insert_file_version(
    conn: &mut Client,
    project_id: &str,
    file_path: &str,
    language: &str,
    content_hash: &str,
) {
    let id = fixture_uuid(&format!("{project_id}:{file_path}:{content_hash}"));
    let project_id = fixture_uuid_param(project_id);
    let params: &[&(dyn ToSql + Sync)] = &[&id, &project_id, &file_path, &language, &content_hash];
    conn.execute(
        "INSERT INTO code_indexed_files
                (id, project_id, file_path, language, content_hash, symbol_count, byte_size,
                 graph_synced, vectors_synced, graph_sync_attempted_at, indexed_at)
             VALUES ($1, $2, $3, $4, $5, 1, 1, false, false, NULL, NOW())",
        params,
    )
    .expect("insert indexed file version");
}

fn insert_machine_file_state(
    conn: &mut Client,
    machine_id: &str,
    project_id: &str,
    root_path: &str,
    file_path: &str,
    content_hash: &str,
) {
    let machine_id = fixture_uuid_param(machine_id);
    let project_id = fixture_uuid_param(project_id);
    conn.execute(
        "INSERT INTO code_indexed_project_states
                (machine_id, project_id, root_path, total_files, total_symbols,
                 last_indexed_at, index_duration_ms)
             VALUES ($1, $2, $3, 0, 0, NOW(), 0)
             ON CONFLICT (machine_id, project_id) DO NOTHING",
        &[&machine_id, &project_id, &root_path],
    )
    .expect("insert machine project state");
    conn.execute(
        "INSERT INTO code_indexed_file_states
                (machine_id, project_id, file_path, content_hash)
             VALUES ($1, $2, $3, $4)",
        &[&machine_id, &project_id, &file_path, &content_hash],
    )
    .expect("insert machine file state");
}

fn insert_symbol_version(
    conn: &mut Client,
    project_id: &str,
    file_path: &str,
    name: &str,
    kind: &str,
    content_hash: &str,
) {
    let id = fixture_uuid(&format!("{project_id}:{file_path}:{name}:{content_hash}"));
    let project_id = fixture_uuid_param(project_id);
    let summary = name.replace('_', " ").replace('+', " plus ");
    let params: &[&(dyn ToSql + Sync)] = &[
        &id,
        &project_id,
        &file_path,
        &name,
        &kind,
        &content_hash,
        &summary,
    ];
    conn.execute(
        "INSERT INTO code_symbols
                (id, project_id, file_path, name, qualified_name, kind, language, byte_start,
                 byte_end, line_start, line_end, signature, docstring, parent_symbol_id,
                 file_content_hash, content_hash, summary, created_at, updated_at)
             VALUES ($1, $2, $3, $4, $4, $5, 'rust', 0, 1, 1, 1, $4, NULL, NULL,
                     $6, $6, $7, NOW(), NOW())",
        params,
    )
    .expect("insert symbol version");
}

fn insert_chunk_version(
    conn: &mut Client,
    project_id: &str,
    file_path: &str,
    chunk_index: i32,
    content: &str,
    content_hash: &str,
) {
    let id = fixture_uuid(&format!(
        "{project_id}:{file_path}:{chunk_index}:{content_hash}"
    ));
    let project_id = fixture_uuid_param(project_id);
    let params: &[&(dyn ToSql + Sync)] = &[
        &id,
        &project_id,
        &file_path,
        &chunk_index,
        &content,
        &content_hash,
    ];
    conn.execute(
        "INSERT INTO code_content_chunks
                (id, project_id, file_path, content_hash, chunk_index, line_start, line_end,
                 content, language, created_at)
             VALUES ($1, $2, $3, $6, $4, 1, 1, $5, 'rust', NOW())",
        params,
    )
    .expect("insert content chunk version");
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn two_machine_divergence_scopes_reads_to_local_file_state() {
    let (mut conn, database_url) = connect_overlay_visibility_test_db();
    let project_id = unique_test_id("two-machine-project");
    cleanup_single_project(&mut conn, &project_id);
    let _cleanup = SingleProjectCleanup {
        database_url: database_url.clone(),
        project_id: project_id.clone(),
    };

    insert_project(&mut conn, &project_id, "/tmp/gcode-two-machine");
    let local_machine =
        gobby_core::machine::read_local_machine_id().expect("read local machine id");
    let foreign_machine = fixture_uuid(&format!("{project_id}:foreign-machine")).to_string();

    // One shared path with divergent versions: the local machine selects
    // hash-local, the foreign machine selects hash-foreign.
    insert_file_version(&mut conn, &project_id, "src/lib.rs", "rust", "hash-local");
    insert_file_version(&mut conn, &project_id, "src/lib.rs", "rust", "hash-foreign");
    insert_machine_file_state(
        &mut conn,
        &local_machine,
        &project_id,
        "/tmp/gcode-two-machine",
        "src/lib.rs",
        "hash-local",
    );
    insert_machine_file_state(
        &mut conn,
        &foreign_machine,
        &project_id,
        "/tmp/gcode-two-machine",
        "src/lib.rs",
        "hash-foreign",
    );
    insert_symbol_version(
        &mut conn,
        &project_id,
        "src/lib.rs",
        "twomachine_local_marker++",
        "fn",
        "hash-local",
    );
    insert_symbol_version(
        &mut conn,
        &project_id,
        "src/lib.rs",
        "twomachine_foreign_marker++",
        "fn",
        "hash-foreign",
    );
    insert_chunk_version(
        &mut conn,
        &project_id,
        "src/lib.rs",
        0,
        "twomachine chunk localonly++",
        "hash-local",
    );
    insert_chunk_version(
        &mut conn,
        &project_id,
        "src/lib.rs",
        0,
        "twomachine chunk foreignonly++",
        "hash-foreign",
    );

    // A path indexed only by the foreign machine must be invisible here.
    insert_file_version(&mut conn, &project_id, "src/ghost.rs", "rust", "hash-ghost");
    insert_machine_file_state(
        &mut conn,
        &foreign_machine,
        &project_id,
        "/tmp/gcode-two-machine",
        "src/ghost.rs",
        "hash-ghost",
    );
    insert_symbol_version(
        &mut conn,
        &project_id,
        "src/ghost.rs",
        "twomachine_ghost_marker++",
        "fn",
        "hash-ghost",
    );
    insert_chunk_version(
        &mut conn,
        &project_id,
        "src/ghost.rs",
        0,
        "twomachine chunk ghostonly++",
        "hash-ghost",
    );

    let ctx = single_project_context(&database_url, &project_id);

    let text = super::symbols::search_text_visible(&mut conn, "twomachine", &ctx, None, &[], 10)
        .expect("visible text search");
    let names: Vec<&str> = text.iter().map(|r| r.name.as_str()).collect();
    assert_eq!(names, vec!["twomachine_local_marker++"]);
    assert_eq!(
        count_text_visible(&mut conn, "twomachine", &ctx, None, &[]).expect("count text"),
        1
    );

    let content =
        super::content::search_content_visible(&mut conn, "twomachine", &ctx, None, &[], 10)
            .expect("visible content search");
    assert_eq!(content.len(), 1);
    assert!(
        content[0].snippet.contains("localonly"),
        "expected the local version snippet, got: {}",
        content[0].snippet
    );
    assert_eq!(
        count_content_visible(&mut conn, "twomachine", &ctx, None, &[]).expect("count content"),
        1
    );
}
