use super::*;
use std::path::Path;
use std::process::Command;

use crate::index::hasher;

// Commits every change under `root` with a fixed identity and no hooks.
fn commit_fixture(root: &Path) {
    let status = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(["add", "-A"])
        .status()
        .expect("run git add");
    assert!(status.success());
    let status = Command::new("git")
        .arg("-C")
        .arg(root)
        .args([
            "-c",
            "user.name=Gcode Test",
            "-c",
            "user.email=gcode@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-q",
            "-m",
            "seed",
        ])
        .status()
        .expect("run git commit");
    assert!(status.success());
}

mod serial_db {
    use std::cell::RefCell;
    use std::time::{SystemTime, UNIX_EPOCH};

    use super::*;
    use crate::index::api;

    fn candidate_versions(database_url: &str, project_id: &str) -> Vec<(String, String)> {
        discover_content_gc(database_url, 17, Some(project_id))
            .expect("discover GC candidates")
            .into_iter()
            .map(|candidate| (candidate.file_path, candidate.content_hash))
            .collect()
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn recent_history_protects_only_the_path_it_holds() {
        let (mut conn, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-gc-path-history");
        cleanup_project(&mut conn, &project_id).expect("pre-clean project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };
        let root = git_init_root();
        std::fs::create_dir_all(root.path().join("src")).expect("create history directory");
        std::fs::write(root.path().join("src/lib.rs"), "retained\n").expect("write history file");
        commit_fixture(root.path());
        seed_project(&mut conn, &project_id, root.path());
        seed_primary_checkout(&mut conn, &project_id, root.path());
        let retained = hasher::content_hash(b"retained\n");
        // Identical bytes at src/lib.rs, which history holds, and at
        // src/moved.rs, which history never held.
        seed_content_version(&mut conn, &project_id, "src/lib.rs", &retained, 60, true);
        seed_content_version(&mut conn, &project_id, "src/moved.rs", &retained, 60, true);

        assert_eq!(
            candidate_versions(&database_url, &project_id),
            vec![("src/moved.rs".to_string(), retained)],
            "history protects a version only at the path where it holds it",
        );
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn history_failure_retains_root_candidates() {
        let (mut conn, database_url) = connect_test_db();
        let failing_id = unique_test_project_id("gcode-gc-history-failure");
        let readable_id = unique_test_project_id("gcode-gc-history-readable");
        cleanup_project(&mut conn, &failing_id).expect("pre-clean failing project rows");
        cleanup_project(&mut conn, &readable_id).expect("pre-clean readable project rows");
        let _failing_cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: failing_id.clone(),
        };
        let _readable_cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: readable_id.clone(),
        };
        // Git cannot read history for a root that does not exist.
        let scratch = tempfile::tempdir().expect("create scratch directory");
        let missing_root = scratch.path().join("missing-root");
        seed_project(&mut conn, &failing_id, &missing_root);
        seed_primary_checkout(&mut conn, &failing_id, &missing_root);
        seed_content_version(
            &mut conn,
            &failing_id,
            "src/lib.rs",
            "gc-hash-lib",
            60,
            true,
        );
        seed_content_version(
            &mut conn,
            &failing_id,
            "src/main.rs",
            "gc-hash-main",
            60,
            true,
        );
        // Readable history that lacks these paths leaves the candidate eligible, so the
        // empty result for the failing root comes from retention, not from discovery.
        let readable_root = git_init_root();
        std::fs::write(readable_root.path().join("other.txt"), "other\n")
            .expect("write unrelated file");
        commit_fixture(readable_root.path());
        seed_project(&mut conn, &readable_id, readable_root.path());
        seed_primary_checkout(&mut conn, &readable_id, readable_root.path());
        seed_content_version(
            &mut conn,
            &readable_id,
            "src/lib.rs",
            "gc-hash-lib",
            60,
            true,
        );

        assert_eq!(
            candidate_hashes(&database_url, &readable_id),
            vec!["gc-hash-lib".to_string()],
            "readable history without these paths leaves the candidate eligible",
        );
        assert_eq!(
            candidate_hashes(&database_url, &failing_id),
            Vec::<String>::new(),
            "unreadable history retains every candidate of its root",
        );
    }

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
        seed_primary_checkout(&mut conn, &project_id, root.path());
        seed_content_version(&mut conn, &project_id, "src/lib.rs", "gc-hash-v1", 60, true);
        seed_content_version(&mut conn, &project_id, "src/lib.rs", "gc-hash-v2", 60, true);

        assert_eq!(
            candidate_hashes(&database_url, &project_id),
            vec!["gc-hash-v1".to_string(), "gc-hash-v2".to_string()],
            "old unreferenced versions start GC-eligible",
        );

        let machine_id = gobby_core::machine::read_local_machine_id().expect("local machine id");
        assert!(
            api::file_state::adopt_file_state(
                &mut conn,
                &machine_id,
                &project_id,
                "src/lib.rs",
                "gc-hash-v1",
                root.path(),
                api::IndexWriteMode::Primary,
            )
            .expect("adopt v1")
        );
        assert_eq!(
            candidate_hashes(&database_url, &project_id),
            vec!["gc-hash-v2".to_string()],
            "adopted content is referenced and leaves the GC set",
        );

        assert!(
            api::file_state::adopt_file_state(
                &mut conn,
                &machine_id,
                &project_id,
                "src/lib.rs",
                "gc-hash-v2",
                root.path(),
                api::IndexWriteMode::Primary,
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
            || false,
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
            || false,
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
            || false,
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
            || false,
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
            || true,
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
            || false,
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

        // Allow the first candidate through, then expire the deterministic
        // budget before the second candidate starts.
        let deadline_checks = std::cell::Cell::new(0);
        let services = test_context(&database_url, &project_id);
        let totals = prune_content_versions_with(
            &services,
            &candidates,
            || {
                let checks = deadline_checks.get();
                deadline_checks.set(checks + 1);
                checks > 0
            },
            |project_id| Ok(test_context(&database_url, project_id)),
            |_: &Context, _: &ContentGcCandidate| Ok(()),
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
            || false,
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

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn pinned_version_survives_prune() {
        let (mut conn, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-gc-pinned");
        cleanup_project(&mut conn, &project_id).expect("pre-clean project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };
        let root = git_init_root();
        seed_project(&mut conn, &project_id, root.path());
        let pinned_id = seed_content_version(
            &mut conn,
            &project_id,
            "src/lib.rs",
            "gc-hash-pinned",
            60,
            true,
        );
        let overlay_id = uuid::Uuid::new_v4().to_string();
        seed_overlay_pin(
            &mut conn,
            &overlay_id,
            &project_id,
            "src/lib.rs",
            "gc-hash-pinned",
        );

        let discovered = discover_content_gc(&database_url, 17, Some(&project_id))
            .expect("discover GC candidates");
        assert!(
            discovered.is_empty(),
            "a version an overlay pin names is never a GC candidate",
        );

        let pinned = ContentGcCandidate {
            id: pinned_id.clone(),
            project_id: project_id.clone(),
            file_path: "src/lib.rs".to_string(),
            content_hash: "gc-hash-pinned".to_string(),
            symbol_ids: Vec::new(),
            has_graph_facts: false,
            graph_synced: true,
            vectors_synced: true,
        };
        let services = test_context(&database_url, &project_id);
        let totals = prune_content_versions_with(
            &services,
            std::slice::from_ref(&pinned),
            || false,
            |project_id| Ok(test_context(&database_url, project_id)),
            delete_candidate_projections,
            code_graph::cleanup_orphans,
        )
        .expect("prune must skip a pinned version instead of aborting");
        assert_eq!(totals.deleted_versions, 0);
        assert_eq!(totals.failed_versions, 0);
        assert_eq!(content_row_count(&mut conn, &pinned_id), 1);

        // The daemon prunes the source project under a principal scoped to it. Row-level
        // security hides pin rows owned by other overlays from that principal, yet the
        // pin still roots the version: prune skips it instead of failing the delete on
        // code_overlay_pins_source_content_fkey.
        seed_primary_checkout(&mut conn, &project_id, root.path());
        let principal = ScopedPrincipal::issue(&mut conn, &database_url, &project_id);
        let scoped_services = test_context(&principal.database_url, &project_id);
        let scoped_totals = prune_content_versions_with(
            &scoped_services,
            std::slice::from_ref(&pinned),
            || false,
            |project_id| Ok(test_context(&principal.database_url, project_id)),
            delete_candidate_projections,
            code_graph::cleanup_orphans,
        )
        .unwrap_or_else(|error| {
            panic!("scoped prune must skip a pinned version instead of aborting: {error:#}")
        });
        assert_eq!(scoped_totals.deleted_versions, 0);
        assert_eq!(scoped_totals.failed_versions, 0);
        assert_eq!(content_row_count(&mut conn, &pinned_id), 1);
        assert!(
            discover_content_gc(&principal.database_url, 17, Some(&project_id))
                .expect("discover GC candidates as the scoped principal")
                .is_empty(),
            "a version another overlay pins is never a candidate for the scoped principal",
        );
        drop(principal);

        conn.execute(
            "DELETE FROM code_overlay_pins WHERE overlay_project_id = $1",
            &[&db::id_param(&overlay_id).expect("overlay pin uuid")],
        )
        .expect("release overlay pin");
        let totals = prune_content_versions_with(
            &services,
            std::slice::from_ref(&pinned),
            || false,
            |project_id| Ok(test_context(&database_url, project_id)),
            delete_candidate_projections,
            code_graph::cleanup_orphans,
        )
        .expect("prune an unpinned version");
        assert_eq!(totals.deleted_versions, 1);
        assert_eq!(content_row_count(&mut conn, &pinned_id), 0);
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn source_pin_visibility_preserves_write_scope() {
        let (mut owner, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-gc-pin-visibility");
        let unrelated_id = unique_test_project_id("gcode-gc-pin-unrelated");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };
        let _unrelated_cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: unrelated_id.clone(),
        };
        let root = git_init_root();
        let unrelated_root = git_init_root();
        seed_project(&mut owner, &project_id, root.path());
        seed_project(&mut owner, &unrelated_id, unrelated_root.path());
        seed_primary_checkout(&mut owner, &project_id, root.path());
        seed_content_version(&mut owner, &project_id, "src/lib.rs", "pinned", 60, true);
        seed_content_version(
            &mut owner,
            &unrelated_id,
            "src/lib.rs",
            "unrelated",
            60,
            true,
        );
        let overlay = uuid::Uuid::new_v4().to_string();
        let remote_overlay = uuid::Uuid::new_v4().to_string();
        let unrelated_overlay = uuid::Uuid::new_v4().to_string();
        seed_overlay_pin(&mut owner, &overlay, &project_id, "src/lib.rs", "pinned");
        seed_overlay_pin(
            &mut owner,
            &remote_overlay,
            &project_id,
            "src/lib.rs",
            "pinned",
        );
        seed_overlay_pin(
            &mut owner,
            &unrelated_overlay,
            &unrelated_id,
            "src/lib.rs",
            "unrelated",
        );
        owner
            .execute(
                "UPDATE code_overlay_pins SET machine_id = $1 WHERE overlay_project_id = $2",
                &[
                    &uuid::Uuid::new_v4(),
                    &db::id_param(&remote_overlay).unwrap(),
                ],
            )
            .expect("move pin to another machine");
        let principal = ScopedPrincipal::issue(&mut owner, &database_url, &project_id);
        let mut scoped = db::connect_readwrite(&principal.database_url).expect("scoped connection");
        let project_uuid = db::id_param(&project_id).unwrap();
        let visible: i64 = scoped
            .query_one(
                "SELECT count(*) FROM code_overlay_pins WHERE source_project_id = $1",
                &[&project_uuid],
            )
            .expect("read source pins")
            .get(0);
        assert_eq!(visible, 2, "source retention sees pins on every machine");
        let unrelated: i64 = scoped
            .query_one(
                "SELECT count(*) FROM code_overlay_pins WHERE source_project_id = $1",
                &[&db::id_param(&unrelated_id).unwrap()],
            )
            .expect("query unrelated pins")
            .get(0);
        assert_eq!(unrelated, 0, "unrelated source pins remain hidden");
        assert_eq!(
            scoped
                .execute(
                    "UPDATE code_overlay_pins SET git_blob_id = 'changed' WHERE source_project_id = $1",
                    &[&project_uuid],
                )
                .expect("attempt source-side update"),
            0,
            "source visibility grants no overlay update authority",
        );
        assert_eq!(
            scoped
                .execute(
                    "DELETE FROM code_overlay_pins WHERE source_project_id = $1",
                    &[&project_uuid]
                )
                .expect("attempt source-side deletion"),
            0,
            "source visibility grants no overlay deletion authority",
        );
    }

    fn seed_overlay_pin(
        conn: &mut postgres::Client,
        overlay_project_id: &str,
        source_project_id: &str,
        file_path: &str,
        content_hash: &str,
    ) {
        let machine_id = db::id_param(
            &gobby_core::machine::read_local_machine_id().expect("read local machine id"),
        )
        .expect("machine id is a uuid");
        conn.execute(
            "INSERT INTO code_overlay_pins
                (machine_id, overlay_project_id, file_path, source_project_id, content_hash)
             VALUES ($1, $2, $3, $4, $5)",
            &[
                &machine_id,
                &db::id_param(overlay_project_id).expect("overlay project uuid"),
                &file_path,
                &db::id_param(source_project_id).expect("source project uuid"),
                &content_hash,
            ],
        )
        .expect("insert overlay pin");
    }

    /// A managed gcode principal bound to one project, as the daemon's maintenance
    /// launches use. Revoked and its session removed on drop.
    struct ScopedPrincipal {
        database_url: String,
        owner_database_url: String,
        execution_id: uuid::Uuid,
        credential_generation: i32,
        session_id: uuid::Uuid,
    }

    impl ScopedPrincipal {
        /// The project needs its registry row (`seed_primary_checkout`) first.
        fn issue(owner: &mut postgres::Client, database_url: &str, project_id: &str) -> Self {
            let machine_id = db::id_param(
                &gobby_core::machine::read_local_machine_id().expect("read local machine id"),
            )
            .expect("machine id is a uuid");
            let project_uuid = db::id_param(project_id).expect("test project id is a uuid");
            let session_id = uuid::Uuid::new_v4();
            let execution_id = uuid::Uuid::new_v4();
            let password = format!("gobby-gc-scoped-{}", uuid::Uuid::new_v4().simple());
            owner
                .execute(
                    "INSERT INTO sessions(id, external_id, machine_id, source, project_id)
                     VALUES ($1, $2, $3, 'test', $4)",
                    &[
                        &session_id,
                        &format!("gc-scoped-{session_id}"),
                        &machine_id,
                        &project_uuid,
                    ],
                )
                .expect("seed scoped principal session");
            let issued = owner
                .query_one(
                    "SELECT role_name::TEXT, credential_generation
                     FROM gobby_agent_auth.issue_tool_principal(
                         $1, $2, $3, clock_timestamp() + INTERVAL '10 minutes', $4
                     )",
                    &[&execution_id, &session_id, &machine_id, &password],
                )
                .expect("issue scoped gcode principal");
            let role_name: String = issued.get(0);
            Self {
                database_url: format!(
                    "{database_url}{}user={role_name}&password={password}",
                    if database_url.contains('?') { '&' } else { '?' }
                ),
                owner_database_url: database_url.to_string(),
                execution_id,
                credential_generation: issued.get(1),
                session_id,
            }
        }
    }

    impl Drop for ScopedPrincipal {
        fn drop(&mut self) {
            if let Ok(mut owner) = db::connect_readwrite(&self.owner_database_url) {
                let _ = owner.query_opt(
                    "SELECT gobby_agent_auth.revoke_principal($1, $2)",
                    &[&self.execution_id, &self.credential_generation],
                );
                let _ = owner.execute("DELETE FROM sessions WHERE id = $1", &[&self.session_id]);
            }
        }
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

    fn seed_primary_checkout(conn: &mut postgres::Client, project_id: &str, root: &Path) {
        let project_uuid = db::id_param(project_id).expect("test project id is a uuid");
        let machine_id = db::id_param(
            &gobby_core::machine::read_local_machine_id().expect("read local machine id"),
        )
        .expect("local machine id is a uuid");
        conn.execute(
            "INSERT INTO projects (id, name) VALUES ($1, $2)",
            &[&project_uuid, &format!("gc-{project_id}")],
        )
        .expect("insert registry project");
        conn.execute(
            "INSERT INTO project_checkouts (machine_id, project_id, root_path)
             VALUES ($1, $2, $3)",
            &[
                &machine_id,
                &project_uuid,
                &root.to_string_lossy().to_string(),
            ],
        )
        .expect("insert primary checkout");
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
            machine_id: gobby_core::machine::read_local_machine_id().expect("read machine id"),
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
            "DELETE FROM code_overlay_pins WHERE source_project_id = $1",
            "DELETE FROM code_indexed_file_states WHERE project_id = $1",
            "DELETE FROM code_indexed_project_states WHERE project_id = $1",
            "DELETE FROM code_symbols WHERE project_id = $1",
            "DELETE FROM code_content_chunks WHERE project_id = $1",
            "DELETE FROM code_indexed_files WHERE project_id = $1",
            "DELETE FROM code_indexed_projects WHERE id = $1",
            "DELETE FROM project_checkouts WHERE project_id = $1",
            "DELETE FROM projects WHERE id = $1",
        ] {
            conn.execute(statement, &[&project_id])?;
        }
        Ok(())
    }
}
