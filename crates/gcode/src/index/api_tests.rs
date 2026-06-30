use std::time::{SystemTime, UNIX_EPOCH};

use crate::db;
use crate::models::{CallRelation, ImportRelation, IndexedFile, IndexedProject, Symbol};

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
        let mut symbol = test_symbol(&project_id, rel, "tracked", 0, "content-hash-v1");
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
    fn api_upsert_file_resets_projection_sync_flags_on_conflict() {
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

        conn.execute(
            "UPDATE code_indexed_files
         SET graph_synced = true,
             graph_sync_attempted_at = NOW()
         WHERE id = $1",
            &[&file.id],
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
                &[&file.id],
            )
            .expect("load indexed file row");
        let content_hash: String = row.get(0);
        let symbol_count: i32 = row.get(1);
        let byte_size: i32 = row.get(2);
        let graph_synced: bool = row.get(3);
        let vectors_synced: bool = row.get(4);
        let graph_attempt_cleared: bool = row.get(5);
        let vector_attempt_cleared: bool = row.get(6);

        assert_eq!(content_hash, "file-hash-v2");
        assert_eq!(symbol_count, 2);
        assert_eq!(byte_size, 32);
        assert!(!graph_synced, "reindex must mark graph projection stale");
        assert!(!vectors_synced, "reindex must mark vector projection stale");
        assert!(
            graph_attempt_cleared,
            "reindex must clear the previous graph sync attempt timestamp"
        );
        assert!(
            vector_attempt_cleared,
            "reindex must clear the previous vector sync attempt timestamp"
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
        let import = ImportRelation {
            file_path: rel.to_string(),
            module_name: "std::fs".to_string(),
        };
        assert_eq!(
            api::upsert_imports(&mut conn, &project_id, rel, &[import.clone(), import])
                .expect("upsert duplicate imports"),
            1
        );

        let call = CallRelation::new(
            "caller-symbol-id".to_string(),
            "read_to_string".to_string(),
            rel.to_string(),
            7,
        );
        assert_eq!(
            api::upsert_calls(&mut conn, &project_id, rel, &[call.clone(), call])
                .expect("upsert duplicate calls"),
            1
        );

        let import_count: i64 = conn
            .query_one(
                "SELECT COUNT(*) FROM code_imports WHERE project_id = $1",
                &[&project_id],
            )
            .expect("count imports")
            .get(0);
        let call_count: i64 = conn
            .query_one(
                "SELECT COUNT(*) FROM code_calls WHERE project_id = $1",
                &[&project_id],
            )
            .expect("count calls")
            .get(0);
        assert_eq!(import_count, 1);
        assert_eq!(call_count, 1);
    }
}

fn connect_test_db() -> (postgres::Client, String) {
    let database_url = crate::test_env::postgres_test_database_url("postgres API SQL tests");
    let conn = gobby_core::postgres::connect_readwrite(&database_url)
        .expect("connect to PostgreSQL test database");
    (conn, database_url)
}

fn seed_project(conn: &mut postgres::Client, project_id: &str) {
    api::upsert_project_stats(
        conn,
        &IndexedProject {
            id: project_id.to_string(),
            root_path: format!("/tmp/{project_id}"),
            total_files: 1,
            total_symbols: 1,
            last_indexed_at: String::new(),
            index_duration_ms: 0,
            total_eligible_files: None,
        },
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
        id: IndexedFile::make_id(project_id, file_path),
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
    name: &str,
    byte_start: usize,
    content_hash: &str,
) -> Symbol {
    Symbol {
        id: Symbol::make_id(project_id, file_path, name, "function", byte_start),
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
        content_hash: content_hash.to_string(),
        summary: None,
        created_at: String::new(),
        updated_at: String::new(),
    }
}

fn symbol_summary(conn: &mut postgres::Client, symbol_id: &str) -> Option<String> {
    conn.query_one(
        "SELECT summary FROM code_symbols WHERE id = $1",
        &[&symbol_id],
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
            &[&project_id, &file_path],
        )
        .expect("load vector sync state");
    (row.get("vectors_synced"), row.get("attempted"))
}

fn unique_test_project_id(prefix: &str) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is after unix epoch")
        .as_nanos();
    format!("{prefix}-{nanos}")
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
