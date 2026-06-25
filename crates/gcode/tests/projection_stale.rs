#[cfg(test)]
mod serial_db {
    use postgres::{Client, NoTls};
    use serde_json::Value;
    use std::process::Command;

    use gobby_code::test_env;

    const PROJECT_ID: &str = "gcode-projection-stale-missing-file";
    const FILE_PATH: &str = "src/lib.rs";

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn vector_sync_file_allows_deleted_indexed_file_row() {
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
        conn.execute(
            "DELETE FROM code_indexed_files WHERE project_id = $1 AND file_path = $2",
            &[&PROJECT_ID, &FILE_PATH],
        )
        .expect("delete indexed file row");

        let output = Command::new(env!("CARGO_BIN_EXE_gcode"))
            .current_dir(project.path())
            .env("GCODE_DATABASE_URL", &database_url)
            .arg("--no-freshness")
            .arg("--format")
            .arg("json")
            .args([
                "vector",
                "sync-file",
                "--file",
                FILE_PATH,
                "--allow-missing-indexed-file",
            ])
            .output()
            .expect("run gcode vector sync-file");

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
    fn prune_reconciles_orphan_project_rows_without_touching_valid_project() {
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
        seed_orphan_project_rows(&mut conn, orphan_project_id);

        let output = Command::new(env!("CARGO_BIN_EXE_gcode"))
            .current_dir(project.path())
            .env("GCODE_DATABASE_URL", &database_url)
            .env_remove("GOBBY_FALKORDB_HOST")
            .env_remove("GOBBY_FALKORDB_PORT")
            .env_remove("GOBBY_FALKORDB_PASSWORD")
            .env_remove("GOBBY_QDRANT_URL")
            .env_remove("GOBBY_QDRANT_API_KEY")
            .arg("--project")
            .arg(project.path())
            .arg("--no-freshness")
            .args(["prune", "--force"])
            .output()
            .expect("run gcode prune");

        assert!(
            output.status.success(),
            "prune should succeed, stdout={}, stderr={}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            stderr.contains("Reconciled 1 orphan code-index project(s)"),
            "orphan reconciliation summary missing, stderr={stderr}"
        );

        assert_eq!(project_child_row_count(&mut conn, orphan_project_id), 0);
        assert_eq!(project_child_row_count(&mut conn, valid_project_id), 2);
        assert_eq!(indexed_project_count(&mut conn, valid_project_id), 1);
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
    }

    fn seed_project_with_root(conn: &mut Client, project_id: &str, root_path: impl AsRef<str>) {
        conn.execute(
            "INSERT INTO code_indexed_projects
                (id, root_path, total_files, total_symbols, last_indexed_at, index_duration_ms)
             VALUES ($1, $2, 1, 1, NOW(), 0)",
            &[&project_id, &root_path.as_ref()],
        )
        .expect("insert indexed project");
    }

    fn seed_indexed_file_without_project(conn: &mut Client, project_id: &str, file_path: &str) {
        conn.execute(
            "INSERT INTO code_indexed_files
                (id, project_id, file_path, language, content_hash, symbol_count, byte_size,
                 graph_synced, vectors_synced, graph_sync_attempted_at, indexed_at)
             VALUES ($1, $2, $3, 'rust', 'hash-1', 1, 19, false, true, NULL, NOW())",
            &[&format!("{project_id}-file"), &project_id, &file_path],
        )
        .expect("insert indexed file");
        conn.execute(
            "INSERT INTO code_symbols
                (id, project_id, file_path, name, qualified_name, kind, language, byte_start,
                 byte_end, line_start, line_end, signature, docstring, parent_symbol_id,
                 content_hash, summary, created_at, updated_at)
             VALUES ($1, $2, $3, 'indexed', 'crate::indexed', 'function', 'rust', 0, 19,
                 1, 1, 'pub fn indexed()', NULL, NULL, 'hash-1', NULL, NOW(), NOW())",
            &[&format!("{project_id}-symbol"), &project_id, &file_path],
        )
        .expect("insert symbol");
    }

    fn seed_orphan_project_rows(conn: &mut Client, project_id: &str) {
        seed_indexed_file_without_project(conn, project_id, FILE_PATH);
        conn.execute(
            "INSERT INTO code_content_chunks
                (id, project_id, file_path, chunk_index, line_start, line_end, content, language)
             VALUES ($1, $2, $3, 0, 1, 1, 'pub fn indexed() {}', 'rust')",
            &[&format!("{project_id}-chunk"), &project_id, &FILE_PATH],
        )
        .expect("insert content chunk");
        conn.execute(
            "INSERT INTO code_imports (project_id, source_file, target_module)
             VALUES ($1, $2, 'std::fmt')",
            &[&project_id, &FILE_PATH],
        )
        .expect("insert import");
        conn.execute(
            "INSERT INTO code_calls
                (project_id, caller_symbol_id, callee_symbol_id, callee_name,
                 callee_target_kind, callee_external_module, file_path, line)
             VALUES ($1, $2, '', 'missing', 'unresolved', '', $3, 1)",
            &[&project_id, &format!("{project_id}-symbol"), &FILE_PATH],
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

    fn indexed_project_count(conn: &mut Client, project_id: &str) -> i64 {
        conn.query_one(
            "SELECT COUNT(*)::BIGINT FROM code_indexed_projects WHERE id = $1",
            &[&project_id],
        )
        .expect("count indexed project rows")
        .get(0)
    }

    fn count_rows(conn: &mut Client, table: &str, project_id: &str) -> i64 {
        conn.query_one(
            &format!("SELECT COUNT(*)::BIGINT FROM {table} WHERE project_id = $1"),
            &[&project_id],
        )
        .expect("count child rows")
        .get(0)
    }

    fn cleanup_project(conn: &mut Client, project_id: &str) -> anyhow::Result<()> {
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
