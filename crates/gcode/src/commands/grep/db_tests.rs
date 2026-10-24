use postgres::Client;
use postgres::types::ToSql;

use super::{GrepOptions, grep_repo};
use crate::config::{CodeVectorSettings, Context, ProjectIndexScope};
use crate::output::Format;

fn fixture_uuid(key: &str) -> uuid::Uuid {
    uuid::Uuid::new_v5(&crate::models::CODE_INDEX_UUID_NAMESPACE, key.as_bytes())
}

fn unique_project_id(prefix: &str) -> String {
    fixture_uuid(&format!(
        "{prefix}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("system time after epoch")
            .as_nanos()
    ))
    .to_string()
}

struct ProjectCleanup {
    database_url: String,
    project_id: uuid::Uuid,
}

impl ProjectCleanup {
    fn run(conn: &mut Client, project_id: &uuid::Uuid) {
        for sql in [
            "DELETE FROM code_indexed_file_states WHERE project_id = $1",
            "DELETE FROM code_indexed_project_states WHERE project_id = $1",
            "DELETE FROM code_content_chunks WHERE project_id = $1",
            "DELETE FROM code_indexed_files WHERE project_id = $1",
            "DELETE FROM code_indexed_projects WHERE id = $1",
        ] {
            let _ = conn.execute(sql, &[project_id]);
        }
    }
}

impl Drop for ProjectCleanup {
    fn drop(&mut self) {
        if let Ok(mut conn) = gobby_core::postgres::connect_readwrite(&self.database_url) {
            Self::run(&mut conn, &self.project_id);
        }
    }
}

fn insert_file_version(
    conn: &mut Client,
    project_id: &uuid::Uuid,
    file_path: &str,
    content_hash: &str,
) {
    let id = fixture_uuid(&format!("{project_id}:{file_path}:{content_hash}"));
    let params: &[&(dyn ToSql + Sync)] = &[&id, project_id, &file_path, &content_hash];
    conn.execute(
        "INSERT INTO code_indexed_files
                (id, project_id, file_path, language, content_hash, symbol_count, byte_size,
                 graph_synced, vectors_synced, graph_sync_attempted_at, indexed_at)
             VALUES ($1, $2, $3, 'rust', $4, 1, 1, false, false, NULL, NOW())",
        params,
    )
    .expect("insert indexed file version");
}

fn insert_machine_state(
    conn: &mut Client,
    machine_id: &uuid::Uuid,
    project_id: &uuid::Uuid,
    root_path: &str,
    file_path: &str,
    content_hash: &str,
) {
    conn.execute(
        "INSERT INTO code_indexed_project_states
                (machine_id, project_id, root_path, total_files, total_symbols,
                 last_indexed_at, index_duration_ms)
             VALUES ($1, $2, $3, 0, 0, NOW(), 0)
             ON CONFLICT (machine_id, project_id) DO NOTHING",
        &[machine_id, project_id, &root_path],
    )
    .expect("insert machine project state");
    conn.execute(
        "INSERT INTO code_indexed_file_states
                (machine_id, project_id, file_path, content_hash)
             VALUES ($1, $2, $3, $4)",
        &[machine_id, project_id, &file_path, &content_hash],
    )
    .expect("insert machine file state");
}

fn insert_chunk_version(
    conn: &mut Client,
    project_id: &uuid::Uuid,
    file_path: &str,
    content: &str,
    content_hash: &str,
) {
    let id = fixture_uuid(&format!("{project_id}:{file_path}:0:{content_hash}"));
    let params: &[&(dyn ToSql + Sync)] = &[&id, project_id, &file_path, &content, &content_hash];
    conn.execute(
        "INSERT INTO code_content_chunks
                (id, project_id, file_path, content_hash, chunk_index, line_start, line_end,
                 content, language, created_at)
             VALUES ($1, $2, $3, $5, 0, 1, 1, $4, 'rust', NOW())",
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
fn overlay_grep_hides_parent_rows_shadowed_by_overlay_state() {
    let database_url = crate::test_env::postgres_test_database_url("overlay grep PostgreSQL tests");
    let mut conn = gobby_core::postgres::connect_readwrite(&database_url)
        .expect("connect grep PostgreSQL test database");
    crate::schema::validate_runtime_schema(&mut conn).expect("grep test schema is valid");

    let parent_root = tempfile::tempdir().expect("create parent root");
    let overlay_root = tempfile::tempdir().expect("create overlay root");
    // The overlay deleted src/tombstoned.rs, so only the parent has it on disk.
    for (root, file_path) in [
        (parent_root.path(), "src/parent_only.rs"),
        (parent_root.path(), "src/shadowed.rs"),
        (parent_root.path(), "src/tombstoned.rs"),
        (overlay_root.path(), "src/overlay_only.rs"),
        (overlay_root.path(), "src/shadowed.rs"),
    ] {
        let absolute = root.join(file_path);
        std::fs::create_dir_all(absolute.parent().expect("file has parent"))
            .expect("create src dir");
        std::fs::write(&absolute, "fn marker() {}\n").expect("write fixture file");
    }

    let parent_id = unique_project_id("grep-overlay-parent");
    let overlay_id = unique_project_id("grep-overlay-child");
    let parent_uuid = crate::db::id_param(&parent_id).expect("parent project uuid");
    let overlay_uuid = crate::db::id_param(&overlay_id).expect("overlay project uuid");
    ProjectCleanup::run(&mut conn, &parent_uuid);
    ProjectCleanup::run(&mut conn, &overlay_uuid);
    let _parent_cleanup = ProjectCleanup {
        database_url: database_url.clone(),
        project_id: parent_uuid,
    };
    let _overlay_cleanup = ProjectCleanup {
        database_url: database_url.clone(),
        project_id: overlay_uuid,
    };
    for project in [&parent_uuid, &overlay_uuid] {
        conn.execute(
            "INSERT INTO code_indexed_projects (id) VALUES ($1)",
            &[project],
        )
        .expect("insert project");
    }

    let machine = crate::db::id_param(
        &gobby_core::machine::read_local_machine_id().expect("read local machine id"),
    )
    .expect("local machine uuid");
    let parent_root_path = parent_root.path().to_string_lossy().to_string();
    let overlay_root_path = overlay_root.path().to_string_lossy().to_string();

    for (file_path, content, hash) in [
        (
            "src/parent_only.rs",
            "grepmarker parent_only",
            "hash-parent-only",
        ),
        (
            "src/shadowed.rs",
            "grepmarker parent_shadowed",
            "hash-parent-shadowed",
        ),
        (
            "src/tombstoned.rs",
            "grepmarker parent_tombstoned",
            "hash-parent-tombstoned",
        ),
    ] {
        insert_file_version(&mut conn, &parent_uuid, file_path, hash);
        insert_machine_state(
            &mut conn,
            &machine,
            &parent_uuid,
            &parent_root_path,
            file_path,
            hash,
        );
        insert_chunk_version(&mut conn, &parent_uuid, file_path, content, hash);
    }
    for (file_path, content, hash) in [
        (
            "src/overlay_only.rs",
            "grepmarker overlay_only",
            "hash-overlay-only",
        ),
        (
            "src/shadowed.rs",
            "grepmarker overlay_shadowed",
            "hash-overlay-shadowed",
        ),
    ] {
        insert_file_version(&mut conn, &overlay_uuid, file_path, hash);
        insert_machine_state(
            &mut conn,
            &machine,
            &overlay_uuid,
            &overlay_root_path,
            file_path,
            hash,
        );
        insert_chunk_version(&mut conn, &overlay_uuid, file_path, content, hash);
    }
    // An overlay tombstone has a state row, which must shadow the parent row,
    // and its chunk must stay hidden because the file version is a tombstone.
    let tombstone_hash = "hash-overlay-tombstone";
    insert_file_version(
        &mut conn,
        &overlay_uuid,
        "src/tombstoned.rs",
        tombstone_hash,
    );
    conn.execute(
        "UPDATE code_indexed_files SET language = $1
         WHERE project_id = $2 AND file_path = $3 AND content_hash = $4",
        &[
            &crate::visibility::TOMBSTONE_LANGUAGE,
            &overlay_uuid,
            &"src/tombstoned.rs",
            &tombstone_hash,
        ],
    )
    .expect("mark overlay file version as tombstone");
    insert_machine_state(
        &mut conn,
        &machine,
        &overlay_uuid,
        &overlay_root_path,
        "src/tombstoned.rs",
        tombstone_hash,
    );
    insert_chunk_version(
        &mut conn,
        &overlay_uuid,
        "src/tombstoned.rs",
        "grepmarker overlay_tombstoned",
        tombstone_hash,
    );

    let ctx = Context {
        database_url: database_url.clone(),
        project_root: overlay_root.path().to_path_buf(),
        project_id: overlay_id.clone(),
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
        index_scope: ProjectIndexScope::Overlay {
            overlay_project_id: overlay_id.clone(),
            overlay_root: overlay_root.path().to_path_buf(),
            parent_project_id: parent_id.clone(),
            parent_root: parent_root.path().to_path_buf(),
        },
    };
    let options = GrepOptions {
        pattern: "grepmarker",
        paths: &[],
        globs: &[],
        fixed_strings: true,
        ignore_case: false,
        word: false,
        context: None,
        before_context: None,
        after_context: None,
        max_count: None,
        offset: 0,
        token_budget: None,
        files_with_matches: false,
        format: Format::Json,
    };

    let result = grep_repo(&ctx, &mut conn, &options).expect("grep overlay repo");
    let mut lines: Vec<&str> = result.matches.iter().map(|m| m.text.as_str()).collect();
    lines.sort_unstable();
    assert_eq!(
        lines,
        vec![
            "grepmarker overlay_only",
            "grepmarker overlay_shadowed",
            "grepmarker parent_only",
        ],
        "overlay grep must show overlay rows and unshadowed parent rows only"
    );
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn grep_scopes_chunks_to_local_machine_file_state() {
    let database_url = crate::test_env::postgres_test_database_url("grep PostgreSQL tests");
    let mut conn = gobby_core::postgres::connect_readwrite(&database_url)
        .expect("connect grep PostgreSQL test database");
    crate::schema::validate_runtime_schema(&mut conn).expect("grep test schema is valid");

    let root = tempfile::tempdir().expect("create temp project root");
    std::fs::create_dir_all(root.path().join("src")).expect("create src dir");
    std::fs::write(root.path().join("src/lib.rs"), "grepmarker localonly\n")
        .expect("write fixture file");

    let project_id = unique_project_id("grep-two-machine");
    let project_uuid = crate::db::id_param(&project_id).expect("project uuid");
    ProjectCleanup::run(&mut conn, &project_uuid);
    let _cleanup = ProjectCleanup {
        database_url: database_url.clone(),
        project_id: project_uuid,
    };

    conn.execute(
        "INSERT INTO code_indexed_projects (id) VALUES ($1)",
        &[&project_uuid],
    )
    .expect("insert project");

    let local_machine = crate::db::id_param(
        &gobby_core::machine::read_local_machine_id().expect("read local machine id"),
    )
    .expect("local machine uuid");
    let foreign_machine = fixture_uuid(&format!("{project_id}:foreign-machine"));
    crate::test_env::seed_test_machine(&mut conn, &foreign_machine.to_string())
        .expect("seed foreign machine");
    let root_path = root.path().to_string_lossy().to_string();

    insert_file_version(&mut conn, &project_uuid, "src/lib.rs", "hash-local");
    insert_file_version(&mut conn, &project_uuid, "src/lib.rs", "hash-foreign");
    insert_machine_state(
        &mut conn,
        &local_machine,
        &project_uuid,
        &root_path,
        "src/lib.rs",
        "hash-local",
    );
    insert_machine_state(
        &mut conn,
        &foreign_machine,
        &project_uuid,
        &root_path,
        "src/lib.rs",
        "hash-foreign",
    );
    insert_chunk_version(
        &mut conn,
        &project_uuid,
        "src/lib.rs",
        "grepmarker localonly",
        "hash-local",
    );
    insert_chunk_version(
        &mut conn,
        &project_uuid,
        "src/lib.rs",
        "grepmarker foreignonly",
        "hash-foreign",
    );

    let ctx = Context {
        database_url: database_url.clone(),
        project_root: root.path().to_path_buf(),
        project_id: project_id.clone(),
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
    let options = GrepOptions {
        pattern: "grepmarker",
        paths: &[],
        globs: &[],
        fixed_strings: true,
        ignore_case: false,
        word: false,
        context: None,
        before_context: None,
        after_context: None,
        max_count: None,
        offset: 0,
        token_budget: None,
        files_with_matches: false,
        format: Format::Json,
    };

    let result = grep_repo(&ctx, &mut conn, &options).expect("grep repo");
    let lines: Vec<&str> = result.matches.iter().map(|m| m.text.as_str()).collect();
    assert_eq!(
        lines,
        vec!["grepmarker localonly"],
        "grep must return only the local machine's active content version"
    );
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn grep_reports_incomplete_coverage_for_unindexed_path() {
    let database_url = crate::test_env::postgres_test_database_url("grep PostgreSQL tests");
    let mut conn = gobby_core::postgres::connect_readwrite(&database_url)
        .expect("connect grep PostgreSQL test database");
    crate::schema::validate_runtime_schema(&mut conn).expect("grep test schema is valid");

    let root = tempfile::tempdir().expect("create temp project root");
    std::fs::create_dir_all(root.path().join("src")).expect("create src dir");
    std::fs::write(root.path().join("src/lib.rs"), "grepmarker localonly\n")
        .expect("write fixture file");

    let project_id = unique_project_id("grep-coverage-gap");
    let project_uuid = crate::db::id_param(&project_id).expect("project uuid");
    ProjectCleanup::run(&mut conn, &project_uuid);
    let _cleanup = ProjectCleanup {
        database_url: database_url.clone(),
        project_id: project_uuid,
    };

    conn.execute(
        "INSERT INTO code_indexed_projects (id) VALUES ($1)",
        &[&project_uuid],
    )
    .expect("insert project");

    let local_machine = crate::db::id_param(
        &gobby_core::machine::read_local_machine_id().expect("read local machine id"),
    )
    .expect("local machine uuid");
    let root_path = root.path().to_string_lossy().to_string();
    insert_file_version(&mut conn, &project_uuid, "src/lib.rs", "hash-local");
    insert_machine_state(
        &mut conn,
        &local_machine,
        &project_uuid,
        &root_path,
        "src/lib.rs",
        "hash-local",
    );
    insert_chunk_version(
        &mut conn,
        &project_uuid,
        "src/lib.rs",
        "grepmarker localonly",
        "hash-local",
    );

    let ctx = Context {
        database_url: database_url.clone(),
        project_root: root.path().to_path_buf(),
        project_id: project_id.clone(),
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
    let missing = "not-indexed/mod.rs".to_string();
    let uncovered = GrepOptions {
        pattern: "grepmarker",
        paths: std::slice::from_ref(&missing),
        globs: &[],
        fixed_strings: true,
        ignore_case: false,
        word: false,
        context: None,
        before_context: None,
        after_context: None,
        max_count: None,
        offset: 0,
        token_budget: None,
        files_with_matches: false,
        format: Format::Json,
    };
    let gap = grep_repo(&ctx, &mut conn, &uncovered).expect("grep unindexed path");
    assert!(gap.matches.is_empty());
    assert_eq!(
        gap.coverage_gap.as_deref(),
        Some(
            "incomplete index coverage: no indexed files match paths not-indexed/mod.rs; run gcode index"
        )
    );

    let indexed_path = "src/lib.rs".to_string();
    let nomatch = GrepOptions {
        pattern: "does-not-appear-in-chunk",
        paths: std::slice::from_ref(&indexed_path),
        ..uncovered
    };
    let covered = grep_repo(&ctx, &mut conn, &nomatch).expect("grep indexed path without matches");
    assert!(covered.matches.is_empty());
    assert_eq!(covered.coverage_gap, None);
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn grep_checks_indexed_file_states_in_one_batch() {
    let database_url = crate::test_env::postgres_test_database_url("grep PostgreSQL tests");
    let mut conn = gobby_core::postgres::connect_readwrite(&database_url)
        .expect("connect grep PostgreSQL test database");
    crate::schema::validate_runtime_schema(&mut conn).expect("grep test schema is valid");

    const FILE_COUNT: usize = 12;
    let root = tempfile::tempdir().expect("create temp project root");
    std::fs::create_dir_all(root.path().join("src")).expect("create src dir");
    let file_paths: Vec<String> = (0..FILE_COUNT)
        .map(|index| format!("src/file_{index:02}.rs"))
        .collect();
    for file_path in &file_paths {
        std::fs::write(root.path().join(file_path), "grepbatch marker\n")
            .expect("write fixture file");
    }

    let project_id = unique_project_id("grep-one-batch");
    let project_uuid = crate::db::id_param(&project_id).expect("project uuid");
    ProjectCleanup::run(&mut conn, &project_uuid);
    let _cleanup = ProjectCleanup {
        database_url: database_url.clone(),
        project_id: project_uuid,
    };

    conn.execute(
        "INSERT INTO code_indexed_projects (id) VALUES ($1)",
        &[&project_uuid],
    )
    .expect("insert project");

    let local_machine = crate::db::id_param(
        &gobby_core::machine::read_local_machine_id().expect("read local machine id"),
    )
    .expect("local machine uuid");
    let root_path = root.path().to_string_lossy().to_string();
    for (index, file_path) in file_paths.iter().enumerate() {
        let content_hash = format!("hash-{index:02}");
        insert_file_version(&mut conn, &project_uuid, file_path, &content_hash);
        insert_machine_state(
            &mut conn,
            &local_machine,
            &project_uuid,
            &root_path,
            file_path,
            &content_hash,
        );
        insert_chunk_version(
            &mut conn,
            &project_uuid,
            file_path,
            "grepbatch marker",
            &content_hash,
        );
    }

    let ctx = Context {
        database_url: database_url.clone(),
        project_root: root.path().to_path_buf(),
        project_id: project_id.clone(),
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
    let options = GrepOptions {
        pattern: "grepbatch",
        paths: &[],
        globs: &[],
        fixed_strings: true,
        ignore_case: false,
        word: false,
        context: None,
        before_context: None,
        after_context: None,
        max_count: None,
        offset: 0,
        token_budget: None,
        files_with_matches: false,
        format: Format::Json,
    };

    super::indexed_paths::reset_lookup_count();
    let result = grep_repo(&ctx, &mut conn, &options).expect("grep repo");
    assert_eq!(
        result.matches.len(),
        FILE_COUNT,
        "grep must return a match for every indexed file"
    );
    assert_eq!(
        super::indexed_paths::lookup_count(),
        1,
        "grep must check indexed-file state with one set-based query, not one per candidate file"
    );
}
