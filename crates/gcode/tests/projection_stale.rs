#[cfg(test)]
mod common;

#[cfg(test)]
mod serial_db {
    use super::common::http::spawn_http_responses;
    use postgres::{Client, NoTls};
    use serde_json::Value;
    use std::path::{Path, PathBuf};
    use std::process::Command;

    use gobby_code::test_env;

    // UUIDv5(CODE_INDEX_UUID_NAMESPACE, "gcode-projection-stale-missing-file"):
    // the hub stores project ids as native uuid.
    const PROJECT_ID: &str = "4424ae52-9ce0-52ae-af19-8c3a093c351f";
    const FILE_PATH: &str = "src/lib.rs";
    const CODE_INDEX_UUID_NAMESPACE: uuid::Uuid = uuid::Uuid::from_bytes([
        0xc0, 0xde, 0x1d, 0xe0, 0x00, 0x00, 0x40, 0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00,
    ]);

    #[test]
    fn projection_fixture_namespace_is_canonical() {
        assert_eq!(
            CODE_INDEX_UUID_NAMESPACE.to_string(),
            "c0de1de0-0000-4000-8000-000000000000"
        );
    }

    /// Parse a fixture id for binding against native-uuid hub columns.
    fn uuid_param(id: &str) -> uuid::Uuid {
        uuid::Uuid::parse_str(id).expect("fixture id is a uuid")
    }

    fn local_machine_uuid() -> uuid::Uuid {
        uuid_param(&gobby_core::machine::read_local_machine_id().expect("read local machine id"))
    }

    fn isolated_gobby_home(root: &Path) -> PathBuf {
        let home = root.join(".no-daemon-home");
        std::fs::create_dir_all(&home).expect("create isolated Gobby home");
        std::fs::write(home.join("machine_id"), local_machine_uuid().to_string())
            .expect("write isolated machine id");
        home
    }

    fn attach_managed_grant(
        command: &mut Command,
        home: &Path,
        project_id: &str,
        connections: gobby_core::grant::DirectConnections,
    ) {
        let machine = std::fs::read_to_string(home.join("machine_id")).expect("read machine id");
        let grant =
            gobby_core::grant::managed_direct_grant(project_id, machine.trim(), &connections);
        let path = gobby_core::grant::write_managed_bootstrap(&home.join("grants"), &grant)
            .expect("write managed grant");
        command
            .env("GOBBY_HOME", home)
            .env("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", path);
    }

    /// Deterministic uuid for a seeded child row of `project_id`.
    fn row_uuid(project_id: &str, label: &str) -> uuid::Uuid {
        uuid::Uuid::new_v5(
            &CODE_INDEX_UUID_NAMESPACE,
            format!("{project_id}:{label}").as_bytes(),
        )
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn vector_sync_file_allows_deleted_local_file_state() {
        let database_url = test_env::postgres_test_database_url("projection stale tests");
        let mut conn = Client::connect(&database_url, NoTls).expect("connect PostgreSQL");
        cleanup_project(&mut conn, PROJECT_ID).expect("pre-clean project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: PROJECT_ID.to_string(),
        };

        let project = tempfile::tempdir().expect("temp project");
        std::fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
        std::fs::create_dir_all(project.path().join("src")).expect("create src");
        std::fs::write(project.path().join(FILE_PATH), "pub fn indexed() {}\n")
            .expect("write source file");
        std::fs::write(
            project.path().join(".gobby/gcode.json"),
            serde_json::json!({
                "id": PROJECT_ID,
                "name": "projection-stale",
                "created_at": "2026-06-17T00:00:00Z"
            })
            .to_string(),
        )
        .expect("write gcode identity");

        seed_indexed_file(&mut conn, PROJECT_ID, FILE_PATH);
        let machine_id = local_machine_uuid();
        conn.execute(
            "DELETE FROM code_indexed_file_states
             WHERE machine_id = $1 AND project_id = $2 AND file_path = $3",
            &[&machine_id, &uuid_param(PROJECT_ID), &FILE_PATH],
        )
        .expect("delete local indexed file state");

        let home = isolated_gobby_home(project.path());
        let mut command = Command::new(env!("CARGO_BIN_EXE_gcode"));
        command
            .current_dir(project.path())
            .arg("--allow-stale")
            .arg("--format")
            .arg("json")
            .args([
                "vector",
                "sync-file",
                "--file",
                FILE_PATH,
                "--allow-missing-indexed-file",
            ]);
        attach_managed_grant(
            &mut command,
            &home,
            PROJECT_ID,
            gobby_core::grant::DirectConnections::postgres(&database_url),
        );
        let output = command.output().expect("run gcode vector sync-file");

        assert!(
            output.status.success(),
            "sync-file should skip missing rows, stderr={}",
            String::from_utf8_lossy(&output.stderr)
        );
        let payload: Value =
            serde_json::from_slice(&output.stdout).expect("sync-file output is JSON");
        assert_eq!(payload["status"], "skipped");
        assert_eq!(payload["reason"], "indexed_file_not_found");
        assert_eq!(payload["skipped_files"], 1);
        assert_eq!(payload["failed_files"], 0);
        assert!(
            !String::from_utf8_lossy(&output.stderr).contains("indexed file was not found"),
            "missing-row tolerance must not emit the old hard failure"
        );
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn prune_retains_recent_unselected_content_without_touching_valid_project() {
        let database_url = test_env::postgres_test_database_url("projection stale tests");
        let mut conn = Client::connect(&database_url, NoTls).expect("connect PostgreSQL");
        let valid_project_id = "11111111-2222-4333-8444-555555555555";
        let orphan_project_id = "66666666-7777-4888-9999-aaaaaaaaaaaa";
        cleanup_project(&mut conn, valid_project_id).expect("pre-clean valid project rows");
        cleanup_project(&mut conn, orphan_project_id).expect("pre-clean orphan project rows");
        let _valid_cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: valid_project_id.to_string(),
        };
        let _orphan_cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: orphan_project_id.to_string(),
        };

        let project = tempfile::tempdir().expect("temp project");
        std::fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
        std::fs::create_dir_all(project.path().join(".git")).expect("create .git");
        std::fs::create_dir_all(project.path().join("src")).expect("create src");
        std::fs::write(project.path().join(FILE_PATH), "pub fn indexed() {}\n")
            .expect("write source file");
        std::fs::write(
            project.path().join(".gobby/gcode.json"),
            serde_json::json!({
                "id": valid_project_id,
                "name": "prune-valid",
                "created_at": "2026-06-24T00:00:00Z"
            })
            .to_string(),
        )
        .expect("write gcode identity");

        seed_project_with_root(
            &mut conn,
            valid_project_id,
            project.path().to_string_lossy(),
        );
        seed_indexed_file_without_project(&mut conn, valid_project_id, FILE_PATH);
        seed_file_state(&mut conn, valid_project_id, FILE_PATH);
        seed_unselected_project_rows(&mut conn, orphan_project_id);

        let home = isolated_gobby_home(project.path());
        let mut command = Command::new(env!("CARGO_BIN_EXE_gcode"));
        command
            .current_dir(project.path())
            .arg("--allow-stale")
            .args(["prune", "--force"]);
        attach_managed_grant(
            &mut command,
            &home,
            valid_project_id,
            gobby_core::grant::DirectConnections::postgres(&database_url),
        );
        let output = command.output().expect("run gcode prune");

        assert!(
            output.status.success(),
            "prune should succeed, stdout={}, stderr={}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            stderr.contains("Content GC: 0 version(s)"),
            "stderr={stderr}"
        );

        assert_eq!(project_child_row_count(&mut conn, orphan_project_id), 5);
        assert_eq!(project_child_row_count(&mut conn, valid_project_id), 2);
        assert_eq!(indexed_project_state_count(&mut conn, valid_project_id), 1);
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn prune_unreachable_falkor_aborts_before_stale_sql_mutation() {
        let database_url = test_env::postgres_test_database_url("projection stale tests");
        let mut conn = Client::connect(&database_url, NoTls).expect("connect PostgreSQL");
        let stale_project_id = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff";
        cleanup_project(&mut conn, stale_project_id).expect("pre-clean stale project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: stale_project_id.to_string(),
        };
        seed_project_with_root(
            &mut conn,
            stale_project_id,
            "/definitely/missing/gcode-prune-stale-project",
        );

        let cwd = tempfile::tempdir().expect("temp cwd");
        std::fs::create_dir_all(cwd.path().join(".gobby")).expect("create .gobby");
        std::fs::write(
            cwd.path().join(".gobby/project.json"),
            serde_json::json!({"id": stale_project_id, "name": "prune-stale"}).to_string(),
        )
        .expect("write project identity");
        let home = isolated_gobby_home(cwd.path());
        let mut command = Command::new(env!("CARGO_BIN_EXE_gcode"));
        command
            .current_dir(cwd.path())
            .arg("--allow-stale")
            .args(["prune", "--force"]);
        attach_managed_grant(
            &mut command,
            &home,
            stale_project_id,
            gobby_core::grant::DirectConnections::postgres(&database_url).with_falkor(
                "127.0.0.1",
                1,
                None,
            ),
        );
        let output = command.output().expect("run gcode prune");

        assert!(
            !output.status.success(),
            "configured unreachable Falkor must fail discovery"
        );
        assert_eq!(indexed_project_state_count(&mut conn, stale_project_id), 1);
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn prune_qdrant_enumeration_failure_aborts_before_stale_sql_mutation() {
        let database_url = test_env::postgres_test_database_url("projection stale tests");
        let mut conn = Client::connect(&database_url, NoTls).expect("connect PostgreSQL");
        let stale_project_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
        cleanup_project(&mut conn, stale_project_id).expect("pre-clean stale project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: stale_project_id.to_string(),
        };
        seed_project_with_root(
            &mut conn,
            stale_project_id,
            "/definitely/missing/gcode-prune-qdrant-stale-project",
        );
        let (qdrant_url, requests) = spawn_http_responses(vec![(
            500,
            serde_json::json!({"status": "enumeration failed"}),
        )]);

        let cwd = tempfile::tempdir().expect("temp cwd");
        std::fs::create_dir_all(cwd.path().join(".gobby")).expect("create .gobby");
        std::fs::write(
            cwd.path().join(".gobby/project.json"),
            serde_json::json!({"id": stale_project_id, "name": "prune-qdrant"}).to_string(),
        )
        .expect("write project identity");
        let home = isolated_gobby_home(cwd.path());
        let mut command = Command::new(env!("CARGO_BIN_EXE_gcode"));
        command
            .current_dir(cwd.path())
            .arg("--allow-stale")
            .args(["prune", "--force"]);
        attach_managed_grant(
            &mut command,
            &home,
            stale_project_id,
            gobby_core::grant::DirectConnections::postgres(&database_url)
                .with_qdrant(qdrant_url, None),
        );
        let output = command.output().expect("run gcode prune");
        let requests = requests
            .join()
            .expect("join Qdrant server")
            .expect("read Qdrant request");

        assert!(
            !output.status.success(),
            "Qdrant enumeration failure must fail discovery"
        );
        assert_eq!(requests.len(), 1);
        assert!(requests[0].contains("GET /collections HTTP/1.1"));
        assert_eq!(indexed_project_state_count(&mut conn, stale_project_id), 1);
    }

    struct ProjectCleanup {
        database_url: String,
        project_id: String,
    }

    impl Drop for ProjectCleanup {
        fn drop(&mut self) {
            if let Ok(mut conn) = Client::connect(&self.database_url, NoTls) {
                let _ = cleanup_project(&mut conn, &self.project_id);
            }
        }
    }

    fn seed_indexed_file(conn: &mut Client, project_id: &str, file_path: &str) {
        seed_project_with_root(conn, project_id, "/tmp/projection-stale");
        seed_indexed_file_without_project(conn, project_id, file_path);
        seed_file_state(conn, project_id, file_path);
    }

    fn seed_project_with_root(conn: &mut Client, project_id: &str, root_path: impl AsRef<str>) {
        let project_id = uuid_param(project_id);
        let machine_id = local_machine_uuid();
        conn.execute(
            "INSERT INTO code_indexed_projects (id) VALUES ($1)",
            &[&project_id],
        )
        .expect("insert indexed project identity");
        conn.execute(
            "INSERT INTO code_indexed_project_states
                (machine_id, project_id, root_path, total_files, total_symbols,
                 last_indexed_at, index_duration_ms)
             VALUES ($1, $2, $3, 1, 1, NOW(), 0)",
            &[&machine_id, &project_id, &root_path.as_ref()],
        )
        .expect("insert indexed project state");
    }

    fn seed_indexed_file_without_project(conn: &mut Client, project_id: &str, file_path: &str) {
        let project_uuid = uuid_param(project_id);
        conn.execute(
            "INSERT INTO code_indexed_files
                (id, project_id, file_path, language, content_hash, symbol_count, byte_size,
                 graph_synced, vectors_synced, graph_sync_attempted_at, indexed_at)
             VALUES ($1, $2, $3, 'rust', 'hash-1', 1, 19, false, true, NULL, NOW())",
            &[&row_uuid(project_id, "file"), &project_uuid, &file_path],
        )
        .expect("insert indexed file");
        conn.execute(
            "INSERT INTO code_symbols
                (id, project_id, file_path, name, qualified_name, kind, language, byte_start,
                 byte_end, line_start, line_end, signature, docstring, parent_symbol_id,
                 file_content_hash, content_hash, summary, created_at, updated_at)
             VALUES ($1, $2, $3, 'indexed', 'crate::indexed', 'function', 'rust', 0, 19,
                 1, 1, 'pub fn indexed()', NULL, NULL, 'hash-1', 'hash-1', NULL, NOW(), NOW())",
            &[&row_uuid(project_id, "symbol"), &project_uuid, &file_path],
        )
        .expect("insert symbol");
    }

    fn seed_file_state(conn: &mut Client, project_id: &str, file_path: &str) {
        let machine_id = local_machine_uuid();
        conn.execute(
            "INSERT INTO code_indexed_file_states
                (machine_id, project_id, file_path, content_hash)
             VALUES ($1, $2, $3, 'hash-1')",
            &[&machine_id, &uuid_param(project_id), &file_path],
        )
        .expect("insert indexed file state");
    }

    fn seed_unselected_project_rows(conn: &mut Client, project_id: &str) {
        conn.execute(
            "INSERT INTO code_indexed_projects (id) VALUES ($1)",
            &[&uuid_param(project_id)],
        )
        .expect("insert unselected project identity");
        seed_indexed_file_without_project(conn, project_id, FILE_PATH);
        let project_uuid = uuid_param(project_id);
        conn.execute(
            "INSERT INTO code_content_chunks
                (id, project_id, file_path, content_hash, chunk_index, line_start, line_end,
                 content, language)
             VALUES ($1, $2, $3, 'hash-1', 0, 1, 1, 'pub fn indexed() {}', 'rust')",
            &[&row_uuid(project_id, "chunk"), &project_uuid, &FILE_PATH],
        )
        .expect("insert content chunk");
        conn.execute(
            "INSERT INTO code_imports (project_id, source_file, content_hash, target_module)
             VALUES ($1, $2, 'hash-1', 'std::fmt')",
            &[&project_uuid, &FILE_PATH],
        )
        .expect("insert import");
        conn.execute(
            "INSERT INTO code_calls
                (project_id, caller_symbol_id, callee_symbol_id, callee_name,
                 callee_target_kind, callee_external_module, file_path, content_hash, line)
             VALUES ($1, $2, NULL, 'missing', 'unresolved', '', $3, 'hash-1', 1)",
            &[&project_uuid, &row_uuid(project_id, "symbol"), &FILE_PATH],
        )
        .expect("insert call");
    }

    fn project_child_row_count(conn: &mut Client, project_id: &str) -> i64 {
        count_rows(conn, "code_indexed_files", project_id)
            + count_rows(conn, "code_symbols", project_id)
            + count_rows(conn, "code_content_chunks", project_id)
            + count_rows(conn, "code_imports", project_id)
            + count_rows(conn, "code_calls", project_id)
    }

    fn indexed_project_state_count(conn: &mut Client, project_id: &str) -> i64 {
        let machine_id = local_machine_uuid();
        conn.query_one(
            "SELECT COUNT(*)::BIGINT FROM code_indexed_project_states
             WHERE machine_id = $1 AND project_id = $2",
            &[&machine_id, &uuid_param(project_id)],
        )
        .expect("count indexed project rows")
        .get(0)
    }

    fn count_rows(conn: &mut Client, table: &str, project_id: &str) -> i64 {
        conn.query_one(
            &format!("SELECT COUNT(*)::BIGINT FROM {table} WHERE project_id = $1"),
            &[&uuid_param(project_id)],
        )
        .expect("count child rows")
        .get(0)
    }

    fn cleanup_project(conn: &mut Client, project_id: &str) -> anyhow::Result<()> {
        let project_id = uuid_param(project_id);
        conn.execute(
            "DELETE FROM code_indexed_file_states WHERE project_id = $1",
            &[&project_id],
        )?;
        conn.execute(
            "DELETE FROM code_indexed_project_states WHERE project_id = $1",
            &[&project_id],
        )?;
        conn.execute(
            "DELETE FROM code_calls
             WHERE project_id = $1
                OR caller_symbol_id IN (SELECT id FROM code_symbols WHERE project_id = $1)
                OR callee_symbol_id IN (SELECT id FROM code_symbols WHERE project_id = $1)",
            &[&project_id],
        )?;
        conn.execute(
            "DELETE FROM code_imports WHERE project_id = $1",
            &[&project_id],
        )?;
        conn.execute(
            "DELETE FROM code_symbols WHERE project_id = $1",
            &[&project_id],
        )?;
        conn.execute(
            "DELETE FROM code_content_chunks WHERE project_id = $1",
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
}
