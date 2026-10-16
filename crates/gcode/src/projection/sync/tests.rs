use super::*;
use std::path::PathBuf;
use std::time::Instant;

#[derive(Default)]
struct CollectingProgress {
    events: Vec<String>,
}

impl ProjectionProgressSink for CollectingProgress {
    fn start(&mut self, target: ProjectionTarget, total: usize) {
        self.events.push(format!("{target:?}:start:{total}"));
    }

    fn advance(&mut self, target: ProjectionTarget, file_path: &str) {
        self.events.push(format!("{target:?}:advance:{file_path}"));
    }

    fn finish(&mut self, target: ProjectionTarget) {
        self.events.push(format!("{target:?}:finish"));
    }
}

#[test]
fn bounded_phase_degrades_and_releases_when_worker_stalls() {
    let mut progress = CollectingProgress::default();
    let start = Instant::now();
    let reports = run_projection_phase_bounded(Duration::from_millis(100), &mut progress, |sink| {
        // Simulate a wedged backend: emit one heartbeat, then block
        // forever with no further progress. A live sender that never sends
        // keeps this receiver blocked indefinitely —
        // a truer stand-in for a wedged socket than a fixed sleep.
        sink.start(ProjectionTarget::Graph, 3);
        sink.advance(ProjectionTarget::Graph, "src/a.rs");
        let (_never_tx, never_rx) = mpsc::channel::<()>();
        let _ = never_rx.recv();
        Ok(ProjectionSyncReports {
            graph: ProjectionSyncReport::ok(3, 0),
            vector: ProjectionSyncReport::ok(0, 0),
        })
    });
    let elapsed = start.elapsed();

    assert!(reports.graph.degraded);
    assert!(reports.vector.degraded);
    assert_eq!(
        reports
            .graph
            .error
            .as_ref()
            .map(|error| error.kind.as_str()),
        Some("projection_sync_timeout")
    );
    // The lock must be released promptly, long before the worker's 2s block.
    assert!(elapsed < Duration::from_secs(1), "elapsed {elapsed:?}");
    // The mid-flight target's progress bar was finished on timeout.
    assert!(progress.events.iter().any(|event| event == "Graph:finish"));
}

#[test]
fn bounded_phase_passes_through_successful_reports() {
    let mut progress = CollectingProgress::default();
    let reports = run_projection_phase_bounded(Duration::from_secs(5), &mut progress, |sink| {
        sink.start(ProjectionTarget::Vectors, 1);
        sink.advance(ProjectionTarget::Vectors, "src/a.rs");
        sink.finish(ProjectionTarget::Vectors);
        Ok(ProjectionSyncReports {
            graph: ProjectionSyncReport::ok(0, 0),
            vector: ProjectionSyncReport::ok(1, 4),
        })
    });

    assert!(!reports.vector.degraded);
    assert_eq!(reports.vector.status, ProjectionStatus::Ok);
    assert_eq!(reports.vector.synced_files, 1);
    assert_eq!(reports.vector.synced_symbols, 4);
    assert_eq!(
        progress.events,
        vec![
            "Vectors:start:1",
            "Vectors:advance:src/a.rs",
            "Vectors:finish"
        ]
    );
}

#[test]
fn bounded_phase_does_not_time_out_while_progress_continues() {
    let (ack_tx, ack_rx) = mpsc::channel::<()>();
    let mut progress = AckingProgress {
        inner: CollectingProgress::default(),
        ack: ack_tx,
    };
    // The worker advances only after the caller has relayed its previous
    // event, so every gap between events is bounded by relay work and the
    // phase must run to completion without a stall.
    let reports =
        run_projection_phase_bounded(Duration::from_secs(5), &mut progress, move |sink| {
            sink.start(ProjectionTarget::Graph, 8);
            for i in 0..8 {
                sink.advance(ProjectionTarget::Graph, &format!("src/f{i}.rs"));
                ack_rx.recv().expect("caller relayed the advance");
            }
            sink.finish(ProjectionTarget::Graph);
            Ok(ProjectionSyncReports {
                graph: ProjectionSyncReport::ok(8, 16),
                vector: ProjectionSyncReport::ok(0, 0),
            })
        });

    assert!(
        !reports.graph.degraded,
        "steady progress must not be treated as a stall"
    );
    assert_eq!(reports.graph.synced_files, 8);
    assert_eq!(progress.inner.events.len(), 10, "start, 8 advances, finish");
}

/// Relays to [`CollectingProgress`] and acknowledges each advance to the
/// worker that emitted it.
struct AckingProgress {
    inner: CollectingProgress,
    ack: mpsc::Sender<()>,
}

impl ProjectionProgressSink for AckingProgress {
    fn start(&mut self, target: ProjectionTarget, total: usize) {
        self.inner.start(target, total);
    }

    fn advance(&mut self, target: ProjectionTarget, file_path: &str) {
        self.inner.advance(target, file_path);
        self.ack.send(()).expect("worker awaits the ack");
    }

    fn finish(&mut self, target: ProjectionTarget) {
        self.inner.finish(target);
    }
}

#[test]
fn bounded_phase_degrades_when_worker_panics() {
    let mut progress = CollectingProgress::default();
    let reports = run_projection_phase_bounded(Duration::from_secs(5), &mut progress, |_sink| {
        panic!("simulated projection backend panic")
    });

    assert!(reports.graph.degraded);
    assert!(reports.vector.degraded);
    assert_eq!(
        reports
            .graph
            .error
            .as_ref()
            .map(|error| error.kind.as_str()),
        Some("projection_sync_worker_lost")
    );
}

fn test_context() -> Context {
    Context {
        database_url: "postgresql://localhost/nonexistent".to_string(),
        project_root: PathBuf::from("/nonexistent"),
        project_id: "project-1".to_string(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: crate::config::CodeVectorSettings::default(),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: crate::config::ProjectIndexScope::Single,
    }
}

#[test]
fn sync_state_continues_after_projection_errors() {
    let files = vec![
        "src/ok.rs".to_string(),
        "src/fail.rs".to_string(),
        "src/next.rs".to_string(),
    ];
    #[derive(Default)]
    struct State {
        synced: Vec<String>,
    }
    let mut state = State::default();

    let report = sync_files_with_state(
        &test_context(),
        &files,
        &mut state,
        |state, file_path| {
            state.synced.push(file_path.to_string());
            if file_path == "src/fail.rs" {
                anyhow::bail!("projection write failed");
            }
            Ok(ProjectionFileSyncOutcome::Synced { symbols: 3 })
        },
        ProjectionTarget::Vectors,
        None,
    );

    assert_eq!(
        state.synced,
        vec!["src/ok.rs", "src/fail.rs", "src/next.rs"]
    );
    assert_eq!(report.status, ProjectionStatus::Degraded);
    assert_eq!(report.synced_files, 2);
    assert_eq!(report.synced_symbols, 6);
    assert_eq!(report.skipped_files, 0);
    assert_eq!(report.failed_files, 1);
    assert!(report.degraded);
    assert_eq!(
        report.error.as_ref().map(|error| error.kind.as_str()),
        Some("sync_failed")
    );
}

#[test]
fn sync_state_treats_missing_indexed_file_as_non_degraded_skip() {
    let files = vec!["src/missing.rs".to_string(), "src/ok.rs".to_string()];
    #[derive(Default)]
    struct State {
        synced: Vec<String>,
    }
    let mut state = State::default();

    let report = sync_files_with_state(
        &test_context(),
        &files,
        &mut state,
        |state, file_path| {
            state.synced.push(file_path.to_string());
            if file_path == "src/missing.rs" {
                return Ok(ProjectionFileSyncOutcome::SkippedMissingIndexedFile);
            }
            Ok(ProjectionFileSyncOutcome::Synced { symbols: 2 })
        },
        ProjectionTarget::Vectors,
        None,
    );

    assert_eq!(state.synced, vec!["src/missing.rs", "src/ok.rs"]);
    assert_eq!(report.status, ProjectionStatus::Ok);
    assert_eq!(report.synced_files, 1);
    assert_eq!(report.synced_symbols, 2);
    assert_eq!(report.skipped_files, 1);
    assert_eq!(report.failed_files, 0);
    assert!(!report.degraded);
    assert!(report.error.is_none());
}

#[test]
fn sync_state_reports_projection_progress_for_each_file() {
    let files = vec!["src/one.rs".to_string(), "src/two.rs".to_string()];
    #[derive(Default)]
    struct State;
    let mut state = State;
    #[derive(Default)]
    struct RecordingProgress {
        events: Vec<String>,
    }
    impl ProjectionProgressSink for RecordingProgress {
        fn start(&mut self, target: ProjectionTarget, total: usize) {
            self.events.push(format!("{target:?}:start:{total}"));
        }

        fn advance(&mut self, target: ProjectionTarget, file_path: &str) {
            self.events.push(format!("{target:?}:advance:{file_path}"));
        }

        fn finish(&mut self, target: ProjectionTarget) {
            self.events.push(format!("{target:?}:finish"));
        }
    }
    let mut progress = RecordingProgress::default();

    let report = sync_files_with_state(
        &test_context(),
        &files,
        &mut state,
        |_state, _file_path| Ok(ProjectionFileSyncOutcome::Synced { symbols: 1 }),
        ProjectionTarget::Graph,
        Some(&mut progress),
    );

    assert_eq!(report.status, ProjectionStatus::Ok);
    assert_eq!(
        progress.events,
        vec![
            "Graph:start:2",
            "Graph:advance:src/one.rs",
            "Graph:advance:src/two.rs",
            "Graph:finish"
        ]
    );
}

#[test]
fn sync_state_empty_files_does_not_start_progress() {
    let files: Vec<String> = Vec::new();
    #[derive(Default)]
    struct State;
    let mut state = State;
    #[derive(Default)]
    struct RecordingProgress {
        events: Vec<String>,
    }
    impl ProjectionProgressSink for RecordingProgress {
        fn start(&mut self, target: ProjectionTarget, total: usize) {
            self.events.push(format!("{target:?}:start:{total}"));
        }

        fn advance(&mut self, target: ProjectionTarget, file_path: &str) {
            self.events.push(format!("{target:?}:advance:{file_path}"));
        }

        fn finish(&mut self, target: ProjectionTarget) {
            self.events.push(format!("{target:?}:finish"));
        }
    }
    let mut progress = RecordingProgress::default();

    let report = sync_files_with_state(
        &test_context(),
        &files,
        &mut state,
        |_state, _file_path| Ok(ProjectionFileSyncOutcome::Synced { symbols: 1 }),
        ProjectionTarget::Graph,
        Some(&mut progress),
    );

    assert_eq!(report.status, ProjectionStatus::Ok);
    assert!(progress.events.is_empty());
}

mod serial_db {
    use super::*;
    use crate::config::{CodeVectorSettings, EmbeddingConfig, QdrantConfig};
    use crate::index::api;
    use crate::models::{IndexedFile, IndexedProject, Symbol};
    use crate::vector::code_symbols::tests::{accept_with_timeout, read_http_request};
    use serde_json::{Value, json};
    use std::io::Write as _;
    use std::net::{TcpListener, TcpStream};
    use std::time::{SystemTime, UNIX_EPOCH};

    const FILE: &str = "src/lib.rs";

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn vector_sync_file_skips_when_state_moves_during_upsert() {
        let database_url = crate::test_env::postgres_test_database_url("projection sync tests");
        let mut conn = db::connect_readwrite(&database_url).expect("connect");
        let project_id = unique_project_id();
        let _cleanup = ProjectCleanup::new(&mut conn, &database_url, &project_id);
        seed_version(&mut conn, &project_id, "hash-h", true);

        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
        listener
            .set_nonblocking(true)
            .expect("set test server nonblocking");
        let url = format!("http://{}", listener.local_addr().expect("local addr"));
        let server = {
            let database_url = database_url.clone();
            let project_id = project_id.clone();
            thread::spawn(move || {
                serve_until_upsert(&listener, || {
                    let mut writer = db::connect_readwrite(&database_url).expect("writer");
                    seed_version(&mut writer, &project_id, "hash-k", false);
                })
            })
        };

        let ctx = Context {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
            ..test_context()
        };
        let lifecycle = CodeSymbolVectorLifecycle::new(
            project_id.clone(),
            QdrantConfig {
                url: Some(url.clone()),
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
        .expect("lifecycle");
        let mut state = VectorProjectionState {
            ctx: &ctx,
            conn,
            lifecycle,
        };

        let outcome = state.sync_file(FILE).expect("sync file");
        let requests = server.join().expect("test server");

        assert!(
            requests.iter().any(|line| line.contains("/points")),
            "the upsert ran: {requests:?}"
        );
        assert_eq!(
            outcome,
            ProjectionFileSyncOutcome::SkippedMissingIndexedFile
        );
        let mut check = db::connect_readwrite(&database_url).expect("connect");
        assert_eq!(
            vector_flags(&mut check, &project_id, "hash-k"),
            (false, true),
            "the new current version stays pending with no attempt"
        );
        assert!(
            !vector_flags(&mut check, &project_id, "hash-h").0,
            "the superseded version is not marked synced"
        );
    }

    /// Answers the lifecycle's Qdrant and embedding requests. Before it answers
    /// the upsert, it runs `on_upsert`, then stops. Returns each request line.
    fn serve_until_upsert(listener: &TcpListener, on_upsert: impl FnOnce()) -> Vec<String> {
        let mut on_upsert = Some(on_upsert);
        let mut lines = Vec::new();
        while let Some(hook) = on_upsert.take() {
            let mut stream =
                accept_with_timeout(listener, Duration::from_secs(5)).expect("accept request");
            let request = read_http_request(&mut stream);
            let line = request.lines().next().unwrap_or_default().to_string();
            lines.push(line.clone());
            if line.starts_with("GET ") {
                on_upsert = Some(hook);
                respond(
                    &mut stream,
                    json!({"result": {"config": {"params": {"vectors": {"size": 3, "distance": "Cosine"}}}}}),
                );
            } else if line.starts_with("PUT ") && line.contains("/points") {
                hook();
                respond(
                    &mut stream,
                    json!({"result": {"operation_id": 1, "status": "completed"}, "status": "ok"}),
                );
            } else if line.starts_with("POST ") && !line.contains("/collections") {
                on_upsert = Some(hook);
                respond(
                    &mut stream,
                    json!({"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}),
                );
            } else {
                panic!("unexpected test request: {line}");
            }
        }
        lines
    }

    fn respond(stream: &mut TcpStream, body: Value) {
        let body = body.to_string();
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
        .expect("write response");
    }

    /// Writes version `hash` of `FILE` and points this machine's state at it.
    fn seed_version(conn: &mut postgres::Client, project_id: &str, hash: &str, with_symbol: bool) {
        let machine_id = gobby_core::machine::read_local_machine_id().expect("machine");
        let file = IndexedFile {
            id: IndexedFile::make_id(project_id, FILE, hash),
            project_id: project_id.to_string(),
            file_path: FILE.to_string(),
            language: "rust".to_string(),
            content_hash: hash.to_string(),
            symbol_count: usize::from(with_symbol),
            byte_size: 16,
            indexed_at: String::new(),
        };
        api::upsert_file(conn, &file).expect("upsert content version");
        if with_symbol {
            let symbol = Symbol {
                id: Symbol::make_id(project_id, FILE, hash, "run", "function", 0),
                project_id: project_id.to_string(),
                file_path: FILE.to_string(),
                name: "run".to_string(),
                qualified_name: "run".to_string(),
                kind: "function".to_string(),
                language: "rust".to_string(),
                byte_start: 0,
                byte_end: 3,
                line_start: 1,
                line_end: 1,
                signature: Some("fn run()".to_string()),
                docstring: None,
                parent_symbol_id: None,
                file_content_hash: hash.to_string(),
                content_hash: format!("{hash}-run"),
                summary: None,
                created_at: String::new(),
                updated_at: String::new(),
            };
            api::upsert_symbols(conn, &[symbol]).expect("upsert symbol");
        }
        api::file_state::upsert_file_state(
            conn,
            &machine_id,
            &file,
            std::path::Path::new("/tmp/projection-sync"),
            api::IndexWriteMode::Overlay,
        )
        .expect("point file state");
    }

    /// Returns `(vectors_synced, attempt_is_null)` for version `hash` of `FILE`.
    fn vector_flags(conn: &mut postgres::Client, project_id: &str, hash: &str) -> (bool, bool) {
        let project_uuid = db::id_param(project_id).expect("uuid");
        let row = conn
            .query_one(
                "SELECT vectors_synced, vector_sync_attempted_at IS NULL
                 FROM code_indexed_files
                 WHERE project_id = $1 AND file_path = $2 AND content_hash = $3",
                &[&project_uuid, &FILE, &hash],
            )
            .expect("load vector flags");
        (row.get(0), row.get(1))
    }

    fn unique_project_id() -> String {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("epoch")
            .as_nanos();
        uuid::Uuid::new_v5(
            &crate::models::CODE_INDEX_UUID_NAMESPACE,
            format!("gcode-projection-sync-{}-{nanos}", std::process::id()).as_bytes(),
        )
        .to_string()
    }

    struct ProjectCleanup {
        database_url: String,
        project_id: String,
    }

    impl ProjectCleanup {
        /// Clears leftovers, seeds this machine's project state, and removes
        /// the project's rows on drop.
        fn new(conn: &mut postgres::Client, database_url: &str, project_id: &str) -> Self {
            cleanup(conn, project_id).expect("pre-clean test project rows");
            let machine_id = gobby_core::machine::read_local_machine_id().expect("machine");
            api::upsert_project_stats(
                conn,
                &machine_id,
                &IndexedProject {
                    id: project_id.to_string(),
                    root_path: format!("/tmp/{project_id}"),
                    total_files: 1,
                    total_symbols: 1,
                    last_indexed_at: String::new(),
                    index_duration_ms: 0,
                    total_eligible_files: None,
                    indexer_version: None,
                },
                api::IndexWriteMode::Overlay,
                true,
            )
            .expect("seed project");
            Self {
                database_url: database_url.to_string(),
                project_id: project_id.to_string(),
            }
        }
    }

    impl Drop for ProjectCleanup {
        fn drop(&mut self) {
            if let Ok(mut conn) = db::connect_readwrite(&self.database_url) {
                let _ = cleanup(&mut conn, &self.project_id);
            }
        }
    }

    fn cleanup(conn: &mut postgres::Client, project_id: &str) -> anyhow::Result<()> {
        let project_uuid = db::id_param(project_id)?;
        for statement in [
            "DELETE FROM code_indexed_file_states WHERE project_id = $1",
            "DELETE FROM code_indexed_project_states WHERE project_id = $1",
            "DELETE FROM code_symbols WHERE project_id = $1",
            "DELETE FROM code_indexed_files WHERE project_id = $1",
            "DELETE FROM code_indexed_projects WHERE id = $1",
        ] {
            conn.execute(statement, &[&project_uuid])?;
        }
        Ok(())
    }
}
