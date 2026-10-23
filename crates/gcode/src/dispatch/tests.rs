use super::*;
use crate::cli::Cli;
use crate::cli_error::CliError;
use crate::commands::embeddings_doctor::EmbeddingsDoctorExit;
use crate::commands::graph::GraphSyncContractError;
use crate::config::{CodeVectorSettings, Context, ProjectIndexScope};
use clap::Parser;
use gobby_core::grant::GrantError;
use std::path::PathBuf;

fn services_for(args: &[&str]) -> config::ServiceConfigSelection {
    let cli = Cli::try_parse_from(std::iter::once("gcode").chain(args.iter().copied()))
        .expect("command parses");
    service_config_selection(&cli.command, None)
}

#[test]
fn stderr_logger_defaults_to_warnings_for_non_quiet_runs() {
    assert_eq!(stderr_log_level(false, None), log::LevelFilter::Warn);
}

#[test]
fn stderr_logger_respects_plain_rust_log_level() {
    assert_eq!(
        stderr_log_level(false, Some("debug")),
        log::LevelFilter::Debug
    );
}

#[test]
fn stderr_logger_uses_quiet_as_hard_mute() {
    assert_eq!(stderr_log_level(true, Some("warn")), log::LevelFilter::Off);
}

#[test]
fn lookup_commands_skip_service_config_resolution() {
    for args in [
        &["grep", "-F", "needle"][..],
        &["tree"][..],
        &["symbol-at", "src/lib.rs:10"][..],
        &["search-content", "needle"][..],
        &["search-text", "needle"][..],
        &["search-symbol", "needle"][..],
    ] {
        assert_eq!(
            services_for(args),
            config::ServiceConfigSelection::database_only()
        );
    }
}

#[test]
fn graph_and_vector_commands_request_only_needed_services() {
    assert_eq!(
        services_for(&["search", "concept"]),
        config::ServiceConfigSelection::hybrid_search()
    );
    assert_eq!(
        services_for(&["search-symbol", "needle", "--with-graph"]),
        config::ServiceConfigSelection::falkordb_only()
    );
    assert_eq!(
        services_for(&["callers", "needle"]),
        config::ServiceConfigSelection::falkordb_only()
    );
    assert_eq!(
        services_for(&["callees", "needle"]),
        config::ServiceConfigSelection::falkordb_only()
    );
    assert_eq!(
        services_for(&["vector", "cleanup-orphans"]),
        config::ServiceConfigSelection::qdrant_only()
    );
    assert_eq!(
        services_for(&["prune", "--force"]),
        config::ServiceConfigSelection::projection_cleanup()
    );
    assert_eq!(
        services_for(&["embeddings", "doctor"]),
        config::ServiceConfigSelection::vectors()
    );
}

#[test]
fn invalidate_requests_projection_cleanup_services() {
    assert_eq!(
        services_for(&[
            "invalidate",
            "--project-id",
            "019bfef8-89bb-7bd1-a5c3-80baabdff01b",
            "--force",
        ]),
        config::ServiceConfigSelection::projection_cleanup()
    );
}

#[test]
fn degraded_freshness_warning_names_allow_stale() {
    let line = freshness_warning(
        false,
        &freshness::FreshnessStatus::Degraded("disk full".to_string()),
    )
    .expect("degraded status should warn");
    assert_eq!(
        line,
        "warning: index refresh failed (disk full); serving existing index \
         (pass --allow-stale to skip this check)"
    );
    assert!(!line.contains('\n'));
}

#[test]
fn degraded_freshness_warning_is_suppressed_when_quiet() {
    assert_eq!(
        freshness_warning(
            true,
            &freshness::FreshnessStatus::Degraded("disk full".to_string()),
        ),
        None
    );
}

#[test]
fn allow_stale_notice_is_one_line_and_respects_quiet() {
    let notice = freshness_skip_notice(false, true).expect("allow-stale should emit a notice");
    assert_eq!(notice, "freshness check skipped (--allow-stale)");
    assert!(!notice.contains('\n'));
    assert_eq!(freshness_skip_notice(true, true), None);
    assert_eq!(freshness_skip_notice(false, false), None);
}

#[test]
fn cli_error_uses_its_exit_status() {
    let error = anyhow::Error::from(CliError {
        code: "usage",
        message: "unknown argument".to_string(),
        recovery: None,
        exit_status: 2,
    });
    assert_eq!(classify_run_error(&error).exit, 2);

    let doctor = anyhow::Error::from(CliError {
        code: "embeddings_doctor",
        message: "config drift".to_string(),
        recovery: None,
        exit_status: 11,
    });
    assert_eq!(classify_run_error(&doctor).exit, 11);
}

#[test]
fn classify_run_error_uses_graph_sync_and_embeddings_doctor_exits() {
    let ctx = Context {
        database_url: "postgresql://localhost/nonexistent".to_string(),
        project_root: PathBuf::from("/nonexistent"),
        project_id: "test-project".to_string(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: CodeVectorSettings::default(),
        machine_id: gobby_core::machine::read_local_machine_id().expect("read machine id"),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: ProjectIndexScope::Single,
    };
    let missing_project = anyhow::Error::from(GraphSyncContractError::project_not_indexed(
        &ctx,
        "src/lib.rs",
    ));
    assert_eq!(classify_run_error(&missing_project).exit, 2);
    let missing_file = anyhow::Error::from(GraphSyncContractError::indexed_file_not_found(
        &ctx,
        "src/missing.rs",
    ));
    assert_eq!(classify_run_error(&missing_file).exit, 2);

    assert_eq!(
        classify_run_error(&anyhow::Error::from(EmbeddingsDoctorExit::with_exit_code(
            10
        )))
        .exit,
        10
    );
    assert_eq!(
        classify_run_error(&anyhow::Error::from(EmbeddingsDoctorExit::with_exit_code(
            11
        )))
        .exit,
        11
    );
    assert_eq!(
        classify_run_error(&anyhow::Error::from(EmbeddingsDoctorExit::with_exit_code(
            20
        )))
        .exit,
        20
    );
}

#[test]
fn grant_error_maps_through_cli_error_grant() {
    let error = anyhow::Error::from(GrantError::DaemonRequired);
    assert_eq!(
        classify_run_error(&error).exit,
        CliError::grant(GrantError::DaemonRequired).exit_status
    );
    assert_eq!(classify_run_error(&error).exit, 2);
}

#[test]
fn unclassified_anyhow_error_exits_one() {
    let error = anyhow::anyhow!("unexpected index panic");
    assert_eq!(classify_run_error(&error).exit, 1);
}
#[test]
fn retirement_uses_existing_prune_services_without_embeddings() {
    assert_eq!(
        services_for(&["retire-files", "--manifest", "/private/manifest.json"]),
        services_for(&["prune", "--force"]),
    );
}

#[test]
fn grep_of_explicit_files_refreshes_only_those_files() {
    let project = tempfile::tempdir().expect("tempdir");
    std::fs::create_dir_all(project.path().join("tests")).expect("create tests");
    std::fs::write(
        project.path().join("tests/runner.py"),
        "def test_a(): ...\n",
    )
    .expect("write file");
    let paths = vec!["tests/runner.py".to_string()];

    let scope = navigation::grep_freshness_scope(project.path(), &paths);

    assert!(matches!(
        scope,
        freshness::FreshnessScope::Files(files)
            if files == vec![PathBuf::from("tests/runner.py")]
    ));
}

fn grep_scope_test_context(project_id: &str, root: &std::path::Path) -> Context {
    let root = root.canonicalize().expect("canonical grep checkout");
    let database_url = crate::test_env::postgres_test_database_url("grep freshness tests");
    let mut conn = crate::db::connect_readwrite(&database_url).expect("connect test hub");
    crate::test_env::seed_test_checkout(&mut conn, project_id, &root)
        .expect("register grep test checkout");
    Context {
        database_url,
        project_root: root,
        project_id: project_id.to_string(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: CodeVectorSettings::default(),
        machine_id: gobby_core::machine::read_local_machine_id().expect("read machine id"),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: ProjectIndexScope::Single,
    }
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn grep_of_one_file_refreshes_that_file_and_leaves_siblings_alone() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    std::fs::create_dir_all(root.join("src")).expect("create src");
    std::fs::write(root.join("src/a.rs"), "pub fn a_old() {}\n").expect("write a");
    std::fs::write(root.join("src/b.rs"), "pub fn b_old() {}\n").expect("write b");
    let project_id = uuid::Uuid::new_v5(
        &crate::models::CODE_INDEX_UUID_NAMESPACE,
        b"gcode-grep-file-scope",
    )
    .to_string();
    let ctx = grep_scope_test_context(&project_id, root);
    {
        let mut conn = crate::db::connect_readwrite(&ctx.database_url).expect("connect test hub");
        crate::index::indexer::invalidate(&mut conn, &ctx.project_id, None)
            .expect("invalidate grep test project");
    }
    crate::index::api::index_files(
        crate::index::api::IndexRequest {
            project_root: ctx.project_root.clone(),
            path_filter: None,
            explicit_files: Vec::new(),
            full: true,
            require_cpp_semantics: false,
            sync_projections: false,
        },
        &ctx,
        crate::index::api::IndexOptions::default(),
    )
    .expect("full index of grep test project");
    std::fs::write(root.join("src/a.rs"), "pub fn a_new() {}\n").expect("dirty a");
    std::fs::write(root.join("src/b.rs"), "pub fn b_new() {}\n").expect("dirty b");

    let scope = navigation::grep_freshness_scope(root, &["src/a.rs".to_string()]);
    freshness::ensure_fresh(&ctx, scope).expect("grep freshness");

    assert_eq!(visible_symbol_names(&ctx, "src/a.rs"), ["a_new"]);
    assert_eq!(visible_symbol_names(&ctx, "src/b.rs"), ["b_old"]);
}

fn visible_symbol_names(ctx: &Context, file_path: &str) -> Vec<String> {
    let mut conn = crate::db::connect_readwrite(&ctx.database_url).expect("connect test hub");
    crate::visibility::visible_symbols_for_file(&mut conn, ctx, file_path)
        .expect("read visible symbols")
        .into_iter()
        .map(|symbol| symbol.name)
        .collect()
}

#[test]
fn grep_of_directories_globs_or_no_paths_keeps_project_freshness() {
    let project = tempfile::tempdir().expect("tempdir");
    std::fs::create_dir_all(project.path().join("tests")).expect("create tests");
    std::fs::write(
        project.path().join("tests/runner.py"),
        "def test_a(): ...\n",
    )
    .expect("write file");

    for paths in [
        vec![],
        vec!["tests".to_string()],
        vec!["tests/*.py".to_string()],
        vec!["tests/runner.py".to_string(), "tests".to_string()],
    ] {
        assert!(
            matches!(
                navigation::grep_freshness_scope(project.path(), &paths),
                freshness::FreshnessScope::Project
            ),
            "paths {paths:?} must keep the whole-project freshness check",
        );
    }
}
