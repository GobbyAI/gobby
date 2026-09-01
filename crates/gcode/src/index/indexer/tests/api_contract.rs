use super::super::lifecycle::refresh_project_stats;
use super::super::types::IndexTarget;
use super::super::{
    IndexDegradation, IndexDurations, IndexOptions, IndexOutcome, IndexRequest, index_files,
};
use crate::cli_error::CliError;
use crate::config::{CodeVectorSettings, Context, ProjectIndexScope};
use crate::db;
use crate::index::api;
use crate::models::{
    CODE_INDEX_UUID_NAMESPACE, CallRelation, CallTargetKind, IndexedFile, IndexedProject,
};
use serde::Serialize;
use serde::de::DeserializeOwned;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

fn assert_cli_independent_contract<T>()
where
    T: Serialize + DeserializeOwned,
{
    let type_name = std::any::type_name::<T>();
    assert!(!type_name.contains("commands::"), "{type_name}");
    assert!(!type_name.contains("output::"), "{type_name}");
    assert!(!type_name.contains("clap"), "{type_name}");
}

#[test]
fn library_api_is_cli_independent() {
    assert_cli_independent_contract::<IndexRequest>();
    assert_cli_independent_contract::<IndexOutcome>();
    assert_cli_independent_contract::<IndexDurations>();
    assert_cli_independent_contract::<IndexDegradation>();

    let request = IndexRequest {
        project_root: PathBuf::from("/tmp/project"),
        path_filter: Some(PathBuf::from("src")),
        explicit_files: vec![PathBuf::from("src/lib.rs")],
        full: true,
        require_cpp_semantics: false,
        sync_projections: true,
    };

    let json = serde_json::to_value(&request).expect("request serializes");
    assert_eq!(json["project_root"], "/tmp/project");
    assert_eq!(json["path_filter"], "src");
    assert_eq!(json["explicit_files"][0], "src/lib.rs");
}

#[test]
fn invalidate_postgres_deletes_only_machine_state() {
    let source = include_str!("../lifecycle.rs");
    for expected in [
        "DELETE FROM code_indexed_project_states",
        "WHERE machine_id = $1 AND project_id = $2",
    ] {
        assert!(
            source.contains(expected),
            "missing scoped delete: {expected}"
        );
    }
    for retained in [
        "DELETE FROM code_symbols",
        "DELETE FROM code_indexed_files",
        "DELETE FROM code_content_chunks",
        "DELETE FROM code_imports",
        "DELETE FROM code_calls",
        "DELETE FROM code_inheritance",
        "DELETE FROM code_indexed_projects",
    ] {
        assert!(
            !source.contains(retained),
            "invalidate must retain shared facts: {retained}"
        );
    }
    let truncate_code = ["TRUNCATE", " code_"].concat();
    let drop_table = ["DROP", " TABLE"].concat();
    assert!(!source.contains(&truncate_code));
    assert!(!source.contains(&drop_table));
}

#[test]
fn call_relation_contract_uses_empty_optional_storage_values() {
    let resolved = CallRelation::new(
        "caller-1".to_string(),
        "foo".to_string(),
        "src/main.py".to_string(),
        12,
    )
    .with_symbol_target("callee-1".to_string());
    let unresolved = CallRelation::new(
        "caller-2".to_string(),
        "bar".to_string(),
        "src/main.py".to_string(),
        18,
    );

    assert_eq!(
        resolved.callee_symbol_id.as_deref().unwrap_or(""),
        "callee-1"
    );
    assert_eq!(unresolved.callee_symbol_id.as_deref().unwrap_or(""), "");
    assert_eq!(resolved.callee_target_kind, CallTargetKind::Symbol);
    assert_eq!(unresolved.callee_target_kind, CallTargetKind::Unresolved);
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn primary_pipelines_reject_stale_checkout_roots() {
    let (mut conn, database_url) = connect_contract_db();
    let project_id = unique_contract_project_id("gcode-primary-pipeline-contract");
    cleanup_contract_project(&mut conn, &project_id).expect("pre-clean pipeline rows");
    let _cleanup = ContractProjectCleanup {
        database_url: database_url.clone(),
        project_id: project_id.clone(),
    };
    let committed_root = tempfile::tempdir().expect("create committed checkout");
    let stale_root = tempfile::tempdir().expect("create stale checkout");
    let source = stale_root.path().join("src/lib.rs");
    std::fs::create_dir_all(source.parent().expect("source parent"))
        .expect("create source directory");
    std::fs::write(&source, b"pub fn stale_root() {}\n").expect("write source file");
    seed_contract_checkout(&mut conn, &project_id, committed_root.path());

    let ctx = contract_context(
        database_url,
        &project_id,
        stale_root.path(),
        ProjectIndexScope::Single,
    );
    let requests = [
        IndexRequest {
            project_root: stale_root.path().to_path_buf(),
            path_filter: None,
            explicit_files: Vec::new(),
            full: true,
            require_cpp_semantics: false,
            sync_projections: false,
        },
        IndexRequest {
            project_root: stale_root.path().to_path_buf(),
            path_filter: None,
            explicit_files: vec![source],
            full: false,
            require_cpp_semantics: false,
            sync_projections: false,
        },
    ];
    for request in requests {
        let error = index_files(request, &ctx, IndexOptions::default())
            .expect_err("primary pipeline must reject a stale checkout root");
        assert!(
            error.to_string().contains("checkout"),
            "unexpected stale-root error: {error:#}"
        );
        let cli = error
            .downcast_ref::<CliError>()
            .expect("pipeline surfaces the typed fence error");
        assert_eq!(cli.code, "checkout_mismatch");
        assert!(
            cli.message
                .contains(&committed_root.path().to_string_lossy().to_string()),
            "{}",
            cli.message
        );
    }
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn refresh_project_stats_propagates_checkout_fence_failure() {
    let (mut conn, database_url) = connect_contract_db();
    let project_id = unique_contract_project_id("gcode-stats-fence-contract");
    cleanup_contract_project(&mut conn, &project_id).expect("pre-clean stats rows");
    let _cleanup = ContractProjectCleanup {
        database_url,
        project_id: project_id.clone(),
    };
    let committed_root = tempfile::tempdir().expect("create committed checkout");
    let stale_root = tempfile::tempdir().expect("create stale checkout");
    let machine_id = gobby_core::machine::read_local_machine_id().expect("read machine id");
    seed_contract_checkout(&mut conn, &project_id, committed_root.path());

    let error = refresh_project_stats(
        &mut conn,
        &machine_id,
        IndexTarget {
            project_id: &project_id,
            root_path: stale_root.path(),
            mode: api::IndexWriteMode::Primary,
        },
        1,
        None,
        None,
    )
    .expect_err("a failed stats fence must fail the index run");
    let cli = error
        .downcast_ref::<CliError>()
        .expect("typed fence error survives the stats context");
    assert_eq!(cli.code, "checkout_mismatch");
    assert!(
        cli.message
            .contains(&committed_root.path().to_string_lossy().to_string()),
        "{}",
        cli.message
    );
    let project_rows: i64 = conn
        .query_one(
            "SELECT COUNT(*)::BIGINT FROM code_indexed_projects WHERE id = $1",
            &[&db::id_param(&project_id).expect("project uuid")],
        )
        .expect("count project rows")
        .get(0);
    assert_eq!(
        project_rows, 0,
        "a failed fence must not seed the project row"
    );

    refresh_project_stats(
        &mut conn,
        &machine_id,
        IndexTarget {
            project_id: &project_id,
            root_path: committed_root.path(),
            mode: api::IndexWriteMode::Primary,
        },
        1,
        None,
        None,
    )
    .expect("committed checkout root refreshes stats");
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn primary_and_overlay_api_modes_enforce_checkout_contract() {
    let (mut conn, database_url) = connect_contract_db();
    let project_id = unique_contract_project_id("gcode-primary-api-contract");
    let overlay_id = unique_contract_project_id("gcode-overlay-api-contract");
    cleanup_contract_project(&mut conn, &project_id).expect("pre-clean primary rows");
    cleanup_contract_project(&mut conn, &overlay_id).expect("pre-clean overlay rows");
    let _primary_cleanup = ContractProjectCleanup {
        database_url: database_url.clone(),
        project_id: project_id.clone(),
    };
    let _overlay_cleanup = ContractProjectCleanup {
        database_url,
        project_id: overlay_id.clone(),
    };
    let primary_root = tempfile::tempdir().expect("create primary root");
    let overlay_root = tempfile::tempdir().expect("create overlay root");
    let stale_root = tempfile::tempdir().expect("create stale root");
    let machine_id = gobby_core::machine::read_local_machine_id().expect("read machine id");
    seed_contract_checkout(&mut conn, &project_id, primary_root.path());

    api::upsert_project_seed(
        &mut conn,
        &machine_id,
        &project_id,
        primary_root.path(),
        api::IndexWriteMode::Primary,
    )
    .expect("matching primary seed");
    api::upsert_project_seed(
        &mut conn,
        &machine_id,
        &project_id,
        stale_root.path(),
        api::IndexWriteMode::Primary,
    )
    .expect_err("stale primary seed");
    let primary_stats = contract_project(&project_id, primary_root.path());
    api::upsert_project_stats(
        &mut conn,
        &machine_id,
        &primary_stats,
        api::IndexWriteMode::Primary,
    )
    .expect("matching primary stats");
    api::upsert_project_stats(
        &mut conn,
        &machine_id,
        &contract_project(&project_id, stale_root.path()),
        api::IndexWriteMode::Primary,
    )
    .expect_err("stale primary stats");

    let file = contract_file(&project_id, "src/lib.rs", "primary-hash");
    seed_contract_file(&mut conn, &file);
    api::upsert_file_state(
        &mut conn,
        &machine_id,
        &file,
        primary_root.path(),
        api::IndexWriteMode::Primary,
    )
    .expect("matching primary file state");
    api::upsert_file_state(
        &mut conn,
        &machine_id,
        &file,
        stale_root.path(),
        api::IndexWriteMode::Primary,
    )
    .expect_err("stale primary file state");
    api::delete_file_state(
        &mut conn,
        &machine_id,
        &project_id,
        &file.file_path,
        stale_root.path(),
        api::IndexWriteMode::Primary,
    )
    .expect_err("stale primary delete");
    assert!(
        api::delete_file_state(
            &mut conn,
            &machine_id,
            &project_id,
            &file.file_path,
            primary_root.path(),
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
            primary_root.path(),
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
        stale_root.path(),
        api::IndexWriteMode::Primary,
    )
    .expect_err("stale primary adoption");

    api::upsert_project_seed(
        &mut conn,
        &machine_id,
        &overlay_id,
        overlay_root.path(),
        api::IndexWriteMode::Overlay,
    )
    .expect("overlay seed without checkout");
    api::upsert_project_stats(
        &mut conn,
        &machine_id,
        &contract_project(&overlay_id, overlay_root.path()),
        api::IndexWriteMode::Overlay,
    )
    .expect("overlay stats without checkout");
    let overlay_file = contract_file(&overlay_id, "src/overlay.rs", "overlay-hash");
    seed_contract_file(&mut conn, &overlay_file);
    api::upsert_file_state(
        &mut conn,
        &machine_id,
        &overlay_file,
        overlay_root.path(),
        api::IndexWriteMode::Overlay,
    )
    .expect("overlay file state without checkout");
    assert!(
        api::delete_file_state(
            &mut conn,
            &machine_id,
            &overlay_id,
            &overlay_file.file_path,
            overlay_root.path(),
            api::IndexWriteMode::Overlay,
        )
        .expect("overlay delete without checkout")
    );
    assert!(
        api::adopt_file_state(
            &mut conn,
            &machine_id,
            &overlay_id,
            &overlay_file.file_path,
            &overlay_file.content_hash,
            overlay_root.path(),
            api::IndexWriteMode::Overlay,
        )
        .expect("overlay adoption without checkout")
    );
}

#[derive(Clone, Copy, Debug)]
enum PrimaryWriter {
    Seed,
    Stats,
    FileUpsert,
    Adopt,
    Delete,
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn every_primary_writer_serializes_with_rebind() {
    for writer in [
        PrimaryWriter::Seed,
        PrimaryWriter::Stats,
        PrimaryWriter::FileUpsert,
        PrimaryWriter::Adopt,
        PrimaryWriter::Delete,
    ] {
        assert_eq!(
            primary_writer_rebind_code(writer).as_deref(),
            Some("57014"),
            "{writer:?} must hold the checkout lock until its transaction ends"
        );
    }
}

fn primary_writer_rebind_code(writer: PrimaryWriter) -> Option<String> {
    let (mut writer_conn, database_url) = connect_contract_db();
    let project_id = unique_contract_project_id(&format!("gcode-primary-writer-{writer:?}"));
    cleanup_contract_project(&mut writer_conn, &project_id).expect("pre-clean writer rows");
    let _cleanup = ContractProjectCleanup {
        database_url: database_url.clone(),
        project_id: project_id.clone(),
    };
    let old_root = tempfile::tempdir().expect("create old checkout");
    let new_root = tempfile::tempdir().expect("create new checkout");
    let machine_id = gobby_core::machine::read_local_machine_id().expect("read machine id");
    seed_contract_checkout(&mut writer_conn, &project_id, old_root.path());
    let file = contract_file(&project_id, "src/lib.rs", "writer-hash");
    if !matches!(writer, PrimaryWriter::Seed) {
        api::upsert_project_seed(
            &mut writer_conn,
            &machine_id,
            &project_id,
            old_root.path(),
            api::IndexWriteMode::Primary,
        )
        .expect("seed indexed project");
    }
    if matches!(
        writer,
        PrimaryWriter::FileUpsert | PrimaryWriter::Adopt | PrimaryWriter::Delete
    ) {
        seed_contract_file(&mut writer_conn, &file);
    }
    if matches!(writer, PrimaryWriter::Delete) {
        api::upsert_file_state(
            &mut writer_conn,
            &machine_id,
            &file,
            old_root.path(),
            api::IndexWriteMode::Primary,
        )
        .expect("seed selector for delete");
    }

    let mut writer_tx = writer_conn.transaction().expect("begin primary writer");
    run_primary_writer(
        &mut writer_tx,
        writer,
        &machine_id,
        &project_id,
        old_root.path(),
        &file,
    )
    .expect("matching primary writer");

    let mut rebind_conn =
        gobby_core::postgres::connect_readwrite(&database_url).expect("connect rebind");
    rebind_conn
        .batch_execute("SET statement_timeout = '250ms'")
        .expect("set bounded rebind wait");
    let machine_uuid = db::id_param(&machine_id).expect("machine uuid");
    let project_uuid = db::id_param(&project_id).expect("project uuid");
    let blocked = rebind_conn
        .execute(
            "UPDATE project_checkouts SET root_path = $3
             WHERE machine_id = $1 AND project_id = $2",
            &[
                &machine_uuid,
                &project_uuid,
                &new_root.path().to_string_lossy(),
            ],
        )
        .expect_err("rebind must wait for the primary writer transaction");
    let blocked_code = blocked.code().map(|code| code.code().to_string());

    writer_tx.commit().expect("commit primary writer");
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
                &new_root.path().to_string_lossy(),
            ],
        )
        .expect("rebind after writer commit");
    run_primary_writer(
        &mut writer_conn,
        writer,
        &machine_id,
        &project_id,
        old_root.path(),
        &file,
    )
    .expect_err("stale primary writer cannot run after rebind");
    blocked_code
}

fn run_primary_writer(
    conn: &mut impl postgres::GenericClient,
    writer: PrimaryWriter,
    machine_id: &str,
    project_id: &str,
    root: &Path,
    file: &IndexedFile,
) -> anyhow::Result<()> {
    match writer {
        PrimaryWriter::Seed => api::upsert_project_seed(
            conn,
            machine_id,
            project_id,
            root,
            api::IndexWriteMode::Primary,
        ),
        PrimaryWriter::Stats => api::upsert_project_stats(
            conn,
            machine_id,
            &contract_project(project_id, root),
            api::IndexWriteMode::Primary,
        ),
        PrimaryWriter::FileUpsert => {
            api::upsert_file_state(conn, machine_id, file, root, api::IndexWriteMode::Primary)
        }
        PrimaryWriter::Adopt => api::adopt_file_state(
            conn,
            machine_id,
            project_id,
            &file.file_path,
            &file.content_hash,
            root,
            api::IndexWriteMode::Primary,
        )
        .map(|_| ()),
        PrimaryWriter::Delete => api::delete_file_state(
            conn,
            machine_id,
            project_id,
            &file.file_path,
            root,
            api::IndexWriteMode::Primary,
        )
        .map(|_| ()),
    }
}

fn connect_contract_db() -> (postgres::Client, String) {
    let database_url = crate::test_env::postgres_test_database_url("indexer API contract tests");
    let conn = gobby_core::postgres::connect_readwrite(&database_url)
        .expect("connect to PostgreSQL test database");
    (conn, database_url)
}

fn contract_context(
    database_url: String,
    project_id: &str,
    project_root: &Path,
    index_scope: ProjectIndexScope,
) -> Context {
    Context {
        database_url,
        project_root: project_root.to_path_buf(),
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
        index_scope,
    }
}

fn contract_project(project_id: &str, root: &Path) -> IndexedProject {
    IndexedProject {
        id: project_id.to_string(),
        root_path: root.display().to_string(),
        total_files: 1,
        total_symbols: 1,
        last_indexed_at: String::new(),
        index_duration_ms: 1,
        total_eligible_files: None,
        indexer_version: None,
    }
}

fn contract_file(project_id: &str, file_path: &str, content_hash: &str) -> IndexedFile {
    IndexedFile {
        id: IndexedFile::make_id(project_id, file_path, content_hash),
        project_id: project_id.to_string(),
        file_path: file_path.to_string(),
        language: "rust".to_string(),
        content_hash: content_hash.to_string(),
        symbol_count: 1,
        byte_size: 32,
        indexed_at: String::new(),
    }
}

fn seed_contract_file(conn: &mut postgres::Client, file: &IndexedFile) {
    api::upsert_file(conn, file).expect("seed shared file");
    conn.execute(
        "UPDATE code_indexed_files SET graph_synced = true, vectors_synced = true
         WHERE id = $1",
        &[&db::id_param(&file.id).expect("file uuid")],
    )
    .expect("mark shared projections synced");
}

fn seed_contract_checkout(conn: &mut postgres::Client, project_id: &str, root: &Path) {
    let project_uuid = db::id_param(project_id).expect("project uuid");
    let machine_id = gobby_core::machine::read_local_machine_id().expect("read machine id");
    let machine_uuid = db::id_param(&machine_id).expect("machine uuid");
    conn.execute(
        "INSERT INTO projects (id, name) VALUES ($1, $2)",
        &[&project_uuid, &format!("contract-{project_id}")],
    )
    .expect("seed registry project");
    conn.execute(
        "INSERT INTO project_checkouts (machine_id, project_id, root_path)
         VALUES ($1, $2, $3)",
        &[&machine_uuid, &project_uuid, &root.to_string_lossy()],
    )
    .expect("seed primary checkout");
}

fn unique_contract_project_id(prefix: &str) -> String {
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

struct ContractProjectCleanup {
    database_url: String,
    project_id: String,
}

impl Drop for ContractProjectCleanup {
    fn drop(&mut self) {
        if let Ok(mut conn) = gobby_core::postgres::connect_readwrite(&self.database_url) {
            let _ = cleanup_contract_project(&mut conn, &self.project_id);
        }
    }
}

fn cleanup_contract_project(conn: &mut postgres::Client, project_id: &str) -> anyhow::Result<()> {
    let project_id = db::id_param(project_id)?;
    for statement in [
        "DELETE FROM code_indexed_file_states WHERE project_id = $1",
        "DELETE FROM code_indexed_project_states WHERE project_id = $1",
        "DELETE FROM code_calls WHERE project_id = $1",
        "DELETE FROM code_inheritance WHERE project_id = $1",
        "DELETE FROM code_imports WHERE project_id = $1",
        "DELETE FROM code_content_chunks WHERE project_id = $1",
        "DELETE FROM code_symbols WHERE project_id = $1",
        "DELETE FROM code_indexed_files WHERE project_id = $1",
        "DELETE FROM code_indexed_projects WHERE id = $1",
        "DELETE FROM project_checkouts WHERE project_id = $1",
        "DELETE FROM projects WHERE id = $1",
    ] {
        conn.execute(statement, &[&project_id])?;
    }
    Ok(())
}
