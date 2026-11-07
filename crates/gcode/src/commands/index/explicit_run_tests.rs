//! An explicit-file index run repairs vectors through `run_index_locked`, the entry point the
//! CLI and trigger paths share. The repair stays scoped to the files the run names.

use super::run_index_locked;
use crate::config::{CodeVectorSettings, Context, ProjectIndexScope, QdrantConfig};
use crate::db;
use crate::index::indexer::{IndexOptions, IndexRequest, index_files};
use crate::vector::code_symbols::tests::{accept_with_timeout, read_http_request};
use serde_json::{Value, json};
use std::io::Write;
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const KEPT: &str = "src/kept.rs";
const OTHER: &str = "src/other.rs";

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn explicit_file_run_repairs_only_the_files_it_names() {
    let (mut conn, database_url) = connect_test_db();
    let project_root = project_tempdir();
    let project_id = unique_test_project_id("gcode-explicit-run-repair");
    cleanup_project(&mut conn, &project_id).expect("pre-clean project rows");
    let _cleanup = ProjectCleanup {
        database_url: database_url.clone(),
        project_id: project_id.clone(),
    };
    write_file(project_root.path(), KEPT, b"pub fn kept() -> u8 { 1 }\n");
    write_file(project_root.path(), OTHER, b"pub fn other() -> u8 { 2 }\n");
    let mut ctx = test_context(
        database_url,
        project_root.path().to_path_buf(),
        project_id.clone(),
    );
    index_files(
        IndexRequest {
            project_root: project_root.path().to_path_buf(),
            path_filter: None,
            explicit_files: Vec::new(),
            full: true,
            require_cpp_semantics: false,
            sync_projections: false,
        },
        &ctx,
        IndexOptions::default(),
    )
    .expect("index both files");
    // Both versions hold their vectors. The run below must reset only the file it names.
    mark_versions_synced(&mut conn, &project_id);
    assert!(vectors_synced(&mut conn, &project_id, KEPT));
    assert!(vectors_synced(&mut conn, &project_id, OTHER));

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind qdrant service");
    listener
        .set_nonblocking(true)
        .expect("nonblocking qdrant service");
    let url = format!("http://{}", listener.local_addr().expect("service address"));
    ctx.qdrant = Some(QdrantConfig {
        url: Some(url),
        api_key: None,
    });
    ctx.code_vectors = CodeVectorSettings::with_vector_dim(Some(3));
    let service = thread::spawn(move || serve_qdrant(&listener));

    run_index_locked(
        &ctx,
        IndexRequest {
            project_root: project_root.path().to_path_buf(),
            path_filter: None,
            explicit_files: vec![project_root.path().join(KEPT)],
            full: false,
            require_cpp_semantics: false,
            sync_projections: true,
        },
    )
    .expect("explicit-file index run");
    let requests = service.join().expect("qdrant service thread");

    let scroll_bodies: Vec<Value> = requests
        .iter()
        .filter(|request| request.contains("/points/scroll"))
        .map(|request| request_body(request))
        .collect();
    assert!(
        !scroll_bodies.is_empty(),
        "the explicit-file run never scrolled its collection"
    );
    for body in &scroll_bodies {
        assert_eq!(
            body["filter"]["must"][1]["match"]["any"],
            json!([KEPT]),
            "every scroll must name only the explicit file"
        );
    }
    assert!(
        !requests
            .iter()
            .any(|request| request.contains("/points/delete")),
        "the explicit-file run must not delete points"
    );
    assert!(
        !vectors_synced(&mut conn, &project_id, KEPT),
        "the explicit file's version must reset so the pending-sync worker re-embeds it"
    );
    assert!(
        vectors_synced(&mut conn, &project_id, OTHER),
        "a file the run does not name keeps its vectors"
    );
}

/// Answers the vector-store requests of one run. Each request ends the wait for the next;
/// the first waits long enough for the run to reach the repair.
fn serve_qdrant(listener: &TcpListener) -> Vec<String> {
    let mut requests = Vec::new();
    let mut timeout = Duration::from_secs(30);
    while let Ok(mut stream) = accept_with_timeout(listener, timeout) {
        timeout = Duration::from_secs(5);
        let request = read_http_request(&mut stream);
        let line = request.lines().next().unwrap_or_default();
        let body = if line.starts_with("GET ") {
            json!({"result": {"config": {"params": {"vectors": {"size": 3, "distance": "Cosine"}}}}})
        } else if line.contains("/points/scroll") {
            json!({"result": {"points": [], "next_page_offset": null}})
        } else {
            json!({"result": {"operation_id": 1, "status": "completed"}, "status": "ok"})
        };
        requests.push(request);
        respond(&mut stream, &body);
    }
    requests
}

fn respond(stream: &mut TcpStream, body: &Value) {
    let body = body.to_string();
    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream
        .write_all(response.as_bytes())
        .expect("write qdrant response");
}

fn request_body(request: &str) -> Value {
    let (_, body) = request
        .split_once("\r\n\r\n")
        .expect("request has a header end");
    serde_json::from_str(body).expect("scroll body is JSON")
}

fn mark_versions_synced(conn: &mut postgres::Client, project_id: &str) {
    let project_id = db::id_param(project_id).expect("project id param");
    conn.execute(
        "UPDATE code_indexed_files SET graph_synced = TRUE, vectors_synced = TRUE
         WHERE project_id = $1",
        &[&project_id],
    )
    .expect("mark versions synced");
}

fn vectors_synced(conn: &mut postgres::Client, project_id: &str, rel: &str) -> bool {
    let project_id = db::id_param(project_id).expect("project id param");
    conn.query_one(
        "SELECT vectors_synced FROM code_indexed_files
         WHERE project_id = $1 AND file_path = $2",
        &[&project_id, &rel],
    )
    .expect("read vectors_synced")
    .get(0)
}

fn connect_test_db() -> (postgres::Client, String) {
    let database_url = crate::test_env::postgres_test_database_url("explicit-file index tests");
    let conn = db::connect_readwrite(&database_url)
        .expect("connect explicit-file index PostgreSQL test database");
    (conn, database_url)
}

fn test_context(database_url: String, project_root: PathBuf, project_id: String) -> Context {
    let mut conn =
        db::connect_readwrite(&database_url).expect("connect to register the test checkout");
    crate::test_env::seed_test_checkout(&mut conn, &project_id, &project_root)
        .expect("register explicit-file test checkout");
    Context {
        database_url,
        project_root,
        project_id,
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

fn project_tempdir() -> tempfile::TempDir {
    // Requests must use the canonical root that test_context registers; macOS's default
    // temp directory aliases /private/var.
    let parent = std::env::temp_dir()
        .canonicalize()
        .expect("canonical temp root");
    tempfile::tempdir_in(parent).expect("project tempdir")
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
    for table in [
        "code_indexed_file_states",
        "code_indexed_project_states",
        "code_calls",
        "code_inheritance",
        "code_imports",
        "code_content_chunks",
        "code_symbols",
        "code_indexed_files",
        "project_checkouts",
    ] {
        tx.execute(
            &format!("DELETE FROM {table} WHERE project_id = $1"),
            &[&project_id],
        )?;
    }
    tx.execute(
        "DELETE FROM code_indexed_projects WHERE id = $1",
        &[&project_id],
    )?;
    tx.execute("DELETE FROM projects WHERE id = $1", &[&project_id])?;
    tx.commit()?;
    Ok(())
}
