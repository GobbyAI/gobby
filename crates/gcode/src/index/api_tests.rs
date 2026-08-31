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
        api::upsert_file_state(
            &mut conn,
            &machine_id,
            &file,
            std::path::Path::new("/tmp/test-view"),
            api::IndexWriteMode::Overlay,
        )
        .expect("insert indexed file state");

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
        api::upsert_file_state(
            &mut conn,
            &local_machine,
            &local_file,
            std::path::Path::new("/tmp/local-view"),
            api::IndexWriteMode::Overlay,
        )
        .expect("local file state");

        let foreign_file = indexed_file(&project_id, "src/foreign.rs", "hash-foreign", 1, 16);
        api::upsert_file(&mut conn, &foreign_file).expect("insert foreign file");
        api::upsert_file_state(
            &mut conn,
            &foreign_machine,
            &foreign_file,
            std::path::Path::new("/tmp/foreign-view"),
            api::IndexWriteMode::Overlay,
        )
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
                std::path::Path::new("/tmp/test-view"),
                api::IndexWriteMode::Overlay,
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
                std::path::Path::new("/tmp/test-view"),
                api::IndexWriteMode::Overlay,
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
    api::upsert_file_state(
        &mut conn,
        &machine_id,
        &file,
        std::path::Path::new("/tmp/test-view"),
        api::IndexWriteMode::Overlay,
    )
    .expect("set current indexed file");
    let old_file = indexed_file(&project_id, "src/lib.rs", "hash-old", 1, 16);
    api::upsert_file(&mut conn, &old_file).expect("insert historic indexed file");
    let empty_file = indexed_file(&project_id, "src/empty.rs", "hash-empty", 0, 0);
    api::upsert_file(&mut conn, &empty_file).expect("insert empty indexed file");
    api::upsert_file_state(
        &mut conn,
        &machine_id,
        &empty_file,
        std::path::Path::new("/tmp/test-view"),
        api::IndexWriteMode::Overlay,
    )
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
    api::upsert_project_stats(
        &mut conn,
        &machine_id,
        &project,
        api::IndexWriteMode::Overlay,
    )
    .expect("seed null version");
    assert_eq!(
        api::project_indexer_version(&mut conn, &machine_id, &project_id)
            .expect("read null version"),
        None
    );

    project.indexer_version = Some("1.6.0".to_string());
    api::upsert_project_stats(
        &mut conn,
        &machine_id,
        &project,
        api::IndexWriteMode::Overlay,
    )
    .expect("stamp version");
    project.indexer_version = None;
    api::upsert_project_stats(
        &mut conn,
        &machine_id,
        &project,
        api::IndexWriteMode::Overlay,
    )
    .expect("preserve version");
    assert_eq!(
        api::project_indexer_version(&mut conn, &machine_id, &project_id)
            .expect("read version")
            .as_deref(),
        Some("1.6.0")
    );

    project.indexer_version = Some("1.6.1".to_string());
    api::upsert_project_stats(
        &mut conn,
        &machine_id,
        &project,
        api::IndexWriteMode::Overlay,
    )
    .expect("replace version");
    assert_eq!(
        api::project_indexer_version(&mut conn, &machine_id, &project_id)
            .expect("read replacement")
            .as_deref(),
        Some("1.6.1")
    );
}

#[test]
#[serial_test::serial(serial_db)]
fn project_seed_modes_require_primary_checkout_and_allow_overlay() {
    let (mut conn, database_url) = connect_test_db();
    let project_id = unique_test_project_id("gcode-primary-mode");
    let overlay_id = unique_test_project_id("gcode-overlay-mode");
    cleanup_project(&mut conn, &project_id).expect("pre-clean primary rows");
    cleanup_project(&mut conn, &overlay_id).expect("pre-clean overlay rows");
    let _primary_cleanup = ProjectCleanup {
        database_url: database_url.clone(),
        project_id: project_id.clone(),
    };
    let _overlay_cleanup = ProjectCleanup {
        database_url: database_url.clone(),
        project_id: overlay_id.clone(),
    };
    let machine_id = gobby_core::machine::read_local_machine_id().expect("machine id");
    let root = std::path::Path::new("/tmp/primary-checkout");
    let project_uuid = db::id_param(&project_id).expect("project uuid");
    let machine_uuid = db::id_param(&machine_id).expect("machine uuid");
    conn.execute(
        "INSERT INTO projects (id, name) VALUES ($1, $2)",
        &[&project_uuid, &format!("primary-{project_id}")],
    )
    .expect("seed registry project");
    conn.execute(
        "INSERT INTO project_checkouts (machine_id, project_id, root_path)
         VALUES ($1, $2, $3)",
        &[&machine_uuid, &project_uuid, &root.to_string_lossy()],
    )
    .expect("seed primary checkout");

    api::upsert_project_seed(
        &mut conn,
        &machine_id,
        &project_id,
        root,
        api::IndexWriteMode::Primary,
    )
    .expect("matching primary seed");
    let stale = api::upsert_project_seed(
        &mut conn,
        &machine_id,
        &project_id,
        std::path::Path::new("/tmp/stale-checkout"),
        api::IndexWriteMode::Primary,
    )
    .expect_err("stale primary root");
    assert!(stale.to_string().contains("checkout"));
    let matching_stats = IndexedProject {
        id: project_id.clone(),
        root_path: root.display().to_string(),
        total_files: 2,
        total_symbols: 3,
        last_indexed_at: String::new(),
        index_duration_ms: 4,
        total_eligible_files: None,
        indexer_version: None,
    };
    api::upsert_project_stats(
        &mut conn,
        &machine_id,
        &matching_stats,
        api::IndexWriteMode::Primary,
    )
    .expect("matching primary stats");
    let mut stale_stats = matching_stats.clone();
    stale_stats.root_path = "/tmp/stale-checkout".to_string();
    api::upsert_project_stats(
        &mut conn,
        &machine_id,
        &stale_stats,
        api::IndexWriteMode::Primary,
    )
    .expect_err("stale primary stats");

    let mut rebind_conn =
        gobby_core::postgres::connect_readwrite(&database_url).expect("connect rebind");
    rebind_conn
        .batch_execute("SET statement_timeout = '250ms'")
        .expect("set bounded rebind wait");
    {
        let mut seed_tx = conn.transaction().expect("begin primary seed writer");
        api::upsert_project_seed(
            &mut seed_tx,
            &machine_id,
            &project_id,
            root,
            api::IndexWriteMode::Primary,
        )
        .expect("seed writer holds checkout lock");
        let blocked = rebind_conn
            .execute(
                "UPDATE project_checkouts SET root_path = $3
                 WHERE machine_id = $1 AND project_id = $2",
                &[
                    &machine_uuid,
                    &project_uuid,
                    &"/tmp/primary-checkout-rebound",
                ],
            )
            .expect_err("rebind must wait for seed writer");
        assert_eq!(blocked.code().map(|code| code.code()), Some("57014"));
        seed_tx.commit().expect("commit primary seed writer");
    }
    {
        let mut stats_tx = conn.transaction().expect("begin primary stats writer");
        api::upsert_project_stats(
            &mut stats_tx,
            &machine_id,
            &matching_stats,
            api::IndexWriteMode::Primary,
        )
        .expect("stats writer holds checkout lock");
        let blocked = rebind_conn
            .execute(
                "UPDATE project_checkouts SET root_path = $3
                 WHERE machine_id = $1 AND project_id = $2",
                &[
                    &machine_uuid,
                    &project_uuid,
                    &"/tmp/primary-checkout-rebound",
                ],
            )
            .expect_err("rebind must wait for stats writer");
        assert_eq!(blocked.code().map(|code| code.code()), Some("57014"));
        stats_tx.commit().expect("commit primary stats writer");
    }
    rebind_conn
        .batch_execute("RESET statement_timeout")
        .expect("reset bounded rebind wait");
    rebind_conn
        .execute(
            "UPDATE project_checkouts SET root_path = $3
             WHERE machine_id = $1 AND project_id = $2",
            &[
                &machine_uuid,
                &project_uuid,
                &"/tmp/primary-checkout-rebound",
            ],
        )
        .expect("rebind after project writers commit");
    api::upsert_project_seed(
        &mut conn,
        &machine_id,
        &project_id,
        root,
        api::IndexWriteMode::Primary,
    )
    .expect_err("old-root seed cannot write after rebind");
    api::upsert_project_stats(
        &mut conn,
        &machine_id,
        &matching_stats,
        api::IndexWriteMode::Primary,
    )
    .expect_err("old-root stats cannot write after rebind");
    api::upsert_project_seed(
        &mut conn,
        &machine_id,
        &overlay_id,
        std::path::Path::new("/tmp/overlay-view"),
        api::IndexWriteMode::Overlay,
    )
    .expect("overlay seed without checkout");
}

#[test]
#[serial_test::serial(serial_db)]
fn file_state_modes_require_primary_checkout_and_allow_overlay() {
    let (mut conn, database_url) = connect_test_db();
    let project_id = unique_test_project_id("gcode-primary-file-mode");
    let overlay_id = unique_test_project_id("gcode-overlay-file-mode");
    cleanup_project(&mut conn, &project_id).expect("pre-clean primary rows");
    cleanup_project(&mut conn, &overlay_id).expect("pre-clean overlay rows");
    let _primary_cleanup = ProjectCleanup {
        database_url: database_url.clone(),
        project_id: project_id.clone(),
    };
    let _overlay_cleanup = ProjectCleanup {
        database_url: database_url.clone(),
        project_id: overlay_id.clone(),
    };
    let machine_id = gobby_core::machine::read_local_machine_id().expect("machine id");
    let root = std::path::Path::new("/tmp/primary-file-checkout");
    let project_uuid = db::id_param(&project_id).expect("project uuid");
    let machine_uuid = db::id_param(&machine_id).expect("machine uuid");
    conn.execute(
        "INSERT INTO projects (id, name) VALUES ($1, $2)",
        &[&project_uuid, &format!("primary-file-{project_id}")],
    )
    .expect("seed registry project");
    conn.execute(
        "INSERT INTO project_checkouts (machine_id, project_id, root_path)
         VALUES ($1, $2, $3)",
        &[&machine_uuid, &project_uuid, &root.to_string_lossy()],
    )
    .expect("seed primary checkout");
    api::upsert_project_seed(
        &mut conn,
        &machine_id,
        &project_id,
        root,
        api::IndexWriteMode::Primary,
    )
    .expect("seed primary indexed project");
    let file = indexed_file(&project_id, "src/lib.rs", "hash-a", 1, 10);
    api::upsert_file(&mut conn, &file).expect("seed shared file");
    conn.execute(
        "UPDATE code_indexed_files SET graph_synced = true, vectors_synced = true
         WHERE id = $1",
        &[&db::id_param(&file.id).expect("file uuid")],
    )
    .expect("mark projections synced");

    api::upsert_file_state(
        &mut conn,
        &machine_id,
        &file,
        root,
        api::IndexWriteMode::Primary,
    )
    .expect("matching primary file state");
    api::upsert_file_state(
        &mut conn,
        &machine_id,
        &file,
        std::path::Path::new("/tmp/stale-file-checkout"),
        api::IndexWriteMode::Primary,
    )
    .expect_err("stale primary file state");
    api::delete_file_state(
        &mut conn,
        &machine_id,
        &project_id,
        &file.file_path,
        std::path::Path::new("/tmp/stale-file-checkout"),
        api::IndexWriteMode::Primary,
    )
    .expect_err("stale primary delete");
    assert!(
        api::delete_file_state(
            &mut conn,
            &machine_id,
            &project_id,
            &file.file_path,
            root,
            api::IndexWriteMode::Primary,
        )
        .expect("matching primary delete")
    );
    assert!(
        api::adopt_file_state(
            &mut conn,
            &machine_id,
            &project_id,
            &file.file_path,
            &file.content_hash,
            root,
            api::IndexWriteMode::Primary,
        )
        .expect("matching primary adoption")
    );
    api::adopt_file_state(
        &mut conn,
        &machine_id,
        &project_id,
        &file.file_path,
        &file.content_hash,
        std::path::Path::new("/tmp/stale-file-checkout"),
        api::IndexWriteMode::Primary,
    )
    .expect_err("stale primary adoption");

    let mut rebind_conn =
        gobby_core::postgres::connect_readwrite(&database_url).expect("connect file rebind");
    rebind_conn
        .batch_execute("SET statement_timeout = '250ms'")
        .expect("set bounded file rebind wait");
    for writer in ["upsert", "adopt", "delete"] {
        let mut writer_tx = conn.transaction().expect("begin primary file writer");
        match writer {
            "upsert" => api::upsert_file_state(
                &mut writer_tx,
                &machine_id,
                &file,
                root,
                api::IndexWriteMode::Primary,
            ),
            "adopt" => api::adopt_file_state(
                &mut writer_tx,
                &machine_id,
                &project_id,
                &file.file_path,
                &file.content_hash,
                root,
                api::IndexWriteMode::Primary,
            )
            .map(|_| ()),
            "delete" => api::delete_file_state(
                &mut writer_tx,
                &machine_id,
                &project_id,
                &file.file_path,
                root,
                api::IndexWriteMode::Primary,
            )
            .map(|_| ()),
            _ => unreachable!("closed writer table"),
        }
        .expect("matching file writer holds checkout lock");
        let blocked = rebind_conn
            .execute(
                "UPDATE project_checkouts SET root_path = $3
                 WHERE machine_id = $1 AND project_id = $2",
                &[
                    &machine_uuid,
                    &project_uuid,
                    &"/tmp/primary-file-checkout-rebound",
                ],
            )
            .expect_err("rebind must wait for file writer");
        assert_eq!(
            blocked.code().map(|code| code.code()),
            Some("57014"),
            "{writer} must hold the checkout lock"
        );
        writer_tx.commit().expect("commit primary file writer");
    }
    rebind_conn
        .batch_execute("RESET statement_timeout")
        .expect("reset bounded file rebind wait");
    rebind_conn
        .execute(
            "UPDATE project_checkouts SET root_path = $3
             WHERE machine_id = $1 AND project_id = $2",
            &[
                &machine_uuid,
                &project_uuid,
                &"/tmp/primary-file-checkout-rebound",
            ],
        )
        .expect("rebind after file writers commit");
    api::upsert_file_state(
        &mut conn,
        &machine_id,
        &file,
        root,
        api::IndexWriteMode::Primary,
    )
    .expect_err("old-root file upsert cannot write after rebind");
    api::adopt_file_state(
        &mut conn,
        &machine_id,
        &project_id,
        &file.file_path,
        &file.content_hash,
        root,
        api::IndexWriteMode::Primary,
    )
    .expect_err("old-root adoption cannot write after rebind");
    api::delete_file_state(
        &mut conn,
        &machine_id,
        &project_id,
        &file.file_path,
        root,
        api::IndexWriteMode::Primary,
    )
    .expect_err("old-root delete cannot write after rebind");

    let overlay_file = indexed_file(&overlay_id, "src/overlay.rs", "hash-overlay", 1, 10);
    api::upsert_project_seed(
        &mut conn,
        &machine_id,
        &overlay_id,
        std::path::Path::new("/tmp/overlay-view"),
        api::IndexWriteMode::Overlay,
    )
    .expect("seed overlay indexed project");
    api::upsert_file(&mut conn, &overlay_file).expect("seed overlay shared file");
    api::upsert_file_state(
        &mut conn,
        &machine_id,
        &overlay_file,
        std::path::Path::new("/tmp/overlay-view"),
        api::IndexWriteMode::Overlay,
    )
    .expect("overlay file state without checkout");
}

#[test]
#[serial_test::serial(serial_db)]
fn primary_writer_blocks_rebind_and_stale_writer_cannot_repopulate() {
    let (mut writer_conn, database_url) = connect_test_db();
    let project_id = unique_test_project_id("gcode-primary-rebind-race");
    cleanup_project(&mut writer_conn, &project_id).expect("pre-clean race rows");
    let _cleanup = ProjectCleanup {
        database_url: database_url.clone(),
        project_id: project_id.clone(),
    };
    let machine_id = gobby_core::machine::read_local_machine_id().expect("machine id");
    let machine_uuid = db::id_param(&machine_id).expect("machine uuid");
    let project_uuid = db::id_param(&project_id).expect("project uuid");
    let old_root = std::path::Path::new("/tmp/primary-race-old");
    let new_root = std::path::Path::new("/tmp/primary-race-new");
    writer_conn
        .execute(
            "INSERT INTO projects (id, name) VALUES ($1, $2)",
            &[&project_uuid, &format!("primary-race-{project_id}")],
        )
        .expect("seed registry project");
    writer_conn
        .execute(
            "INSERT INTO project_checkouts (machine_id, project_id, root_path)
             VALUES ($1, $2, $3)",
            &[&machine_uuid, &project_uuid, &old_root.to_string_lossy()],
        )
        .expect("seed primary checkout");
    api::upsert_project_seed(
        &mut writer_conn,
        &machine_id,
        &project_id,
        old_root,
        api::IndexWriteMode::Primary,
    )
    .expect("seed indexed project");
    let file = indexed_file(&project_id, "src/lib.rs", "race-hash", 1, 10);
    api::upsert_file(&mut writer_conn, &file).expect("seed shared file");

    let mut writer_tx = writer_conn.transaction().expect("begin primary writer");
    api::upsert_file_state(
        &mut writer_tx,
        &machine_id,
        &file,
        old_root,
        api::IndexWriteMode::Primary,
    )
    .expect("write primary selector while holding checkout lock");

    let mut rebind_conn =
        gobby_core::postgres::connect_readwrite(&database_url).expect("connect concurrent rebind");
    rebind_conn
        .batch_execute("SET statement_timeout = '250ms'")
        .expect("set bounded lock wait");
    let blocked = rebind_conn
        .execute(
            "UPDATE project_checkouts SET root_path = $3
             WHERE machine_id = $1 AND project_id = $2",
            &[&machine_uuid, &project_uuid, &new_root.to_string_lossy()],
        )
        .expect_err("rebind must wait for the primary writer transaction");
    assert_eq!(blocked.code().map(|code| code.code()), Some("57014"));

    writer_tx.commit().expect("commit primary writer");
    rebind_conn
        .batch_execute("RESET statement_timeout")
        .expect("reset bounded lock wait");
    let mut rebind_tx = rebind_conn.transaction().expect("begin rebind");
    rebind_tx
        .query_one(
            "SELECT 1 FROM project_checkouts
             WHERE machine_id = $1 AND project_id = $2 FOR UPDATE",
            &[&machine_uuid, &project_uuid],
        )
        .expect("lock checkout for rebind");
    rebind_tx
        .execute(
            "UPDATE project_checkouts SET root_path = $3
             WHERE machine_id = $1 AND project_id = $2",
            &[&machine_uuid, &project_uuid, &new_root.to_string_lossy()],
        )
        .expect("commit new checkout root");
    rebind_tx
        .execute(
            "DELETE FROM code_indexed_file_states
             WHERE machine_id = $1 AND project_id = $2",
            &[&machine_uuid, &project_uuid],
        )
        .expect("clear rebound machine selectors");
    rebind_tx.commit().expect("commit rebind");

    let selector_count: i64 = writer_conn
        .query_one(
            "SELECT COUNT(*)::BIGINT FROM code_indexed_file_states
             WHERE machine_id = $1 AND project_id = $2",
            &[&machine_uuid, &project_uuid],
        )
        .expect("count rebound selectors")
        .get(0);
    assert_eq!(selector_count, 0);
    api::upsert_file_state(
        &mut writer_conn,
        &machine_id,
        &file,
        old_root,
        api::IndexWriteMode::Primary,
    )
    .expect_err("stale writer cannot repopulate after rebind");
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
        api::IndexWriteMode::Overlay,
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
    conn.execute(
        "DELETE FROM project_checkouts WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute("DELETE FROM projects WHERE id = $1", &[&project_id])?;
    Ok(())
}
