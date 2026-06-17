#[cfg(test)]
mod serial_db {
    use postgres::{Client, NoTls};
    use serde_json::Value;
    use std::process::Command;

    const POSTGRES_DSN_ENV: &str = "GCODE_POSTGRES_TEST_DATABASE_URL";
    const PROJECT_ID: &str = "gcode-projection-stale-missing-file";
    const FILE_PATH: &str = "src/lib.rs";

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires GCODE_POSTGRES_TEST_DATABASE_URL"
    )]
    #[serial_test::serial(serial_db)]
    fn vector_sync_file_allows_deleted_indexed_file_row() {
        let database_url = std::env::var(POSTGRES_DSN_ENV)
            .expect("GCODE_POSTGRES_TEST_DATABASE_URL must be set for projection stale tests");
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
        conn.execute(
            "INSERT INTO code_indexed_projects
                (id, root_path, total_files, total_symbols, last_indexed_at, index_duration_ms)
             VALUES ($1, '/tmp/projection-stale', 1, 1, NOW(), 0)",
            &[&project_id],
        )
        .expect("insert indexed project");
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
