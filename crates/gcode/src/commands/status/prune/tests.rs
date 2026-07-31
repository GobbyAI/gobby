use std::time::{SystemTime, UNIX_EPOCH};

use super::reconcile::{bounded_project_id_summary, optional_reconcile_totals_lines};
use super::*;

#[test]
fn prune_without_project_uses_all_indexed_projection_scope() {
    assert_eq!(
        projection_cleanup_scope(None),
        ProjectionCleanupScope::AllIndexedProjects
    );
}

#[test]
fn prune_with_project_uses_single_resolved_projection_scope() {
    assert_eq!(
        projection_cleanup_scope(Some("/tmp/project")),
        ProjectionCleanupScope::ResolvedProjectOverride
    );
}

#[test]
fn optional_reconcile_status_reports_configured_without_totals() {
    assert_eq!(
        optional_reconcile_totals_lines("Qdrant collection reconciliation", true, None, None,),
        [
            "Qdrant collection reconciliation: configured, but no reconciliation totals were produced"
        ]
    );
}

#[test]
fn optional_reconcile_status_reports_orphan_buckets_separately() {
    let totals = ReconcileTotals {
        scanned: 7,
        active: 1,
        orphaned: 5,
        invalid: 1,
        ..ReconcileTotals::default()
    };

    let lines = optional_reconcile_totals_lines(
        "Qdrant collection reconciliation",
        true,
        Some(&totals),
        Some((2, 3)),
    );

    assert_eq!(
        lines,
        [
            "Qdrant collection reconciliation: scanned=7, active=1, orphaned=5, deleted=0, already_missing=0, busy=0, invalid=1, failed=0",
            "  orphan buckets: existing=2, pending_stale_project_cleanup=3",
        ]
    );
}

#[test]
fn orphan_projection_cleanup_requires_confirmed_non_skipped_cleanup() {
    assert!(orphan_projection_cleanup_confirmed(true, false));
    assert!(!orphan_projection_cleanup_confirmed(true, true));
    assert!(!orphan_projection_cleanup_confirmed(false, false));
    assert!(!orphan_projection_cleanup_confirmed(false, true));
}

#[test]
fn bounded_project_id_summary_caps_ids() {
    let ids = (0..10)
        .map(|idx| format!("project-{idx:02}-abcdef"))
        .collect::<Vec<_>>();

    let summary = bounded_project_id_summary(&ids);

    assert!(summary.contains("project-"));
    assert!(summary.contains("+2 more"));
}

#[test]
fn global_prune_strict_collection_classification() {
    let active_id = "11111111-1111-1111-1111-111111111111";
    let stale_id = "22222222-2222-2222-2222-222222222222";
    let orphan_id = "33333333-3333-3333-3333-333333333333";
    let authority = HashSet::from([active_id.to_string(), stale_id.to_string()]);
    let stale = HashSet::from([stale_id.to_string()]);
    let collections = vec![
        format!("code_symbols_{active_id}"),
        format!("code_symbols_{stale_id}"),
        format!("code_symbols_{orphan_id}"),
        "code_symbols_33333333333333333333333333333333".to_string(),
        "code_symbols_44444444-4444-4444-4444-44444444444A".to_string(),
        "code_symbols_invalid".to_string(),
        "memory_vectors".to_string(),
    ];

    let inventory = classify_collection_inventory(&collections, &authority, &stale);

    assert_eq!(inventory.scanned, 6);
    assert_eq!(inventory.active, 1);
    assert_eq!(inventory.invalid, 3);
    assert_eq!(inventory.existing_orphan_ids, vec![orphan_id.to_string()]);
    assert_eq!(inventory.would_be_orphan_ids, vec![stale_id.to_string()]);
}

#[test]
fn global_prune_authorization_matrix_uses_single_gate() {
    let cases = [
        DestructiveSet {
            stale_project_ids: vec!["stale".to_string()],
            orphan_collection_ids: vec!["collection".to_string()],
            orphan_graph_scope_ids: Vec::new(),
            orphan_sql_project_ids: Vec::new(),
        },
        DestructiveSet {
            stale_project_ids: vec!["stale".to_string()],
            ..DestructiveSet::default()
        },
        DestructiveSet {
            orphan_collection_ids: vec!["collection".to_string()],
            ..DestructiveSet::default()
        },
        DestructiveSet {
            orphan_graph_scope_ids: vec!["graph".to_string()],
            ..DestructiveSet::default()
        },
    ];

    for pending in cases {
        let mut prompts = 0;
        let authorized = authorize_prune_with(false, &pending, |_| {
            prompts += 1;
            Ok(true)
        })
        .expect("authorize prune");
        assert!(authorized);
        assert_eq!(prompts, 1);
    }

    let pending = DestructiveSet {
        stale_project_ids: vec!["stale".to_string()],
        orphan_collection_ids: vec!["collection".to_string()],
        orphan_graph_scope_ids: vec!["graph".to_string()],
        orphan_sql_project_ids: Vec::new(),
    };
    let mut prompts = 0;
    let authorized = authorize_prune_with(false, &pending, |_| {
        prompts += 1;
        Ok(false)
    })
    .expect("decline prune");
    assert!(!authorized);
    assert_eq!(prompts, 1);

    let mut forced_prompts = 0;
    assert!(
        authorize_prune_with(true, &pending, |_| {
            forced_prompts += 1;
            Ok(false)
        })
        .expect("force prune")
    );
    assert_eq!(forced_prompts, 0);
}

#[test]
fn global_prune_sweep_rechecks_and_continues_after_failures() {
    let ids = vec![
        "active".to_string(),
        "busy".to_string(),
        "deleted".to_string(),
        "missing".to_string(),
        "failed".to_string(),
    ];
    let mut visited = Vec::new();

    let totals = sweep_discovered_ids_with(&ids, |project_id| {
        visited.push(project_id.to_string());
        match project_id {
            "active" => Ok(SweepOutcome::Active),
            "busy" => Ok(SweepOutcome::Busy),
            "deleted" => Ok(SweepOutcome::Deleted),
            "missing" => Ok(SweepOutcome::AlreadyMissing),
            "failed" => anyhow::bail!("backend delete failed"),
            _ => unreachable!(),
        }
    });

    assert_eq!(visited, ids);
    assert_eq!(totals.scanned, 5);
    assert_eq!(totals.active, 1);
    assert_eq!(totals.orphaned, 5);
    assert_eq!(totals.deleted, 1);
    assert_eq!(totals.already_missing, 1);
    assert_eq!(totals.busy, 1);
    assert_eq!(totals.failed, 1);
    assert!(totals.has_failures());
}

mod serial_db {
    use super::*;

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn global_prune_collection_recheck_retains_row_inserted_after_discovery() {
        let (mut conn, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-prune-recheck");
        cleanup_project(&mut conn, &project_id).expect("pre-clean project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };
        let authority = HashSet::new();
        let inventory = classify_collection_inventory(
            &[format!("code_symbols_{project_id}")],
            &authority,
            &HashSet::new(),
        );
        assert_eq!(inventory.existing_orphan_ids, vec![project_id.clone()]);

        seed_project_with_child_rows(&mut conn, &project_id, true);
        let ctx = prune_test_context(database_url, &project_id, true);

        assert_eq!(
            reconcile_orphan_collection(&ctx, &project_id).expect("recheck collection"),
            SweepOutcome::Active
        );
        assert!(db::indexed_project_exists(&mut conn, &project_id).expect("project exists"));
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn global_prune_busy_lock_defers_entire_stale_project() {
        let (mut conn, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-prune-stale-busy");
        cleanup_project(&mut conn, &project_id).expect("pre-clean project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };
        seed_project_with_child_rows(&mut conn, &project_id, true);
        let lock_key = crate::index_lock::project_lock_key(&project_id);
        conn.query_one("SELECT pg_advisory_lock($1)", &[&lock_key])
            .expect("hold project lock");

        let discovery = GlobalPruneDiscovery {
            services: prune_test_context(database_url, &project_id, true),
            stale_projects: vec![StaleProjectPlan {
                id: project_id.clone(),
                label: project_id.clone(),
                reason: "test stale project".to_string(),
            }],
            collections: None,
            graph_scopes: None,
            orphan_sql_project_ids: Vec::new(),
        };
        let totals = mutate_stale_projects(&discovery);

        conn.query_one("SELECT pg_advisory_unlock($1)", &[&lock_key])
            .expect("release project lock");
        assert_eq!(totals.busy, 1);
        assert_eq!(totals.deleted, 0);
        assert_eq!(totals.failed, 0);
        assert!(db::indexed_project_exists(&mut conn, &project_id).expect("project exists"));
        assert_eq!(project_child_row_count(&mut conn, &project_id), 5);
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn project_scoped_discovery_filters_stale_and_orphan_rows_to_override() {
        let (mut conn, database_url) = connect_test_db();
        let target_stale_id = unique_test_project_id("gcode-prune-target-stale");
        let unrelated_stale_id = unique_test_project_id("gcode-prune-unrelated-stale");
        let target_orphan_id = unique_test_project_id("gcode-prune-target-orphan");
        let unrelated_orphan_id = unique_test_project_id("gcode-prune-unrelated-orphan");
        let project_ids = [
            &target_stale_id,
            &unrelated_stale_id,
            &target_orphan_id,
            &unrelated_orphan_id,
        ];
        for project_id in project_ids {
            cleanup_project(&mut conn, project_id).expect("pre-clean project rows");
        }
        let _cleanups = project_ids.map(|project_id| ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        });

        seed_project_with_child_rows(&mut conn, &target_stale_id, true);
        seed_project_with_child_rows(&mut conn, &unrelated_stale_id, true);
        seed_project_with_child_rows(&mut conn, &target_orphan_id, false);
        seed_project_with_child_rows(&mut conn, &unrelated_orphan_id, false);

        let stale_context = prune_test_context(database_url.clone(), &target_stale_id, false);
        let stale_discovery =
            discover_project_scoped_records(&stale_context).expect("discover target stale project");
        assert_eq!(
            stale_discovery
                .stale_projects
                .iter()
                .map(|project| project.id.as_str())
                .collect::<Vec<_>>(),
            [target_stale_id.as_str()]
        );
        assert!(stale_discovery.orphan_sql_project_ids.is_empty());

        let orphan_context = prune_test_context(database_url, &target_orphan_id, false);
        let orphan_discovery = discover_project_scoped_records(&orphan_context)
            .expect("discover target orphan project");
        assert!(orphan_discovery.stale_projects.is_empty());
        assert_eq!(orphan_discovery.orphan_sql_project_ids, [target_orphan_id]);
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn orphan_project_discovery_and_sql_deletion_counts() {
        let (mut conn, database_url) = connect_test_db();
        let valid_project_id = unique_test_project_id("gcode-orphan-valid");
        let orphan_project_id = unique_test_project_id("gcode-orphan-missing-parent");
        cleanup_project(&mut conn, &valid_project_id).expect("pre-clean valid project rows");
        cleanup_project(&mut conn, &orphan_project_id).expect("pre-clean orphan project rows");
        let _valid_cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: valid_project_id.clone(),
        };
        let _orphan_cleanup = ProjectCleanup {
            database_url,
            project_id: orphan_project_id.clone(),
        };

        seed_project_with_child_rows(&mut conn, &valid_project_id, true);
        seed_project_with_child_rows(&mut conn, &orphan_project_id, false);

        let orphan_ids = collect_orphan_project_ids(&mut conn).expect("discover orphan projects");
        assert!(orphan_ids.contains(&orphan_project_id));
        assert!(!orphan_ids.contains(&valid_project_id));

        let counts = delete_orphan_project_sql_rows(&mut conn, &orphan_project_id)
            .expect("delete orphan rows");

        assert_eq!(
            counts,
            OrphanSqlDeletionCounts {
                symbols_deleted: 1,
                files_deleted: 1,
                content_chunks_deleted: 1,
                imports_deleted: 1,
                calls_deleted: 1,
            }
        );
        assert_eq!(project_child_row_count(&mut conn, &orphan_project_id), 0);
        assert_eq!(project_child_row_count(&mut conn, &valid_project_id), 5);
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

    fn connect_test_db() -> (postgres::Client, String) {
        let database_url = crate::test_env::postgres_test_database_url("prune tests");
        let conn = db::connect_readwrite(&database_url).expect("connect prune PostgreSQL test DB");
        (conn, database_url)
    }

    fn prune_test_context(database_url: String, project_id: &str, qdrant: bool) -> Context {
        Context {
            database_url,
            project_root: std::path::PathBuf::new(),
            project_id: project_id.to_string(),
            quiet: true,
            falkordb: None,
            qdrant: qdrant.then_some(crate::config::QdrantConfig {
                url: Some("http://127.0.0.1:1".to_string()),
                api_key: None,
            }),
            embedding: None,
            code_vectors: crate::config::CodeVectorSettings::default(),
            indexing: gobby_core::config::IndexingConfig::default(),
            daemon_url: None,
            index_scope: crate::config::ProjectIndexScope::Single,
        }
    }

    fn unique_test_project_id(prefix: &str) -> String {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system time after epoch")
            .as_nanos();
        uuid::Uuid::new_v5(
            &crate::models::CODE_INDEX_UUID_NAMESPACE,
            format!("{prefix}-{nanos}").as_bytes(),
        )
        .to_string()
    }

    fn test_uuid(conn_id: &str, label: &str) -> uuid::Uuid {
        uuid::Uuid::new_v5(
            &crate::models::CODE_INDEX_UUID_NAMESPACE,
            format!("{conn_id}:{label}").as_bytes(),
        )
    }

    fn seed_project_with_child_rows(
        conn: &mut postgres::Client,
        project_id: &str,
        include_project_row: bool,
    ) {
        let file_path = "src/lib.rs";
        let project_uuid = db::id_param(project_id).expect("test project id is a uuid");
        let file_id = test_uuid(project_id, "file");
        let symbol_id = test_uuid(project_id, "symbol");
        let chunk_id = test_uuid(project_id, "chunk");
        if include_project_row {
            conn.execute(
                "INSERT INTO code_indexed_projects
                    (id, root_path, total_files, total_symbols, last_indexed_at, index_duration_ms)
                 VALUES ($1, $2, 1, 1, NOW(), 0)",
                &[&project_uuid, &format!("/tmp/{project_id}")],
            )
            .expect("insert indexed project");
        }
        conn.execute(
            "INSERT INTO code_indexed_files
                (id, project_id, file_path, language, content_hash, symbol_count, byte_size)
             VALUES ($1, $2, $3, 'rust', 'hash-1', 1, 19)",
            &[&file_id, &project_uuid, &file_path],
        )
        .expect("insert indexed file");
        conn.execute(
            "INSERT INTO code_symbols
                (id, project_id, file_path, name, qualified_name, kind, language, byte_start,
                 byte_end, line_start, line_end, signature, docstring, parent_symbol_id,
                 content_hash, summary, created_at, updated_at)
             VALUES ($1, $2, $3, 'indexed', 'crate::indexed', 'function', 'rust', 0, 19,
                 1, 1, 'pub fn indexed()', NULL, NULL, 'hash-1', NULL, NOW(), NOW())",
            &[&symbol_id, &project_uuid, &file_path],
        )
        .expect("insert symbol");
        conn.execute(
            "INSERT INTO code_content_chunks
                (id, project_id, file_path, chunk_index, line_start, line_end, content, language)
             VALUES ($1, $2, $3, 0, 1, 1, 'pub fn indexed() {}', 'rust')",
            &[&chunk_id, &project_uuid, &file_path],
        )
        .expect("insert content chunk");
        conn.execute(
            "INSERT INTO code_imports (project_id, source_file, target_module)
             VALUES ($1, $2, 'std::fmt')",
            &[&project_uuid, &file_path],
        )
        .expect("insert import");
        conn.execute(
            "INSERT INTO code_calls
                (project_id, caller_symbol_id, callee_symbol_id, callee_name,
                 callee_target_kind, callee_external_module, file_path, line)
             VALUES ($1, $2, NULL, 'missing', 'unresolved', '', $3, 1)",
            &[&project_uuid, &symbol_id, &file_path],
        )
        .expect("insert call");
    }

    fn cleanup_project(conn: &mut postgres::Client, project_id: &str) -> anyhow::Result<()> {
        let project_id = db::id_param(project_id)?;
        conn.execute(
            "DELETE FROM code_calls WHERE project_id = $1",
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

    fn project_child_row_count(conn: &mut postgres::Client, project_id: &str) -> i64 {
        let files = count_rows(conn, "code_indexed_files", project_id);
        let symbols = count_rows(conn, "code_symbols", project_id);
        let chunks = count_rows(conn, "code_content_chunks", project_id);
        let imports = count_rows(conn, "code_imports", project_id);
        let calls = count_rows(conn, "code_calls", project_id);
        files + symbols + chunks + imports + calls
    }

    fn count_rows(conn: &mut postgres::Client, table: &str, project_id: &str) -> i64 {
        conn.query_one(
            &format!("SELECT COUNT(*)::BIGINT FROM {table} WHERE project_id = $1"),
            &[&db::id_param(project_id).expect("test project id is a uuid")],
        )
        .expect("count rows")
        .get(0)
    }
}
