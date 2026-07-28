use super::{IndexOptions, IndexRequest, index_files};
use crate::config::{Context, ProjectIndexScope};
use crate::db;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

mod serial_db {
    use super::*;

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn discovered_scan_deletes_stale_facts_when_ast_indexing_returns_none() {
        let (mut conn, database_url) = connect_test_db();
        let project_root = tempfile::tempdir().expect("project tempdir");
        let project_id = unique_test_project_id("gcode-discovered-skip-cleanup");
        cleanup_project(&mut conn, &project_id).expect("pre-clean project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };

        write_file(
            project_root.path(),
            "src/lib.rs",
            b"pub fn indexed() -> u8 { 1 }\n",
        );
        let ctx = test_context(
            database_url,
            project_root.path().to_path_buf(),
            project_id.clone(),
        );
        let initial = index_files(
            discovered_request(project_root.path(), true),
            &ctx,
            IndexOptions::default(),
        )
        .expect("initial discovered index");
        assert_eq!(initial.indexed_files, 1);
        assert!(
            symbol_count(&mut conn, &project_id, "src/lib.rs") > 0,
            "initial discovered scan should index at least one symbol"
        );

        write_file(project_root.path(), "src/lib.rs", b"");
        let reindex = index_files(
            discovered_request(project_root.path(), false),
            &ctx,
            IndexOptions::default(),
        )
        .expect("reindex discovered scan");

        assert_eq!(reindex.indexed_files, 0);
        assert_eq!(
            symbol_count(&mut conn, &project_id, "src/lib.rs"),
            0,
            "stale code_symbols rows should be deleted when AST indexing skips an indexed file"
        );
        assert_eq!(
            indexed_file_count(&mut conn, &project_id, "src/lib.rs"),
            0,
            "deleted file facts should not leave the file stale for every later scan"
        );
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn token_named_css_is_indexed_while_plaintext_token_container_is_skipped() {
        let (mut conn, database_url) = connect_test_db();
        let project_root = tempfile::tempdir().expect("project tempdir");
        let project_id = unique_test_project_id("gcode-token-content-policy");
        cleanup_project(&mut conn, &project_id).expect("pre-clean project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };

        write_file(
            project_root.path(),
            "styles/tokens.css",
            b":root { --light: white; }\n",
        );
        write_file(project_root.path(), "token.txt", b"plaintext-token\n");
        let ctx = test_context(
            database_url,
            project_root.path().to_path_buf(),
            project_id.clone(),
        );

        index_files(
            discovered_request(project_root.path(), true),
            &ctx,
            IndexOptions::default(),
        )
        .expect("index token-named files");

        assert!(
            content_chunk_count(&mut conn, &project_id, "styles/tokens.css") > 0,
            "tokens.css should produce content chunks"
        );
        assert_eq!(
            content_chunk_count(&mut conn, &project_id, "token.txt"),
            0,
            "token.txt should remain excluded as a plaintext secret container"
        );
        assert_eq!(
            indexed_file_count(&mut conn, &project_id, "token.txt"),
            0,
            "excluded token.txt should not produce an indexed-file row"
        );
    }
}

fn connect_test_db() -> (postgres::Client, String) {
    let database_url = crate::test_env::postgres_test_database_url("stale cleanup tests");
    let conn = db::connect_readwrite(&database_url)
        .expect("connect stale cleanup PostgreSQL test database");
    (conn, database_url)
}

fn test_context(database_url: String, project_root: PathBuf, project_id: String) -> Context {
    Context {
        database_url,
        project_root,
        project_id,
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: crate::config::CodeVectorSettings { vector_dim: None },
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        index_scope: ProjectIndexScope::Single,
    }
}

fn discovered_request(project_root: &Path, full: bool) -> IndexRequest {
    IndexRequest {
        project_root: project_root.to_path_buf(),
        path_filter: None,
        explicit_files: Vec::new(),
        full,
        require_cpp_semantics: false,
        sync_projections: false,
    }
}

fn write_file(root: &Path, rel: &str, contents: &[u8]) {
    let path = root.join(rel);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).expect("create parent");
    }
    std::fs::write(path, contents).expect("write file");
}

fn unique_test_project_id(prefix: &str) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time after epoch")
        .as_nanos();
    uuid::Uuid::new_v5(
        &crate::models::CODE_INDEX_UUID_NAMESPACE,
        format!("{prefix}-{}-{nanos}", std::process::id()).as_bytes(),
    )
    .to_string()
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

fn cleanup_project(conn: &mut postgres::Client, project_id: &str) -> anyhow::Result<()> {
    let project_id = db::id_param(project_id)?;
    let mut tx = conn.transaction()?;
    tx.execute(
        "DELETE FROM code_calls WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_imports WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_content_chunks WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_symbols WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_indexed_files WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_indexed_projects WHERE id = $1",
        &[&project_id],
    )?;
    tx.commit()?;
    Ok(())
}

fn symbol_count(conn: &mut postgres::Client, project_id: &str, rel: &str) -> i64 {
    count_rows(
        conn,
        "SELECT COUNT(*) FROM code_symbols WHERE project_id = $1 AND file_path = $2",
        project_id,
        rel,
    )
}

fn indexed_file_count(conn: &mut postgres::Client, project_id: &str, rel: &str) -> i64 {
    count_rows(
        conn,
        "SELECT COUNT(*) FROM code_indexed_files WHERE project_id = $1 AND file_path = $2",
        project_id,
        rel,
    )
}

fn content_chunk_count(conn: &mut postgres::Client, project_id: &str, rel: &str) -> i64 {
    count_rows(
        conn,
        "SELECT COUNT(*) FROM code_content_chunks WHERE project_id = $1 AND file_path = $2",
        project_id,
        rel,
    )
}

fn count_rows(conn: &mut postgres::Client, sql: &str, project_id: &str, rel: &str) -> i64 {
    let project_id = db::id_param(project_id).expect("test project id is a uuid");
    conn.query_one(sql, &[&project_id, &rel])
        .expect("count rows")
        .try_get(0)
        .expect("row count")
}
