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
    let mut progress = CollectingProgress::default();
    // Stall window 300ms; the worker advances every 30ms. Total wall time
    // exceeds the window, but the gap between events never does. A whole-
    // phase cap would abort this; a stall timeout must not.
    let reports = run_projection_phase_bounded(Duration::from_millis(300), &mut progress, |sink| {
        sink.start(ProjectionTarget::Graph, 8);
        for i in 0..8 {
            thread::sleep(Duration::from_millis(30));
            sink.advance(ProjectionTarget::Graph, &format!("src/f{i}.rs"));
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
