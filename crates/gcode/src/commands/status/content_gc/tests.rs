use super::*;

#[test]
fn recent_git_blob_protects_matching_content() -> anyhow::Result<()> {
    let repo = tempfile::tempdir()?;
    let hooks = tempfile::tempdir()?;
    std::fs::write(repo.path().join("tracked.txt"), "retained\n")?;
    for args in [vec!["init"], vec!["add", "tracked.txt"]] {
        let status = Command::new("git")
            .arg("-C")
            .arg(repo.path())
            .args(args)
            .status()?;
        assert!(status.success());
    }
    let status = Command::new("git")
        .arg("-C")
        .arg(repo.path())
        .args([
            "-c",
            "user.name=Gcode Test",
            "-c",
            "user.email=gcode@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
        ])
        .arg(format!("core.hooksPath={}", hooks.path().display()))
        .args(["commit", "-m", "seed"])
        .status()?;
    assert!(status.success());
    // An annotated tag is listed by `rev-list --objects` under its tag name
    // even with the blob type filter; the batch reader must discard it.
    let status = Command::new("git")
        .arg("-C")
        .arg(repo.path())
        .args([
            "-c",
            "user.name=Gcode Test",
            "-c",
            "user.email=gcode@example.invalid",
            "-c",
            "tag.gpgsign=false",
            "tag",
            "-a",
            "v1",
            "-m",
            "annotated",
        ])
        .status()?;
    assert!(status.success());

    let hashes = recent_content_hashes_in_git_history(repo.path(), 17)?;
    assert!(hashes.contains(&hasher::content_hash(b"retained\n")));
    assert!(!hashes.contains(&hasher::content_hash(b"different\n")));
    Ok(())
}

mod serial_db {
    use std::cell::RefCell;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    use super::*;
    use crate::index::api;

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn adoption_refreshes_last_reference_and_shields_content_from_gc() {
        let (mut conn, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-gc-adoption");
        cleanup_project(&mut conn, &project_id).expect("pre-clean project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };
        let root = git_init_root();
        seed_project(&mut conn, &project_id, root.path());
        seed_content_version(&mut conn, &project_id, "src/lib.rs", "gc-hash-v1", 60, true);
        seed_content_version(&mut conn, &project_id, "src/lib.rs", "gc-hash-v2", 60, true);

        assert_eq!(
            candidate_hashes(&database_url, &project_id),
            vec!["gc-hash-v1".to_string(), "gc-hash-v2".to_string()],
            "old unreferenced versions start GC-eligible",
        );

        let machine_id = gobby_core::machine::read_local_machine_id().expect("local machine id");
        assert!(
            api::adopt_file_state(
                &mut conn,
                &machine_id,
                &project_id,
                "src/lib.rs",
                "gc-hash-v1"
            )
            .expect("adopt v1")
        );
        assert_eq!(
            candidate_hashes(&database_url, &project_id),
            vec!["gc-hash-v2".to_string()],
            "adopted content is referenced and leaves the GC set",
        );

        assert!(
            api::adopt_file_state(
                &mut conn,
                &machine_id,
                &project_id,
                "src/lib.rs",
                "gc-hash-v2"
            )
            .expect("adopt v2")
        );
        assert_eq!(
            candidate_hashes(&database_url, &project_id),
            Vec::<String>::new(),
            "recently adopted content survives GC despite old indexed_at",
        );

        let row = conn
            .query_one(
                "SELECT last_referenced_at > NOW() - INTERVAL '1 hour' AS recently_referenced,
                        indexed_at < NOW() - INTERVAL '59 days' AS written_long_ago
                 FROM code_indexed_files
                 WHERE project_id = $1 AND file_path = 'src/lib.rs' AND content_hash = 'gc-hash-v1'",
                &[&db::id_param(&project_id).expect("project uuid")],
            )
            .expect("read v1 timestamps");
        assert!(row.get::<_, bool>("recently_referenced"));
        assert!(row.get::<_, bool>("written_long_ago"));
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn prune_collects_unreferenced_content_and_spares_any_machine_reference() {
        let (mut conn, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-gc-prune");
        cleanup_project(&mut conn, &project_id).expect("pre-clean project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };
        let root = git_init_root();
        seed_project(&mut conn, &project_id, root.path());
        let expired_id = seed_content_version(
            &mut conn,
            &project_id,
            "src/lib.rs",
            "gc-hash-old",
            60,
            true,
        );
        let foreign_id = seed_content_version(
            &mut conn,
            &project_id,
            "src/keep.rs",
            "gc-hash-keep",
            60,
            true,
        );
        let foreign_machine = uuid::Uuid::new_v4();
        crate::test_env::seed_test_machine(&mut conn, &foreign_machine.to_string())
            .expect("seed foreign machine");
        seed_file_state(
            &mut conn,
            foreign_machine,
            &project_id,
            "src/keep.rs",
            "gc-hash-keep",
        );

        let discovered = discover_content_gc(&database_url, 17, Some(&project_id))
            .expect("discover GC candidates");
        assert_eq!(
            discovered
                .iter()
                .map(|candidate| candidate.id.as_str())
                .collect::<Vec<_>>(),
            vec![expired_id.as_str()],
            "content referenced by any machine never becomes a candidate",
        );

        // A stale candidate for still-referenced content must survive pruning.
        let mut candidates = discovered;
        candidates.push(ContentGcCandidate {
            id: foreign_id.clone(),
            project_id: project_id.clone(),
            file_path: "src/keep.rs".to_string(),
            content_hash: "gc-hash-keep".to_string(),
            symbol_ids: Vec::new(),
            has_graph_facts: false,
            graph_synced: true,
            vectors_synced: true,
        });
        let services = test_context(&database_url, &project_id);
        let totals = prune_content_versions_with(
            &services,
            &candidates,
            None,
            |project_id| Ok(test_context(&database_url, project_id)),
            delete_candidate_projections,
            code_graph::cleanup_orphans,
        )
        .expect("prune content versions");

        assert_eq!(totals.deleted_versions, 1);
        assert_eq!(totals.failed_versions, 0);
        assert_eq!(content_row_count(&mut conn, &expired_id), 0);
        assert_eq!(content_row_count(&mut conn, &foreign_id), 1);
        let row = conn
            .query_one(
                "SELECT graph_synced, vectors_synced FROM code_indexed_files WHERE id = $1",
                &[&db::id_param(&foreign_id).expect("file uuid")],
            )
            .expect("read referenced row flags");
        assert!(row.get::<_, bool>("graph_synced"));
        assert!(row.get::<_, bool>("vectors_synced"));
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn projection_delete_failure_retains_pending_cleanup_state() {
        let (mut conn, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-gc-projection-fail");
        cleanup_project(&mut conn, &project_id).expect("pre-clean project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };
        let root = git_init_root();
        seed_project(&mut conn, &project_id, root.path());
        let file_id = seed_content_version(
            &mut conn,
            &project_id,
            "src/lib.rs",
            "gc-hash-fail",
            60,
            true,
        );
        seed_symbol(&mut conn, &project_id, "src/lib.rs", "gc-hash-fail");

        let candidates = discover_content_gc(&database_url, 17, Some(&project_id))
            .expect("discover GC candidates");
        assert_eq!(candidates.len(), 1);
        assert!(!candidates[0].symbol_ids.is_empty());

        let services = test_context(&database_url, &project_id);
        let swept = RefCell::new(Vec::new());
        let totals = prune_content_versions_with(
            &services,
            &candidates,
            None,
            |project_id| {
                let mut ctx = test_context(&database_url, project_id);
                ctx.falkordb = Some(crate::config::FalkorConfig {
                    host: "127.0.0.1".to_string(),
                    port: 1,
                    password: None,
                    graph_name: "gcode_gc_test".to_string(),
                });
                // Both stores must be configured so the skip gate does not retain
                // the candidate before the graph delete gets to fail.
                ctx.qdrant = Some(crate::config::QdrantConfig {
                    url: Some("http://127.0.0.1:1".to_string()),
                    api_key: None,
                });
                Ok(ctx)
            },
            delete_candidate_projections,
            |ctx: &Context| {
                swept.borrow_mut().push(ctx.project_id.clone());
                Ok(())
            },
        )
        .expect("prune isolates projection failures");

        assert_eq!(totals.deleted_versions, 0);
        assert_eq!(totals.failed_versions, 1);
        assert!(
            swept.into_inner().is_empty(),
            "a failed projection delete leaves nothing to sweep"
        );
        assert_eq!(content_row_count(&mut conn, &file_id), 1);
        let row = conn
            .query_one(
                "SELECT graph_synced, vectors_synced FROM code_indexed_files WHERE id = $1",
                &[&db::id_param(&file_id).expect("file uuid")],
            )
            .expect("read retained flags");
        assert!(row.get::<_, bool>("graph_synced"));
        assert!(row.get::<_, bool>("vectors_synced"));
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn unconfigured_projection_store_retains_synced_content() {
        let (mut conn, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-gc-store-skip");
        cleanup_project(&mut conn, &project_id).expect("pre-clean project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };
        let root = git_init_root();
        seed_project(&mut conn, &project_id, root.path());
        let synced_id = seed_content_version(
            &mut conn,
            &project_id,
            "src/lib.rs",
            "gc-hash-synced",
            60,
            true,
        );
        seed_symbol(&mut conn, &project_id, "src/lib.rs", "gc-hash-synced");
        let unsynced_id = seed_content_version(
            &mut conn,
            &project_id,
            "src/other.rs",
            "gc-hash-unsynced",
            60,
            false,
        );
        seed_symbol(&mut conn, &project_id, "src/other.rs", "gc-hash-unsynced");
        let import_only_id = seed_content_version(
            &mut conn,
            &project_id,
            "src/imports.rs",
            "gc-hash-imports",
            60,
            true,
        );
        seed_import(&mut conn, &project_id, "src/imports.rs", "gc-hash-imports");
        let call_only_id = seed_content_version(
            &mut conn,
            &project_id,
            "src/calls.rs",
            "gc-hash-calls",
            60,
            true,
        );
        seed_call(&mut conn, &project_id, "src/calls.rs", "gc-hash-calls");

        let candidates = discover_content_gc(&database_url, 17, Some(&project_id))
            .expect("discover GC candidates");
        assert_eq!(candidates.len(), 4);
        for file_path in ["src/imports.rs", "src/calls.rs"] {
            let candidate = candidates
                .iter()
                .find(|candidate| candidate.file_path == file_path)
                .expect("relation-only content candidate");
            assert!(candidate.symbol_ids.is_empty());
            assert!(candidate.has_graph_facts);
        }

        // test_context configures neither FalkorDB nor Qdrant.
        let services = test_context(&database_url, &project_id);
        let totals = prune_content_versions_with(
            &services,
            &candidates,
            None,
            |project_id| Ok(test_context(&database_url, project_id)),
            delete_candidate_projections,
            code_graph::cleanup_orphans,
        )
        .expect("prune content versions");

        assert_eq!(totals.skipped_versions, 3);
        assert_eq!(totals.deleted_versions, 1);
        assert_eq!(totals.failed_versions, 0);
        // The synced version keeps its row and flags for a machine that can
        // reach the stores; the never-projected version is deleted.
        assert_eq!(content_row_count(&mut conn, &synced_id), 1);
        assert_eq!(content_row_count(&mut conn, &unsynced_id), 0);
        assert_eq!(content_row_count(&mut conn, &import_only_id), 1);
        assert_eq!(content_row_count(&mut conn, &call_only_id), 1);
        let row = conn
            .query_one(
                "SELECT graph_synced, vectors_synced FROM code_indexed_files WHERE id = $1",
                &[&db::id_param(&synced_id).expect("file uuid")],
            )
            .expect("read retained row flags");
        assert!(row.get::<_, bool>("graph_synced"));
        assert!(row.get::<_, bool>("vectors_synced"));
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn prune_resolves_projection_services_per_candidate_project() {
        let (mut conn, database_url) = connect_test_db();
        let first_project = unique_test_project_id("gcode-gc-services-a");
        let second_project = unique_test_project_id("gcode-gc-services-b");
        for project_id in [&first_project, &second_project] {
            cleanup_project(&mut conn, project_id).expect("pre-clean project rows");
        }
        let _cleanups = [&first_project, &second_project].map(|project_id| ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        });
        let root = git_init_root();
        let mut candidates = Vec::new();
        for project_id in [&first_project, &second_project] {
            seed_project(&mut conn, project_id, root.path());
            let file_id =
                seed_content_version(&mut conn, project_id, "src/lib.rs", "gc-hash-old", 60, true);
            candidates.push(ContentGcCandidate {
                id: file_id,
                project_id: project_id.clone(),
                file_path: "src/lib.rs".to_string(),
                content_hash: "gc-hash-old".to_string(),
                symbol_ids: Vec::new(),
                has_graph_facts: false,
                graph_synced: true,
                vectors_synced: true,
            });
        }

        let resolved = RefCell::new(Vec::new());
        let services = test_context(&database_url, &first_project);
        let totals = prune_content_versions_with(
            &services,
            &candidates,
            None,
            |project_id| {
                resolved.borrow_mut().push(project_id.to_string());
                Ok(test_context(&database_url, project_id))
            },
            delete_candidate_projections,
            code_graph::cleanup_orphans,
        )
        .expect("prune content versions");

        assert_eq!(totals.deleted_versions, 2);
        assert_eq!(
            resolved.into_inner(),
            vec![first_project.clone(), second_project.clone()],
            "each candidate project resolves its own service context exactly once",
        );
        for candidate in &candidates {
            assert_eq!(content_row_count(&mut conn, &candidate.id), 0);
        }
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn expired_time_budget_defers_every_remaining_candidate() {
        let (mut conn, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-gc-deadline");
        cleanup_project(&mut conn, &project_id).expect("pre-clean project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };
        let root = git_init_root();
        seed_project(&mut conn, &project_id, root.path());
        let file_id = seed_content_version(
            &mut conn,
            &project_id,
            "src/lib.rs",
            "gc-hash-late",
            60,
            true,
        );
        let candidates = discover_content_gc(&database_url, 17, Some(&project_id))
            .expect("discover GC candidates");
        assert_eq!(candidates.len(), 1);

        let services = test_context(&database_url, &project_id);
        let totals = prune_content_versions_with(
            &services,
            &candidates,
            Some(Instant::now()),
            |project_id| Ok(test_context(&database_url, project_id)),
            |_: &Context, _: &ContentGcCandidate| {
                panic!("an expired budget must not touch projections")
            },
            |_: &Context| panic!("an expired budget leaves nothing to sweep"),
        )
        .expect("prune honours the time budget");

        assert_eq!(totals.deferred_versions, 1);
        assert_eq!(totals.deleted_versions, 0);
        assert_eq!(content_row_count(&mut conn, &file_id), 1);
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn graph_orphans_are_swept_once_per_project_after_its_deletions() {
        let (mut conn, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-gc-sweep");
        cleanup_project(&mut conn, &project_id).expect("pre-clean project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };
        let root = git_init_root();
        seed_project(&mut conn, &project_id, root.path());
        for content_hash in ["gc-hash-sweep-a", "gc-hash-sweep-b"] {
            seed_content_version(&mut conn, &project_id, "src/lib.rs", content_hash, 60, true);
            seed_symbol(&mut conn, &project_id, "src/lib.rs", content_hash);
        }
        let candidates = discover_content_gc(&database_url, 17, Some(&project_id))
            .expect("discover GC candidates");
        assert_eq!(candidates.len(), 2);
        assert!(candidates.iter().all(|candidate| candidate.has_graph_facts));

        let deleted = RefCell::new(Vec::new());
        let swept = RefCell::new(Vec::new());
        let services = test_context(&database_url, &project_id);
        let totals = prune_content_versions_with(
            &services,
            &candidates,
            None,
            |project_id| {
                let mut ctx = test_context(&database_url, project_id);
                // Both stores are "configured" so the skip gate lets the
                // candidates through; the injected delete never contacts them.
                ctx.falkordb = Some(crate::config::FalkorConfig {
                    host: "127.0.0.1".to_string(),
                    port: 1,
                    password: None,
                    graph_name: "gcode_gc_test".to_string(),
                });
                ctx.qdrant = Some(crate::config::QdrantConfig {
                    url: Some("http://127.0.0.1:1".to_string()),
                    api_key: None,
                });
                Ok(ctx)
            },
            |_: &Context, candidate: &ContentGcCandidate| {
                deleted.borrow_mut().push(candidate.content_hash.clone());
                Ok(())
            },
            |ctx: &Context| {
                swept.borrow_mut().push(ctx.project_id.clone());
                Ok(())
            },
        )
        .expect("prune content versions");

        assert_eq!(totals.deleted_versions, 2);
        assert_eq!(totals.orphan_sweep_failures, 0);
        assert_eq!(deleted.into_inner().len(), 2);
        assert_eq!(
            swept.into_inner(),
            vec![project_id.clone()],
            "one orphan sweep per project, after every version was deleted",
        );
        for candidate in &candidates {
            assert_eq!(content_row_count(&mut conn, &candidate.id), 0);
        }
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn budgeted_run_commits_progress_so_a_rerun_sees_fewer_candidates() {
        let (mut conn, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-gc-progress");
        cleanup_project(&mut conn, &project_id).expect("pre-clean project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };
        let root = git_init_root();
        seed_project(&mut conn, &project_id, root.path());
        for content_hash in ["gc-hash-progress-a", "gc-hash-progress-b"] {
            seed_content_version(&mut conn, &project_id, "src/lib.rs", content_hash, 60, true);
        }
        let candidates = discover_content_gc(&database_url, 17, Some(&project_id))
            .expect("discover GC candidates");
        assert_eq!(candidates.len(), 2);

        // The first delete outlives the budget, so the second candidate is
        // deferred while the first stays committed.
        let budget = Duration::from_millis(50);
        let services = test_context(&database_url, &project_id);
        let totals = prune_content_versions_with(
            &services,
            &candidates,
            Some(Instant::now() + budget),
            |project_id| Ok(test_context(&database_url, project_id)),
            |_: &Context, _: &ContentGcCandidate| {
                std::thread::sleep(budget * 3);
                Ok(())
            },
            |_: &Context| panic!("no graph store is configured, nothing to sweep"),
        )
        .expect("prune honours the time budget");
        assert_eq!(totals.deleted_versions, 1);
        assert_eq!(totals.deferred_versions, 1);

        let remaining = discover_content_gc(&database_url, 17, Some(&project_id))
            .expect("rediscover GC candidates");
        assert_eq!(
            remaining.len(),
            1,
            "the interrupted run's deletion is committed, so a rerun has less to do",
        );
        let key = crate::index_lock::project_lock_key(&project_id);
        let unheld: bool = conn
            .query_one("SELECT pg_try_advisory_lock($1)", &[&key])
            .expect("probe project advisory lock")
            .get(0);
        assert!(
            unheld,
            "an interrupted run never leaves the project advisory lock held"
        );
        conn.query_one("SELECT pg_advisory_unlock($1)", &[&key])
            .expect("release probe lock");

        let totals = prune_content_versions_with(
            &services,
            &remaining,
            None,
            |project_id| Ok(test_context(&database_url, project_id)),
            delete_candidate_projections,
            code_graph::cleanup_orphans,
        )
        .expect("rerun finishes the backlog");
        assert_eq!(totals.deleted_versions, 1);
        assert!(
            discover_content_gc(&database_url, 17, Some(&project_id))
                .expect("final discovery")
                .is_empty()
        );
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn inheritance_only_content_has_graph_facts() {
        let (mut conn, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-gc-inherit-only");
        cleanup_project(&mut conn, &project_id).expect("pre-clean project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };
        let root = git_init_root();
        seed_project(&mut conn, &project_id, root.path());
        seed_content_version(
            &mut conn,
            &project_id,
            "src/heritage.rs",
            "gc-hash-inherit",
            60,
            true,
        );
        seed_inheritance(&mut conn, &project_id, "src/heritage.rs", "gc-hash-inherit");

        let candidates = discover_content_gc(&database_url, 17, Some(&project_id))
            .expect("discover GC candidates");
        let candidate = candidates
            .iter()
            .find(|candidate| candidate.file_path == "src/heritage.rs")
            .expect("inheritance-only content candidate");
        assert!(candidate.symbol_ids.is_empty());
        assert!(
            candidate.has_graph_facts,
            "inheritance-only content must participate in projection deletion before PostgreSQL GC"
        );
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
        let database_url = crate::test_env::postgres_test_database_url("content GC tests");
        let conn =
            db::connect_readwrite(&database_url).expect("connect content GC PostgreSQL test DB");
        (conn, database_url)
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

    fn git_init_root() -> tempfile::TempDir {
        let root = tempfile::tempdir().expect("create scratch git root");
        let status = Command::new("git")
            .arg("-C")
            .arg(root.path())
            .arg("init")
            .status()
            .expect("run git init");
        assert!(status.success());
        root
    }

    fn seed_project(conn: &mut postgres::Client, project_id: &str, root: &Path) {
        let project_uuid = db::id_param(project_id).expect("test project id is a uuid");
        let machine_id = db::id_param(
            &gobby_core::machine::read_local_machine_id().expect("read local machine id"),
        )
        .expect("local machine id is a uuid");
        conn.execute(
            "INSERT INTO code_indexed_projects (id) VALUES ($1)",
            &[&project_uuid],
        )
        .expect("insert indexed project identity");
        conn.execute(
            "INSERT INTO code_indexed_project_states
                (machine_id, project_id, root_path, total_files, total_symbols,
                 last_indexed_at, index_duration_ms)
             VALUES ($1, $2, $3, 1, 1, NOW(), 0)",
            &[
                &machine_id,
                &project_uuid,
                &root.to_string_lossy().to_string(),
            ],
        )
        .expect("insert indexed project state");
    }

    fn seed_content_version(
        conn: &mut postgres::Client,
        project_id: &str,
        file_path: &str,
        content_hash: &str,
        days_old: i32,
        synced: bool,
    ) -> String {
        let project_uuid = db::id_param(project_id).expect("test project id is a uuid");
        let file_id = uuid::Uuid::new_v5(
            &crate::models::CODE_INDEX_UUID_NAMESPACE,
            format!("{project_id}:{file_path}:{content_hash}").as_bytes(),
        );
        conn.execute(
            "INSERT INTO code_indexed_files
                (id, project_id, file_path, language, content_hash, symbol_count, byte_size,
                 graph_synced, vectors_synced, indexed_at, last_referenced_at)
             VALUES ($1, $2, $3, 'rust', $4, 1, 19, $5, $5,
                 NOW() - make_interval(days => $6),
                 NOW() - make_interval(days => $6))",
            &[
                &file_id,
                &project_uuid,
                &file_path,
                &content_hash,
                &synced,
                &days_old,
            ],
        )
        .expect("insert content version");
        file_id.to_string()
    }

    fn seed_file_state(
        conn: &mut postgres::Client,
        machine_id: uuid::Uuid,
        project_id: &str,
        file_path: &str,
        content_hash: &str,
    ) {
        let project_uuid = db::id_param(project_id).expect("test project id is a uuid");
        conn.execute(
            "INSERT INTO code_indexed_project_states
                (machine_id, project_id, root_path, total_files, total_symbols,
                 last_indexed_at, index_duration_ms)
             VALUES ($1, $2, '/nonexistent/foreign-machine-root', 1, 1, NOW(), 0)
             ON CONFLICT (machine_id, project_id) DO NOTHING",
            &[&machine_id, &project_uuid],
        )
        .expect("insert project state for file-state machine");
        conn.execute(
            "INSERT INTO code_indexed_file_states
                (machine_id, project_id, file_path, content_hash)
             VALUES ($1, $2, $3, $4)",
            &[&machine_id, &project_uuid, &file_path, &content_hash],
        )
        .expect("insert file state");
    }

    fn seed_symbol(
        conn: &mut postgres::Client,
        project_id: &str,
        file_path: &str,
        content_hash: &str,
    ) {
        let project_uuid = db::id_param(project_id).expect("test project id is a uuid");
        let symbol_id = uuid::Uuid::new_v5(
            &crate::models::CODE_INDEX_UUID_NAMESPACE,
            format!("{project_id}:{file_path}:{content_hash}:symbol").as_bytes(),
        );
        conn.execute(
            "INSERT INTO code_symbols
                (id, project_id, file_path, name, qualified_name, kind, language, byte_start,
                 byte_end, line_start, line_end, signature, docstring, parent_symbol_id,
                 file_content_hash, content_hash, summary, created_at, updated_at)
             VALUES ($1, $2, $3, 'expired', 'crate::expired', 'function', 'rust', 0, 19,
                 1, 1, 'pub fn expired()', NULL, NULL, $4, $4, NULL, NOW(), NOW())",
            &[&symbol_id, &project_uuid, &file_path, &content_hash],
        )
        .expect("insert symbol");
    }

    fn seed_import(
        conn: &mut postgres::Client,
        project_id: &str,
        file_path: &str,
        content_hash: &str,
    ) {
        let project_uuid = db::id_param(project_id).expect("test project id is a uuid");
        conn.execute(
            "INSERT INTO code_imports (project_id, source_file, content_hash, target_module)
         VALUES ($1, $2, $3, 'std::fmt')",
            &[&project_uuid, &file_path, &content_hash],
        )
        .expect("insert import");
    }

    fn seed_inheritance(
        conn: &mut postgres::Client,
        project_id: &str,
        file_path: &str,
        content_hash: &str,
    ) {
        let project_uuid = db::id_param(project_id).expect("test project id is a uuid");
        conn.execute(
            "INSERT INTO code_inheritance
            (project_id, source_symbol_id, source_name, source_kind, source_external_module,
             target_symbol_id, target_name, target_kind, target_external_module,
             heritage_kind, file_path, content_hash, line)
         VALUES ($1, NULL, 'Derived', 'unresolved', '', NULL, 'Base', 'unresolved', '',
                 'EXTENDS', $2, $3, 1)",
            &[&project_uuid, &file_path, &content_hash],
        )
        .expect("insert inheritance");
    }

    fn seed_call(
        conn: &mut postgres::Client,
        project_id: &str,
        file_path: &str,
        content_hash: &str,
    ) {
        let project_uuid = db::id_param(project_id).expect("test project id is a uuid");
        conn.execute(
            "INSERT INTO code_calls
            (project_id, caller_symbol_id, callee_symbol_id, callee_name,
             callee_target_kind, callee_external_module, file_path, content_hash, line)
         VALUES ($1, NULL, NULL, 'missing', 'unresolved', '', $2, $3, 1)",
            &[&project_uuid, &file_path, &content_hash],
        )
        .expect("insert call");
    }

    fn candidate_hashes(database_url: &str, project_id: &str) -> Vec<String> {
        discover_content_gc(database_url, 17, Some(project_id))
            .expect("discover GC candidates")
            .into_iter()
            .map(|candidate| candidate.content_hash)
            .collect()
    }

    fn content_row_count(conn: &mut postgres::Client, file_id: &str) -> i64 {
        conn.query_one(
            "SELECT COUNT(*)::BIGINT FROM code_indexed_files WHERE id = $1",
            &[&db::id_param(file_id).expect("file id is a uuid")],
        )
        .expect("count content rows")
        .get(0)
    }

    fn test_context(database_url: &str, project_id: &str) -> Context {
        Context {
            database_url: database_url.to_string(),
            project_root: PathBuf::new(),
            project_id: project_id.to_string(),
            quiet: true,
            falkordb: None,
            qdrant: None,
            embedding: None,
            code_vectors: crate::config::CodeVectorSettings::default(),
            runtime_config_capture_degraded: false,
            indexing: gobby_core::config::IndexingConfig::default(),
            daemon_url: None,
            grant_ai: None,
            index_scope: crate::config::ProjectIndexScope::Single,
        }
    }

    fn cleanup_project(conn: &mut postgres::Client, project_id: &str) -> anyhow::Result<()> {
        let project_id = db::id_param(project_id)?;
        for statement in [
            "DELETE FROM code_indexed_file_states WHERE project_id = $1",
            "DELETE FROM code_indexed_project_states WHERE project_id = $1",
            "DELETE FROM code_symbols WHERE project_id = $1",
            "DELETE FROM code_content_chunks WHERE project_id = $1",
            "DELETE FROM code_indexed_files WHERE project_id = $1",
            "DELETE FROM code_indexed_projects WHERE id = $1",
        ] {
            conn.execute(statement, &[&project_id])?;
        }
        Ok(())
    }
}
