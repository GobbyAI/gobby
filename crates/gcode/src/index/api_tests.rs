use std::time::{SystemTime, UNIX_EPOCH};

use crate::db;
use crate::models::{
    CODE_INDEX_UUID_NAMESPACE, CallRelation, CallTargetKind, HeritageKind, ImportRelation,
    IndexedFile, IndexedProject, InheritanceRelation, Symbol,
};

use super::api;

mod serial_db {
    use super::*;

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn api_upsert_symbols_preserves_same_hash_summary_and_clears_changed_hash() {
        let (mut conn, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-api-symbol-upsert");
        cleanup_project(&mut conn, &project_id).expect("pre-clean test project rows");
        let _cleanup = ProjectCleanup {
            database_url,
            project_id: project_id.clone(),
        };
        seed_project(&mut conn, &project_id);

        let rel = "src/lib.rs";
        api::upsert_file(
            &mut conn,
            &indexed_file(&project_id, rel, "file-hash", 1, 16),
        )
        .expect("seed indexed file");
        let mut symbol = test_symbol(
            &project_id,
            rel,
            "file-hash",
            "tracked",
            0,
            "content-hash-v1",
        );
        symbol.summary = Some("daemon summary".to_string());
        assert_eq!(
            api::upsert_symbols(&mut conn, &[symbol.clone()]).expect("insert symbol"),
            1
        );
        assert_eq!(
            symbol_summary(&mut conn, &symbol.id),
            Some("daemon summary".to_string())
        );

        let mut same_hash_update = symbol.clone();
        same_hash_update.signature = Some("fn tracked(value: i32)".to_string());
        same_hash_update.summary = Some("incoming replacement summary".to_string());
        assert_eq!(
            api::upsert_symbols(&mut conn, &[same_hash_update]).expect("same-hash upsert"),
            1
        );
        assert_eq!(
            symbol_summary(&mut conn, &symbol.id),
            Some("daemon summary".to_string()),
            "same-hash upserts must preserve existing summaries"
        );

        let mut changed_hash_update = symbol.clone();
        changed_hash_update.content_hash = "content-hash-v2".to_string();
        changed_hash_update.summary = Some("incoming stale summary".to_string());
        assert_eq!(
            api::upsert_symbols(&mut conn, &[changed_hash_update]).expect("changed-hash upsert"),
            1
        );
        assert_eq!(
            symbol_summary(&mut conn, &symbol.id),
            None,
            "content-hash changes must clear existing summaries"
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn api_upsert_file_preserves_immutable_content_and_projection_state_on_conflict() {
        let (mut conn, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-api-file-upsert");
        cleanup_project(&mut conn, &project_id).expect("pre-clean test project rows");
        let _cleanup = ProjectCleanup {
            database_url,
            project_id: project_id.clone(),
        };
        seed_project(&mut conn, &project_id);

        let rel = "src/lib.rs";
        let mut file = indexed_file(&project_id, rel, "file-hash-v1", 1, 16);
        api::upsert_file(&mut conn, &file).expect("insert indexed file");
        let machine_id = gobby_core::machine::read_local_machine_id().expect("read machine id");
        api::upsert_file_state(&mut conn, &machine_id, &file).expect("insert indexed file state");

        assert!(
            db::mark_vector_sync_attempted(&mut conn, &project_id, rel)
                .expect("mark vector attempt")
        );
        assert_eq!(
            vector_sync_state(&mut conn, &project_id, rel),
            (false, true)
        );
        assert!(db::mark_vectors_synced(&mut conn, &project_id, rel).expect("mark vectors synced"));
        assert_eq!(
            db::reset_vectors_sync_for_project(&mut conn, &project_id).expect("reset vectors"),
            1
        );
        assert_eq!(
            vector_sync_state(&mut conn, &project_id, rel),
            (false, false)
        );
        assert_eq!(
            db::mark_project_vector_sync_attempted(&mut conn, &project_id)
                .expect("mark project vector attempt"),
            1
        );
        assert_eq!(
            db::mark_project_vectors_synced(&mut conn, &project_id).expect("mark project vectors"),
            1
        );
        assert_eq!(vector_sync_state(&mut conn, &project_id, rel), (true, true));

        let file_uuid = db::id_param(&file.id).expect("indexed-file id is a uuid");
        conn.execute(
            "UPDATE code_indexed_files
         SET graph_synced = true,
             graph_sync_attempted_at = NOW()
         WHERE id = $1",
            &[&file_uuid],
        )
        .expect("mark projections synced");

        file.content_hash = "file-hash-v2".to_string();
        file.symbol_count = 2;
        file.byte_size = 32;
        api::upsert_file(&mut conn, &file).expect("conflict upsert indexed file");

        let row = conn
            .query_one(
                "SELECT content_hash,
                    symbol_count,
                    byte_size,
                    graph_synced,
                    vectors_synced,
                    graph_sync_attempted_at IS NULL,
                    vector_sync_attempted_at IS NULL
             FROM code_indexed_files
             WHERE id = $1",
                &[&file_uuid],
            )
            .expect("load indexed file row");
        let content_hash: String = row.get(0);
        let symbol_count: i32 = row.get(1);
        let byte_size: i32 = row.get(2);
        let graph_synced: bool = row.get(3);
        let vectors_synced: bool = row.get(4);
        let graph_attempt_cleared: bool = row.get(5);
        let vector_attempt_cleared: bool = row.get(6);

        assert_eq!(content_hash, "file-hash-v1");
        assert_eq!(symbol_count, 2);
        assert_eq!(byte_size, 32);
        assert!(graph_synced, "existing content keeps its graph projection");
        assert!(
            vectors_synced,
            "existing content keeps its vector projection"
        );
        assert!(
            !graph_attempt_cleared,
            "existing content keeps the graph sync attempt timestamp"
        );
        assert!(
            !vector_attempt_cleared,
            "existing content keeps the vector sync attempt timestamp"
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn cross_machine_states_widen_orphan_keep_set_and_projection_resets() {
        let (mut conn, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-cross-machine-keep");
        cleanup_project(&mut conn, &project_id).expect("pre-clean test project rows");
        let _cleanup = ProjectCleanup {
            database_url,
            project_id: project_id.clone(),
        };
        seed_project(&mut conn, &project_id);

        let local_machine = gobby_core::machine::read_local_machine_id().expect("read machine id");
        let foreign_machine = uuid::Uuid::new_v5(
            &CODE_INDEX_UUID_NAMESPACE,
            format!("{project_id}:foreign-machine").as_bytes(),
        )
        .to_string();
        crate::test_env::seed_test_machine(&mut conn, &foreign_machine)
            .expect("seed foreign machine");
        seed_project_for_machine(&mut conn, &foreign_machine, &project_id);

        let local_file = indexed_file(&project_id, "src/local.rs", "hash-local", 1, 16);
        api::upsert_file(&mut conn, &local_file).expect("insert local file");
        api::upsert_file_state(&mut conn, &local_machine, &local_file).expect("local file state");

        let foreign_file = indexed_file(&project_id, "src/foreign.rs", "hash-foreign", 1, 16);
        api::upsert_file(&mut conn, &foreign_file).expect("insert foreign file");
        api::upsert_file_state(&mut conn, &foreign_machine, &foreign_file)
            .expect("foreign file state");

        // Content referenced by no machine stays out of the keep-set.
        let orphan_file = indexed_file(&project_id, "src/orphan.rs", "hash-orphan", 1, 16);
        api::upsert_file(&mut conn, &orphan_file).expect("insert orphan file");

        assert_eq!(
            db::list_indexed_file_paths(&mut conn, &project_id).expect("local paths"),
            vec!["src/local.rs".to_string()]
        );
        assert_eq!(
            db::list_all_machine_indexed_file_paths(&mut conn, &project_id).expect("all paths"),
            vec!["src/foreign.rs".to_string(), "src/local.rs".to_string()],
            "orphan cleanup keep-set must span every machine's file states"
        );

        // Clear/rebuild wipe the shared projections, so the resets must cover
        // rows referenced only by other machines and skip unreferenced rows.
        conn.execute(
            "UPDATE code_indexed_files
             SET vectors_synced = true, graph_synced = true
             WHERE project_id = $1",
            &[&db::id_param(&project_id).expect("test project id is a uuid")],
        )
        .expect("mark project rows synced");

        assert_eq!(
            db::reset_vectors_sync_for_project(&mut conn, &project_id).expect("reset vectors"),
            2,
            "vector reset must cover both machines' referenced rows only"
        );
        assert_eq!(
            db::reset_graph_sync_for_project(&mut conn, &project_id).expect("reset graph"),
            2,
            "graph reset must cover both machines' referenced rows only"
        );
        let (foreign_vectors_synced, _) =
            vector_sync_state(&mut conn, &project_id, "src/foreign.rs");
        assert!(
            !foreign_vectors_synced,
            "foreign machine's referenced row must drop to unsynced"
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn incomplete_projection_content_version_is_not_adopted() {
        let (mut conn, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-api-incomplete-adoption");
        let machine_id = unique_test_project_id("gcode-api-incomplete-adoption-machine");
        cleanup_project(&mut conn, &project_id).expect("pre-clean test project rows");
        let _cleanup = ProjectCleanup {
            database_url,
            project_id: project_id.clone(),
        };
        crate::test_env::seed_test_machine(&mut conn, &machine_id).expect("seed test machine");
        seed_project_for_machine(&mut conn, &machine_id, &project_id);

        let file = indexed_file(&project_id, "src/lib.rs", "file-hash", 1, 16);
        api::upsert_file(&mut conn, &file).expect("seed incomplete content version");
        assert!(
            !api::adopt_file_state(
                &mut conn,
                &machine_id,
                &project_id,
                &file.file_path,
                &file.content_hash,
            )
            .expect("reject incomplete adoption")
        );

        conn.execute(
            "UPDATE code_indexed_files
             SET graph_synced = TRUE, vectors_synced = TRUE
             WHERE id = $1",
            &[&uuid::Uuid::parse_str(&file.id).expect("valid file id")],
        )
        .expect("complete projection state");
        assert!(
            api::adopt_file_state(
                &mut conn,
                &machine_id,
                &project_id,
                &file.file_path,
                &file.content_hash,
            )
            .expect("adopt complete content version")
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn api_upsert_imports_and_calls_report_rows_inserted_not_input_len() {
        let (mut conn, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-api-relation-upsert");
        cleanup_project(&mut conn, &project_id).expect("pre-clean test project rows");
        let _cleanup = ProjectCleanup {
            database_url,
            project_id: project_id.clone(),
        };
        seed_project(&mut conn, &project_id);

        let rel = "src/lib.rs";
        api::upsert_file(
            &mut conn,
            &indexed_file(&project_id, rel, "file-hash", 0, 16),
        )
        .expect("seed indexed file");
        let import = ImportRelation {
            file_path: rel.to_string(),
            module_name: "std::fs".to_string(),
        };
        assert_eq!(
            api::upsert_imports(
                &mut conn,
                &project_id,
                rel,
                "file-hash",
                &[import.clone(), import],
            )
            .expect("upsert duplicate imports"),
            1
        );

        let call = CallRelation::new(
            Symbol::make_id(&project_id, rel, "file-hash", "caller", "function", 0),
            "read_to_string".to_string(),
            rel.to_string(),
            7,
        );
        assert_eq!(
            api::upsert_calls(
                &mut conn,
                &project_id,
                rel,
                "file-hash",
                &[call.clone(), call],
            )
            .expect("upsert duplicate calls"),
            1
        );

        let project_uuid = db::id_param(&project_id).expect("test project id is a uuid");
        let import_count: i64 = conn
            .query_one(
                "SELECT COUNT(*) FROM code_imports WHERE project_id = $1",
                &[&project_uuid],
            )
            .expect("count imports")
            .get(0);
        let call_count: i64 = conn
            .query_one(
                "SELECT COUNT(*) FROM code_calls WHERE project_id = $1",
                &[&project_uuid],
            )
            .expect("count calls")
            .get(0);
        assert_eq!(import_count, 1);
        assert_eq!(call_count, 1);
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn api_upsert_inheritance_reports_rows_inserted_not_input_len() {
        let (mut conn, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-api-inheritance-upsert");
        cleanup_project(&mut conn, &project_id).expect("pre-clean test project rows");
        let _cleanup = ProjectCleanup {
            database_url,
            project_id: project_id.clone(),
        };
        seed_project(&mut conn, &project_id);

        let rel = "src/lib.rs";
        api::upsert_file(
            &mut conn,
            &indexed_file(&project_id, rel, "file-hash", 0, 16),
        )
        .expect("seed indexed file");
        let source_id = Symbol::make_id(&project_id, rel, "file-hash", "Derived", "class", 0);
        let relation = InheritanceRelation {
            source_symbol_id: Some(source_id),
            source_name: "Derived".to_string(),
            source_kind: CallTargetKind::Symbol,
            source_external_module: None,
            target_symbol_id: None,
            target_name: "Base".to_string(),
            target_kind: CallTargetKind::Unresolved,
            target_external_module: None,
            heritage_kind: HeritageKind::Inherits,
            file_path: rel.to_string(),
            content_hash: "file-hash".to_string(),
            line: 3,
        };
        assert_eq!(
            api::upsert_inheritance(
                &mut conn,
                &project_id,
                rel,
                "file-hash",
                &[relation.clone(), relation],
            )
            .expect("upsert duplicate inheritance"),
            1
        );

        let project_uuid = db::id_param(&project_id).expect("test project id is a uuid");
        let inheritance_count: i64 = conn
            .query_one(
                "SELECT COUNT(*) FROM code_inheritance WHERE project_id = $1",
                &[&project_uuid],
            )
            .expect("count inheritance")
            .get(0);
        assert_eq!(inheritance_count, 1);
    }
}

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn mark_graph_unsynced_clears_completion_and_attempt_for_selected_files() {
    let (mut conn, database_url) = connect_test_db();
    let project_id = unique_test_project_id("gcode-mark-graph-unsynced");
    cleanup_project(&mut conn, &project_id).expect("pre-clean test project rows");
    let _cleanup = ProjectCleanup {
        database_url,
        project_id: project_id.clone(),
    };
    seed_project(&mut conn, &project_id);
    let machine_id = gobby_core::machine::read_local_machine_id().expect("machine id");
    let file = indexed_file(&project_id, "src/lib.rs", "hash-1", 1, 16);
    api::upsert_file(&mut conn, &file).expect("insert indexed file");
    api::upsert_file_state(&mut conn, &machine_id, &file).expect("set current indexed file");
    let old_file = indexed_file(&project_id, "src/lib.rs", "hash-old", 1, 16);
    api::upsert_file(&mut conn, &old_file).expect("insert historic indexed file");
    let empty_file = indexed_file(&project_id, "src/empty.rs", "hash-empty", 0, 0);
    api::upsert_file(&mut conn, &empty_file).expect("insert empty indexed file");
    api::upsert_file_state(&mut conn, &machine_id, &empty_file)
        .expect("set current empty indexed file");
    let file_id = db::id_param(&file.id).expect("file uuid");
    conn.execute(
        "UPDATE code_indexed_files
         SET graph_synced = TRUE, graph_sync_attempted_at = NOW()
         WHERE project_id = $1",
        &[&db::id_param(&project_id).expect("project uuid")],
    )
    .expect("mark graph synced");
    assert_eq!(
        api::graph_synced_files(&mut conn, &machine_id, &project_id)
            .expect("list graph candidates")
            .iter()
            .map(|file| file.file_path.as_str())
            .collect::<Vec<_>>(),
        vec!["src/lib.rs"]
    );

    assert_eq!(
        api::mark_graph_unsynced(
            &mut conn,
            &machine_id,
            &project_id,
            &["src/lib.rs".to_string()],
        )
        .expect("mark graph pending"),
        1
    );
    let row = conn
        .query_one(
            "SELECT graph_synced, graph_sync_attempted_at IS NULL
             FROM code_indexed_files WHERE id = $1",
            &[&file_id],
        )
        .expect("read graph state");
    assert!(!row.get::<_, bool>(0));
    assert!(row.get::<_, bool>(1));
    let historic_synced: bool = conn
        .query_one(
            "SELECT graph_synced FROM code_indexed_files WHERE id = $1",
            &[&db::id_param(&old_file.id).expect("historic file uuid")],
        )
        .expect("read historic graph state")
        .get(0);
    assert!(historic_synced);
}

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn project_stats_preserve_and_replace_indexer_version() {
    let (mut conn, database_url) = connect_test_db();
    let project_id = unique_test_project_id("gcode-indexer-version");
    cleanup_project(&mut conn, &project_id).expect("pre-clean test project rows");
    let _cleanup = ProjectCleanup {
        database_url,
        project_id: project_id.clone(),
    };
    let machine_id = gobby_core::machine::read_local_machine_id().expect("machine id");
    let mut project = IndexedProject {
        id: project_id.clone(),
        root_path: format!("/tmp/{project_id}"),
        total_files: 1,
        total_symbols: 1,
        last_indexed_at: String::new(),
        index_duration_ms: 0,
        total_eligible_files: None,
        indexer_version: None,
    };
    api::upsert_project_stats(&mut conn, &machine_id, &project).expect("seed null version");
    assert_eq!(
        api::project_indexer_version(&mut conn, &machine_id, &project_id)
            .expect("read null version"),
        None
    );

    project.indexer_version = Some("1.6.0".to_string());
    api::upsert_project_stats(&mut conn, &machine_id, &project).expect("stamp version");
    project.indexer_version = None;
    api::upsert_project_stats(&mut conn, &machine_id, &project).expect("preserve version");
    assert_eq!(
        api::project_indexer_version(&mut conn, &machine_id, &project_id)
            .expect("read version")
            .as_deref(),
        Some("1.6.0")
    );

    project.indexer_version = Some("1.6.1".to_string());
    api::upsert_project_stats(&mut conn, &machine_id, &project).expect("replace version");
    assert_eq!(
        api::project_indexer_version(&mut conn, &machine_id, &project_id)
            .expect("read replacement")
            .as_deref(),
        Some("1.6.1")
    );
}

fn connect_test_db() -> (postgres::Client, String) {
    let database_url = crate::test_env::postgres_test_database_url("postgres API SQL tests");
    let conn = gobby_core::postgres::connect_readwrite(&database_url)
        .expect("connect to PostgreSQL test database");
    (conn, database_url)
}

fn seed_project(conn: &mut postgres::Client, project_id: &str) {
    let machine_id = gobby_core::machine::read_local_machine_id().expect("read machine id");
    seed_project_for_machine(conn, &machine_id, project_id);
}

fn seed_project_for_machine(conn: &mut postgres::Client, machine_id: &str, project_id: &str) {
    api::upsert_project_stats(
        conn,
        machine_id,
        &IndexedProject {
            id: project_id.to_string(),
            root_path: format!("/tmp/{project_id}"),
            total_files: 1,
            total_symbols: 1,
            last_indexed_at: String::new(),
            index_duration_ms: 0,
            total_eligible_files: None,
            indexer_version: None,
        },
    )
    .expect("seed project row");
}

fn indexed_file(
    project_id: &str,
    file_path: &str,
    content_hash: &str,
    symbol_count: usize,
    byte_size: usize,
) -> IndexedFile {
    IndexedFile {
        id: IndexedFile::make_id(project_id, file_path, content_hash),
        project_id: project_id.to_string(),
        file_path: file_path.to_string(),
        language: "rust".to_string(),
        content_hash: content_hash.to_string(),
        symbol_count,
        byte_size,
        indexed_at: String::new(),
    }
}

fn test_symbol(
    project_id: &str,
    file_path: &str,
    file_content_hash: &str,
    name: &str,
    byte_start: usize,
    content_hash: &str,
) -> Symbol {
    Symbol {
        id: Symbol::make_id(
            project_id,
            file_path,
            file_content_hash,
            name,
            "function",
            byte_start,
        ),
        project_id: project_id.to_string(),
        file_path: file_path.to_string(),
        name: name.to_string(),
        qualified_name: name.to_string(),
        kind: "function".to_string(),
        language: "rust".to_string(),
        byte_start,
        byte_end: byte_start + name.len(),
        line_start: 1,
        line_end: 1,
        signature: Some(format!("fn {name}()")),
        docstring: None,
        parent_symbol_id: None,
        file_content_hash: file_content_hash.to_string(),
        content_hash: content_hash.to_string(),
        summary: None,
        created_at: String::new(),
        updated_at: String::new(),
    }
}

fn symbol_summary(conn: &mut postgres::Client, symbol_id: &str) -> Option<String> {
    conn.query_one(
        "SELECT summary FROM code_symbols WHERE id = $1",
        &[&db::id_param(symbol_id).expect("test symbol id is a uuid")],
    )
    .expect("load symbol summary")
    .get(0)
}

fn vector_sync_state(
    conn: &mut postgres::Client,
    project_id: &str,
    file_path: &str,
) -> (bool, bool) {
    let row = conn
        .query_one(
            "SELECT vectors_synced, vector_sync_attempted_at IS NOT NULL AS attempted
             FROM code_indexed_files
             WHERE project_id = $1 AND file_path = $2",
            &[
                &db::id_param(project_id).expect("test project id is a uuid"),
                &file_path,
            ],
        )
        .expect("load vector sync state");
    (row.get("vectors_synced"), row.get("attempted"))
}

fn unique_test_project_id(prefix: &str) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is after unix epoch")
        .as_nanos();
    uuid::Uuid::new_v5(
        &CODE_INDEX_UUID_NAMESPACE,
        format!("{prefix}-{nanos}").as_bytes(),
    )
    .to_string()
}

struct ProjectCleanup {
    database_url: String,
    project_id: String,
}

impl Drop for ProjectCleanup {
    fn drop(&mut self) {
        if let Ok(mut conn) = gobby_core::postgres::connect_readwrite(&self.database_url) {
            let _ = cleanup_project(&mut conn, &self.project_id);
        }
    }
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
        "DELETE FROM code_calls WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_inheritance WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_imports WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_content_chunks WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_symbols WHERE project_id = $1",
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
