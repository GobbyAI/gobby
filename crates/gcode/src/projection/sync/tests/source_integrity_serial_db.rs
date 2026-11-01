use super::serial_db::{
    FILE, ProjectCleanup, pinned_fixture, respond, seed_version, unique_project_id, vector_flags,
    version_hash, version_source,
};
use super::*;
use crate::config::{CodeVectorSettings, EmbeddingConfig, QdrantConfig};
use crate::index::{api, hasher};
use crate::models::{IndexedFile, Symbol};
use crate::vector::code_symbols::tests::read_http_request;
use serde_json::{Value, json};
use std::net::TcpListener;
use std::sync::mpsc::{Sender, channel};

const BODY: &str = "fn run() { consume(\"old_body_λ\"); }";
const REVISED_BODY: &str = "fn run() { consume(\"new_body_λ\"); }";

fn source(body: &str) -> String {
    format!("// before_source_café\n{body}\n// after_source_secret\n")
}

fn index_source(conn: &mut postgres::Client, ctx: &Context, body: &str) -> String {
    let source = source(body);
    let hash = hasher::content_hash(source.as_bytes());
    let start = source.find(body).expect("body in source");
    let end = start + body.len();
    seed_version(conn, &ctx.project_id, &hash, false);
    let file = IndexedFile {
        id: IndexedFile::make_id(&ctx.project_id, FILE, &hash),
        project_id: ctx.project_id.clone(),
        file_path: FILE.to_string(),
        language: "rust".to_string(),
        content_hash: hash.clone(),
        symbol_count: 1,
        byte_size: source.len(),
        indexed_at: String::new(),
    };
    api::upsert_file(conn, &file).expect("indexed source version");
    let symbol = Symbol {
        id: Symbol::make_id(&ctx.project_id, FILE, &hash, "run", "function", start),
        project_id: ctx.project_id.clone(),
        file_path: FILE.to_string(),
        name: "run".to_string(),
        qualified_name: "run".to_string(),
        kind: "function".to_string(),
        language: "rust".to_string(),
        byte_start: start,
        byte_end: end,
        line_start: 2,
        line_end: 2,
        signature: Some("fn run()".to_string()),
        docstring: None,
        parent_symbol_id: None,
        file_content_hash: hash.clone(),
        content_hash: hasher::symbol_content_hash(source.as_bytes(), start, end)
            .expect("symbol hash"),
        summary: None,
        created_at: String::new(),
        updated_at: String::new(),
    };
    api::upsert_symbols(conn, &[symbol]).expect("indexed symbol");
    api::file_state::upsert_file_state(
        conn,
        &ctx.machine_id,
        &file,
        &ctx.project_root,
        api::IndexWriteMode::Overlay,
    )
    .expect("current indexed version");
    hash
}

fn service() -> (String, Sender<()>, thread::JoinHandle<Vec<String>>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("test service");
    listener.set_nonblocking(true).expect("nonblocking service");
    let url = format!("http://{}", listener.local_addr().expect("service address"));
    let (stop, stopped) = channel();
    let handle = thread::spawn(move || {
        let mut requests = Vec::new();
        loop {
            match listener.accept() {
                Ok((mut stream, _)) => {
                    let request = read_http_request(&mut stream);
                    let line = request.lines().next().expect("request line");
                    let response = if line.starts_with("GET ") {
                        json!({"result": {"config": {"params": {"vectors": {"size": 3, "distance": "Cosine"}}}}})
                    } else if line.starts_with("POST /v1/embeddings ") {
                        json!({"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]})
                    } else if line.starts_with("PUT ") && line.contains("/points") {
                        json!({"result": {"operation_id": 1, "status": "completed"}, "status": "ok"})
                    } else {
                        panic!("unexpected test request: {line}");
                    };
                    requests.push(request);
                    respond(&mut stream, response);
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    match stopped.recv_timeout(Duration::from_millis(10)) {
                        Err(RecvTimeoutError::Timeout) => {}
                        Ok(()) | Err(RecvTimeoutError::Disconnected) => break,
                    }
                }
                Err(error) => panic!("test service accept: {error}"),
            }
        }
        requests
    });
    (url, stop, handle)
}

fn lifecycle(ctx: &Context, url: &str) -> CodeSymbolVectorLifecycle {
    CodeSymbolVectorLifecycle::new(
        ctx.project_id.clone(),
        QdrantConfig {
            url: Some(url.to_string()),
            api_key: None,
        },
        EmbeddingConfig {
            api_base: format!("{url}/v1"),
            model: "embed-small".to_string(),
            api_key: None,
            query_prefix: None,
            timeout_seconds: 10,
        },
        CodeVectorSettings::with_vector_dim(Some(3)),
    )
    .expect("lifecycle")
    .with_project_root(ctx.project_root.clone())
}

fn embedding_inputs(requests: &[String]) -> Vec<String> {
    requests
        .iter()
        .filter(|request| request.starts_with("POST /v1/embeddings "))
        .map(|request| {
            let (_, body) = request.split_once("\r\n\r\n").expect("request body");
            let value: Value = serde_json::from_str(body).expect("embedding request JSON");
            value["input"][0].as_str().expect("symbol text").to_string()
        })
        .collect()
}

/// Observations from one `exercise_source_sync` run. Each test asserts on them.
struct SourceSyncRun {
    inputs: Vec<String>,
    upserted: bool,
    first: anyhow::Result<ProjectionFileSyncOutcome>,
    first_flags: (bool, bool),
    second: Option<SecondSync>,
}

struct SecondSync {
    outcome: anyhow::Result<ProjectionFileSyncOutcome>,
    flags: (bool, bool),
}

fn exercise_source_sync(disk_body: Option<&str>, reindex: bool) -> SourceSyncRun {
    let database_url =
        crate::test_env::postgres_test_database_url("source integrity projection tests");
    let mut conn = db::connect_readwrite(&database_url).expect("test database");
    let project_id = unique_project_id();
    let _cleanup = ProjectCleanup::new(&mut conn, &database_url, &project_id);
    let root = tempfile::tempdir().expect("source directory");
    std::fs::create_dir(root.path().join("src")).expect("source subdirectory");
    let ctx = Context {
        database_url,
        project_id,
        project_root: root.path().to_path_buf(),
        ..test_context()
    };
    let hash = index_source(&mut conn, &ctx, BODY);
    if let Some(body) = disk_body {
        std::fs::write(root.path().join(FILE), source(body)).expect("worktree source");
    }
    let (url, stop, server) = service();
    let mut state = VectorProjectionState {
        ctx: &ctx,
        conn,
        lifecycle: lifecycle(&ctx, &url),
    };
    let first = state.sync_file(FILE);
    let first_flags = vector_flags(&mut state.conn, &ctx.project_id, &hash);
    let second = if reindex {
        std::fs::write(root.path().join(FILE), source(REVISED_BODY)).expect("body-only edit");
        let revised_hash = index_source(&mut state.conn, &ctx, REVISED_BODY);
        assert!(!vector_flags(&mut state.conn, &ctx.project_id, &revised_hash).0);
        let outcome = state.sync_file(FILE);
        let flags = vector_flags(&mut state.conn, &ctx.project_id, &revised_hash);
        Some(SecondSync { outcome, flags })
    } else {
        None
    };
    stop.send(()).expect("stop test service");
    let requests = server.join().expect("test service requests");
    let upserted = requests.iter().any(|request| request.starts_with("PUT "));
    SourceSyncRun {
        inputs: embedding_inputs(&requests),
        upserted,
        first,
        first_flags,
        second,
    }
}

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn stale_source_is_not_embedded_or_marked_synced() {
    let run = exercise_source_sync(Some(REVISED_BODY), false);
    assert_eq!(
        (run.inputs.len(), run.upserted, run.first_flags.0),
        (0, false, false),
        "rewritten source must not be embedded, upserted or marked synced; inputs={:?}, outcome={:?}",
        run.inputs,
        run.first,
    );
    assert_eq!(
        run.first.expect("rewritten source defers without failing"),
        ProjectionFileSyncOutcome::SkippedSourceChanged,
    );
}

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn missing_source_is_not_embedded_or_marked_synced() {
    let run = exercise_source_sync(None, false);
    assert_eq!(
        (
            run.inputs.len(),
            run.upserted,
            run.first_flags.0,
            run.first.is_err()
        ),
        (0, false, false, true),
        "invalid source must be rejected without embedding, upsert or synced flag; inputs={:?}, outcome={:?}",
        run.inputs,
        run.first,
    );
}

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn valid_source_embeds_exact_stored_range() {
    let run = exercise_source_sync(Some(BODY), false);
    assert_eq!(
        run.first.as_ref().expect("valid source sync"),
        &ProjectionFileSyncOutcome::Synced { symbols: 1 }
    );
    assert!(run.first_flags.0, "valid source should sync");
    assert_eq!(run.inputs.len(), 1);
    assert!(run.inputs[0].contains(BODY));
    assert!(!run.inputs[0].contains("before_source_café"));
    assert!(!run.inputs[0].contains("after_source_secret"));
}

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn body_only_reindex_reembeds_source() {
    let run = exercise_source_sync(Some(BODY), true);
    assert_eq!(
        run.first.as_ref().expect("valid source sync"),
        &ProjectionFileSyncOutcome::Synced { symbols: 1 }
    );
    assert!(run.first_flags.0, "valid source should sync");
    let second = run.second.expect("body-only reindex run");
    assert_eq!(
        second.outcome.expect("body-only reindex sync"),
        ProjectionFileSyncOutcome::Synced { symbols: 1 }
    );
    assert!(second.flags.0, "body-only reindex should sync");
    assert_eq!(run.inputs.len(), 2, "body-only reindex must embed again");
    assert!(run.inputs[0].contains(BODY));
    assert!(!run.inputs[0].contains("before_source_café"));
    assert!(!run.inputs[0].contains("after_source_secret"));
    assert!(run.inputs[1].contains(REVISED_BODY));
    assert!(!run.inputs[1].contains(BODY));
}

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn pinned_source_rewritten_after_indexing_defers_without_marking() {
    let database_url =
        crate::test_env::postgres_test_database_url("pinned source rewritten after indexing");
    let mut conn = db::connect_readwrite(&database_url).expect("connect");
    let (mut ctx, source, _source_cleanup, _overlay_cleanup, root) =
        pinned_fixture(&mut conn, &database_url);
    let (url, stop, server) = service();
    ctx.qdrant = Some(QdrantConfig {
        url: Some(url.clone()),
        api_key: None,
    });
    ctx.embedding = Some(EmbeddingConfig {
        api_base: format!("{url}/v1"),
        model: "embed-small".to_string(),
        api_key: None,
        query_prefix: None,
        timeout_seconds: 10,
    });
    ctx.code_vectors = CodeVectorSettings::with_vector_dim(Some(3));
    std::fs::write(root.path().join(FILE), version_source("rewritten"))
        .expect("rewrite pinned source");
    let hash_h = version_hash("h");
    let mut state = VectorProjectionState {
        ctx: &ctx,
        conn,
        lifecycle: lifecycle(&ctx, &url),
    };
    let mut check = db::connect_readwrite(&database_url).expect("check connection");

    let outcome = state.sync_file(FILE);
    stop.send(()).expect("stop test service");
    let requests = server.join().expect("test service requests");

    assert!(
        embedding_inputs(&requests).is_empty(),
        "rewritten pinned source must not be embedded"
    );
    assert!(
        !requests.iter().any(|request| request.starts_with("PUT ")),
        "rewritten pinned source must not be upserted"
    );
    assert_eq!(
        outcome.expect("rewritten pinned source defers without failing"),
        ProjectionFileSyncOutcome::SkippedSourceChanged,
    );
    assert_eq!(
        vector_flags(&mut check, &source, hash_h.as_str()),
        (false, true),
        "rewritten pinned source must stay unsynced and unattempted"
    );
    let pin = check
        .query_opt(
            "SELECT vector_projected FROM code_overlay_pins
             WHERE overlay_project_id = $1 AND file_path = $2",
            &[&db::id_param(&ctx.project_id).expect("overlay UUID"), &FILE],
        )
        .expect("read current pin");
    assert!(
        !pin.is_some_and(|row| row.get::<_, bool>(0)),
        "rewritten pinned source must not stamp the pin"
    );
}
