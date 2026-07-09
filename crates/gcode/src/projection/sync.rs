use std::sync::mpsc::{self, RecvTimeoutError};
use std::thread;
use std::time::Duration;

use super::reconcile_deleted_file;
use crate::config::Context;
use crate::db;
use crate::graph::code_graph::{self, GraphReadError};
use crate::vector::code_symbols::{
    self, CodeSymbolVectorLifecycle, VectorLifecycleError, embedding_source_from_context,
};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProjectionTarget {
    Graph,
    Vectors,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProjectionSyncRequest {
    pub project_id: String,
    pub file_paths: Vec<String>,
    pub targets: Vec<ProjectionTarget>,
}

pub trait ProjectionProgressSink {
    fn start(&mut self, target: ProjectionTarget, total: usize);
    fn advance(&mut self, target: ProjectionTarget, file_path: &str);
    fn finish(&mut self, target: ProjectionTarget);
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProjectionSyncStatus {
    pub project_id: String,
    pub file_paths: Vec<String>,
    pub graph_pending: bool,
    pub vectors_pending: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProjectionStatus {
    Ok,
    Degraded,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProjectionSyncError {
    pub kind: String,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProjectionSyncReport {
    pub status: ProjectionStatus,
    pub synced_files: usize,
    pub synced_symbols: usize,
    pub skipped_files: usize,
    pub failed_files: usize,
    pub degraded: bool,
    pub error: Option<ProjectionSyncError>,
}

impl ProjectionSyncReport {
    pub fn ok(synced_files: usize, synced_symbols: usize) -> Self {
        Self::ok_with_counts(synced_files, synced_symbols, 0, 0)
    }

    pub fn ok_with_counts(
        synced_files: usize,
        synced_symbols: usize,
        skipped_files: usize,
        failed_files: usize,
    ) -> Self {
        Self {
            status: ProjectionStatus::Ok,
            synced_files,
            synced_symbols,
            skipped_files,
            failed_files,
            degraded: false,
            error: None,
        }
    }

    pub fn degraded(
        kind: impl Into<String>,
        message: impl Into<String>,
        synced_files: usize,
        synced_symbols: usize,
    ) -> Self {
        Self::degraded_with_counts(kind, message, synced_files, synced_symbols, 0, 0)
    }

    pub fn degraded_with_counts(
        kind: impl Into<String>,
        message: impl Into<String>,
        synced_files: usize,
        synced_symbols: usize,
        skipped_files: usize,
        failed_files: usize,
    ) -> Self {
        Self {
            status: ProjectionStatus::Degraded,
            synced_files,
            synced_symbols,
            skipped_files,
            failed_files,
            degraded: true,
            error: Some(ProjectionSyncError {
                kind: kind.into(),
                message: message.into(),
            }),
        }
    }

    fn degraded_from_error(
        error: &anyhow::Error,
        synced_files: usize,
        synced_symbols: usize,
    ) -> Self {
        Self::degraded_from_error_with_counts(error, synced_files, synced_symbols, 0, 0)
    }

    fn degraded_from_error_with_counts(
        error: &anyhow::Error,
        synced_files: usize,
        synced_symbols: usize,
        skipped_files: usize,
        failed_files: usize,
    ) -> Self {
        let typed = typed_projection_error(error);
        Self {
            status: ProjectionStatus::Degraded,
            synced_files,
            synced_symbols,
            skipped_files,
            failed_files,
            degraded: true,
            error: Some(typed),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProjectionSyncReports {
    pub graph: ProjectionSyncReport,
    pub vector: ProjectionSyncReport,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ProjectionFileSyncOutcome {
    Synced { symbols: usize },
    SkippedMissingIndexedFile,
}

pub fn pending_after_code_fact_write(request: ProjectionSyncRequest) -> ProjectionSyncStatus {
    ProjectionSyncStatus {
        graph_pending: request.targets.contains(&ProjectionTarget::Graph),
        vectors_pending: request.targets.contains(&ProjectionTarget::Vectors),
        project_id: request.project_id,
        file_paths: request.file_paths,
    }
}

pub fn sync_after_index(
    ctx: &Context,
    file_paths: &[String],
    progress: &mut dyn ProjectionProgressSink,
) -> anyhow::Result<ProjectionSyncReports> {
    let graph = sync_graph_files(ctx, file_paths, Some(progress))?;
    let vector = sync_vector_files(ctx, file_paths, Some(progress))?;
    Ok(ProjectionSyncReports { graph, vector })
}

/// Hard cap on how long the projection-sync phase may run *without observable
/// per-file progress* while it holds the per-project index lock.
///
/// FalkorDB (via the `falkordb` crate) exposes no socket connect/read timeout,
/// so a wedged or unreachable graph backend can block a sync worker forever and
/// pin the advisory lock — observed live as a 10h+ idle hold. Qdrant and the
/// embedding endpoint are already `reqwest`-bounded per request, but neither
/// per-request timeouts nor a whole-repo cap distinguish "slow but progressing"
/// from "wedged". A stall timeout does: every synced file relays a progress
/// event, and only a lack of progress within this window abandons the worker
/// so the caller can drop the lock (#17711).
pub(crate) const DEFAULT_PROJECTION_SYNC_STALL_TIMEOUT: Duration = Duration::from_secs(300);

/// A progress event relayed from the detached sync worker back to the caller.
///
/// Each event doubles as a liveness heartbeat: as long as files keep syncing the
/// caller keeps waiting; silence past the stall timeout means a backend is
/// wedged and the worker must be abandoned to free the lock.
enum ProjectionProgressEvent {
    Start {
        target: ProjectionTarget,
        total: usize,
    },
    Advance {
        target: ProjectionTarget,
        file_path: String,
    },
    Finish {
        target: ProjectionTarget,
    },
}

/// Messages sent from the detached projection-sync worker to the bounded caller.
enum ProjectionPhaseMessage {
    Progress(ProjectionProgressEvent),
    Done(anyhow::Result<ProjectionSyncReports>),
}

/// [`ProjectionProgressSink`] that forwards every event to the bounded caller
/// over a channel instead of rendering it. The receiving side both renders the
/// event and treats it as proof the worker is still making progress.
struct ChannelProgressSink {
    tx: mpsc::Sender<ProjectionPhaseMessage>,
}

impl ProjectionProgressSink for ChannelProgressSink {
    fn start(&mut self, target: ProjectionTarget, total: usize) {
        let _ = self.tx.send(ProjectionPhaseMessage::Progress(
            ProjectionProgressEvent::Start { target, total },
        ));
    }

    fn advance(&mut self, target: ProjectionTarget, file_path: &str) {
        let _ = self.tx.send(ProjectionPhaseMessage::Progress(
            ProjectionProgressEvent::Advance {
                target,
                file_path: file_path.to_string(),
            },
        ));
    }

    fn finish(&mut self, target: ProjectionTarget) {
        let _ = self.tx.send(ProjectionPhaseMessage::Progress(
            ProjectionProgressEvent::Finish { target },
        ));
    }
}

/// Run the post-index projection sync under a stall timeout that guarantees the
/// per-project index lock cannot be pinned indefinitely by a wedged backend.
///
/// The sync runs on a detached worker thread; its progress is relayed back and
/// rendered through `progress`, and each event resets the stall deadline. If no
/// event arrives within `stall_timeout` the worker is abandoned; bounded
/// FalkorDB socket timeouts should let it terminate shortly while degraded
/// reports are returned so the caller's `with_project_lock` guard can drop the
/// lock. Every failure mode folds into degraded reports rather than an error:
/// the PostgreSQL index already succeeded, and best-effort projections must
/// never fail the index command or hold the lock (#17711).
pub fn sync_after_index_bounded(
    ctx: &Context,
    file_paths: &[String],
    stall_timeout: Duration,
    progress: &mut dyn ProjectionProgressSink,
) -> ProjectionSyncReports {
    let worker_ctx = ctx.clone();
    let worker_files = file_paths.to_vec();
    run_projection_phase_bounded(stall_timeout, progress, move |sink| {
        sync_after_index(&worker_ctx, &worker_files, sink)
    })
}

/// Core stall-timeout driver, factored out from [`sync_after_index_bounded`] so
/// it can be exercised without live projection backends. `run` performs the
/// actual sync on a worker thread using the channel-backed sink it is handed.
fn run_projection_phase_bounded<F>(
    stall_timeout: Duration,
    progress: &mut dyn ProjectionProgressSink,
    run: F,
) -> ProjectionSyncReports
where
    F: FnOnce(&mut ChannelProgressSink) -> anyhow::Result<ProjectionSyncReports> + Send + 'static,
{
    let (tx, rx) = mpsc::channel::<ProjectionPhaseMessage>();
    let spawned = thread::Builder::new()
        .name("gcode-projection-sync".to_string())
        .spawn(move || {
            let mut sink = ChannelProgressSink { tx: tx.clone() };
            let reports = run(&mut sink);
            // The receiver may already be gone if we timed out; ignore the error.
            let _ = tx.send(ProjectionPhaseMessage::Done(reports));
        });
    if let Err(error) = spawned {
        // Spawning failed (resource exhaustion). Do not run inline: that would
        // reintroduce the unbounded lock hold this function exists to prevent.
        return degraded_projection_reports(
            "projection_sync_spawn_failed",
            format!("failed to spawn projection sync worker: {error}"),
        );
    }

    let mut active_target: Option<ProjectionTarget> = None;
    loop {
        match rx.recv_timeout(stall_timeout) {
            Ok(ProjectionPhaseMessage::Progress(event)) => {
                active_target = apply_relayed_progress(progress, event);
            }
            Ok(ProjectionPhaseMessage::Done(Ok(reports))) => return reports,
            Ok(ProjectionPhaseMessage::Done(Err(error))) => {
                return degraded_projection_reports("projection_sync_failed", format!("{error:#}"));
            }
            Err(RecvTimeoutError::Timeout) => {
                if let Some(target) = active_target.take() {
                    progress.finish(target);
                }
                let message = format!(
                    "projection sync made no progress for {}s while holding the per-project \
                     index lock; abandoning the sync worker to release the lock (a projection \
                     backend — FalkorDB, Qdrant, or the embedding endpoint — is likely wedged)",
                    stall_timeout.as_secs(),
                );
                log::error!("{message}");
                return degraded_projection_reports("projection_sync_timeout", message);
            }
            Err(RecvTimeoutError::Disconnected) => {
                // Worker dropped its sender without a Done message (it panicked).
                if let Some(target) = active_target.take() {
                    progress.finish(target);
                }
                log::error!("projection sync worker exited without a result");
                return degraded_projection_reports(
                    "projection_sync_worker_lost",
                    "projection sync worker exited without a result".to_string(),
                );
            }
        }
    }
}

/// Render a relayed progress event and report which target is mid-flight so a
/// timeout or panic can cleanly finish that target's progress bar.
fn apply_relayed_progress(
    sink: &mut dyn ProjectionProgressSink,
    event: ProjectionProgressEvent,
) -> Option<ProjectionTarget> {
    match event {
        ProjectionProgressEvent::Start { target, total } => {
            sink.start(target, total);
            Some(target)
        }
        ProjectionProgressEvent::Advance { target, file_path } => {
            sink.advance(target, &file_path);
            Some(target)
        }
        ProjectionProgressEvent::Finish { target } => {
            sink.finish(target);
            None
        }
    }
}

/// Build a fully-degraded [`ProjectionSyncReports`] for both projection targets.
fn degraded_projection_reports(kind: &str, message: String) -> ProjectionSyncReports {
    ProjectionSyncReports {
        graph: ProjectionSyncReport::degraded(kind, message.clone(), 0, 0),
        vector: ProjectionSyncReport::degraded(kind, message, 0, 0),
    }
}

pub(crate) fn sync_files_with_state<S>(
    ctx: &Context,
    file_paths: &[String],
    state: &mut S,
    mut sync_one: impl FnMut(&mut S, &str) -> anyhow::Result<ProjectionFileSyncOutcome>,
    target: ProjectionTarget,
    progress: Option<&mut dyn ProjectionProgressSink>,
) -> ProjectionSyncReport {
    let mut synced_files = 0usize;
    let mut synced_symbols = 0usize;
    let mut skipped_files = 0usize;
    let mut failed_files = 0usize;
    let mut errors = Vec::new();
    let mut error_kind = None;
    let mut progress = ActiveProjectionProgress::new(progress, target, file_paths.len());

    for file_path in file_paths {
        match sync_one(state, file_path) {
            Ok(ProjectionFileSyncOutcome::Synced { symbols }) => {
                synced_files += 1;
                synced_symbols += symbols;
            }
            Ok(ProjectionFileSyncOutcome::SkippedMissingIndexedFile) => {
                skipped_files += 1;
                for failure in reconcile_deleted_file(ctx, file_path) {
                    error_kind.get_or_insert_with(|| "projection_reconcile_failed".to_string());
                    errors.push(format!(
                        "{file_path}: failed to reconcile {:?} projection: {}",
                        failure.target, failure.message
                    ));
                }
            }
            Err(error) => {
                failed_files += 1;
                let typed = typed_projection_error(&error);
                error_kind.get_or_insert(typed.kind);
                errors.push(format!("{file_path}: {}", typed.message));
            }
        }
        progress.advance(file_path);
    }

    if errors.is_empty() {
        ProjectionSyncReport::ok_with_counts(
            synced_files,
            synced_symbols,
            skipped_files,
            failed_files,
        )
    } else {
        ProjectionSyncReport::degraded_with_counts(
            error_kind.unwrap_or_else(|| "sync_failed".to_string()),
            errors.join("; "),
            synced_files,
            synced_symbols,
            skipped_files,
            failed_files,
        )
    }
}

fn sync_graph_files(
    ctx: &Context,
    file_paths: &[String],
    progress: Option<&mut dyn ProjectionProgressSink>,
) -> anyhow::Result<ProjectionSyncReport> {
    if file_paths.is_empty() {
        return Ok(ProjectionSyncReport::ok(0, 0));
    }
    if let Err(error) = code_graph::require_graph_reads(ctx) {
        return Ok(ProjectionSyncReport::degraded_from_error(&error, 0, 0));
    }

    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    let mut progress =
        ActiveProjectionProgress::new(progress, ProjectionTarget::Graph, file_paths.len());
    let report = match code_graph::with_code_graph(ctx, |graph| {
        let mut synced_files = 0usize;
        let mut synced_symbols = 0usize;
        let mut skipped_files = 0usize;
        let mut failed_files = 0usize;
        let mut errors = Vec::new();
        let mut error_kind = None;

        for file_path in file_paths {
            match sync_graph_file(ctx, &mut conn, graph, file_path) {
                Ok(ProjectionFileSyncOutcome::Synced { symbols }) => {
                    synced_files += 1;
                    synced_symbols += symbols;
                }
                Ok(ProjectionFileSyncOutcome::SkippedMissingIndexedFile) => {
                    skipped_files += 1;
                    for failure in reconcile_deleted_file(ctx, file_path) {
                        error_kind.get_or_insert_with(|| "projection_reconcile_failed".to_string());
                        errors.push(format!(
                            "{file_path}: failed to reconcile {:?} projection: {}",
                            failure.target, failure.message
                        ));
                    }
                }
                Err(error) => {
                    failed_files += 1;
                    let typed = typed_projection_error(&error);
                    error_kind.get_or_insert(typed.kind);
                    errors.push(format!("{file_path}: {}", typed.message));
                }
            }
            progress.advance(file_path);
        }

        if errors.is_empty() {
            Ok(ProjectionSyncReport::ok_with_counts(
                synced_files,
                synced_symbols,
                skipped_files,
                failed_files,
            ))
        } else {
            Ok(ProjectionSyncReport::degraded_with_counts(
                error_kind.unwrap_or_else(|| "sync_failed".to_string()),
                errors.join("; "),
                synced_files,
                synced_symbols,
                skipped_files,
                failed_files,
            ))
        }
    }) {
        Ok(report) => report,
        Err(error)
            if matches!(
                error.downcast_ref::<GraphReadError>(),
                Some(GraphReadError::Unreachable { .. })
            ) =>
        {
            return Ok(ProjectionSyncReport::degraded_from_error(&error, 0, 0));
        }
        Err(error) => return Err(error),
    };
    if report.synced_files > 0
        && report.error.is_none()
        && let Err(error) = code_graph::cleanup_orphans(ctx)
    {
        return Ok(ProjectionSyncReport::degraded_from_error_with_counts(
            &error,
            report.synced_files,
            report.synced_symbols,
            report.skipped_files,
            report.failed_files,
        ));
    }
    Ok(report)
}

fn sync_vector_files(
    ctx: &Context,
    file_paths: &[String],
    progress: Option<&mut dyn ProjectionProgressSink>,
) -> anyhow::Result<ProjectionSyncReport> {
    if file_paths.is_empty() {
        return Ok(ProjectionSyncReport::ok(0, 0));
    }

    let lifecycle = match vector_lifecycle_from_context(ctx) {
        Ok(lifecycle) => lifecycle,
        Err(error) => {
            return Ok(ProjectionSyncReport::degraded(
                vector_error_kind(&error),
                error.to_string(),
                0,
                0,
            ));
        }
    };
    let conn = db::connect_readwrite(&ctx.database_url)?;
    let mut state = VectorProjectionState {
        ctx,
        conn,
        lifecycle,
    };
    Ok(sync_files_with_state(
        ctx,
        file_paths,
        &mut state,
        VectorProjectionState::sync_file,
        ProjectionTarget::Vectors,
        progress,
    ))
}

struct ActiveProjectionProgress<'a> {
    sink: Option<&'a mut dyn ProjectionProgressSink>,
    target: ProjectionTarget,
    started: bool,
}

impl<'a> ActiveProjectionProgress<'a> {
    fn new(
        sink: Option<&'a mut dyn ProjectionProgressSink>,
        target: ProjectionTarget,
        total: usize,
    ) -> Self {
        let mut progress = Self {
            sink,
            target,
            started: total > 0,
        };
        if progress.started
            && let Some(sink) = progress.sink.as_deref_mut()
        {
            sink.start(target, total);
        }
        progress
    }

    fn advance(&mut self, file_path: &str) {
        if let Some(sink) = self.sink.as_deref_mut() {
            sink.advance(self.target, file_path);
        }
    }
}

impl Drop for ActiveProjectionProgress<'_> {
    fn drop(&mut self) {
        if self.started
            && let Some(sink) = self.sink.as_deref_mut()
        {
            sink.finish(self.target);
        }
    }
}

fn sync_graph_file(
    ctx: &Context,
    conn: &mut postgres::Client,
    graph: &mut code_graph::CodeGraph<'_>,
    file_path: &str,
) -> anyhow::Result<ProjectionFileSyncOutcome> {
    if !db::mark_graph_sync_attempted(conn, &ctx.project_id, file_path)? {
        return Ok(ProjectionFileSyncOutcome::SkippedMissingIndexedFile);
    }
    let facts = db::read_graph_file_facts(conn, &ctx.project_id, file_path)?;
    graph.sync_file(
        &facts.file_path,
        &facts.imports,
        &facts.definitions,
        &facts.calls,
        false,
    )?;
    if !db::mark_graph_synced(conn, &ctx.project_id, file_path)? {
        return Ok(ProjectionFileSyncOutcome::SkippedMissingIndexedFile);
    }
    Ok(ProjectionFileSyncOutcome::Synced {
        symbols: facts.definitions.len(),
    })
}

struct VectorProjectionState<'a> {
    ctx: &'a Context,
    conn: postgres::Client,
    lifecycle: CodeSymbolVectorLifecycle,
}

impl VectorProjectionState<'_> {
    fn sync_file(&mut self, file_path: &str) -> anyhow::Result<ProjectionFileSyncOutcome> {
        if !db::mark_vector_sync_attempted(&mut self.conn, &self.ctx.project_id, file_path)? {
            return Ok(ProjectionFileSyncOutcome::SkippedMissingIndexedFile);
        }
        let symbols =
            code_symbols::fetch_symbols_for_file(&mut self.conn, &self.ctx.project_id, file_path)?;
        let symbol_count = symbols.len();
        self.lifecycle.sync_file_symbols(file_path, &symbols)?;
        if db::mark_vectors_synced(&mut self.conn, &self.ctx.project_id, file_path)? {
            Ok(ProjectionFileSyncOutcome::Synced {
                symbols: symbol_count,
            })
        } else {
            Ok(ProjectionFileSyncOutcome::SkippedMissingIndexedFile)
        }
    }
}

fn vector_lifecycle_from_context(
    ctx: &Context,
) -> Result<CodeSymbolVectorLifecycle, VectorLifecycleError> {
    let qdrant = ctx
        .qdrant
        .clone()
        .ok_or(VectorLifecycleError::MissingQdrantConfig)?;
    let embedding =
        embedding_source_from_context(ctx).ok_or(VectorLifecycleError::MissingEmbeddingConfig)?;
    CodeSymbolVectorLifecycle::new(
        ctx.project_id.clone(),
        qdrant,
        embedding,
        ctx.code_vectors.clone(),
    )
}

fn typed_projection_error(error: &anyhow::Error) -> ProjectionSyncError {
    let kind = error
        .downcast_ref::<VectorLifecycleError>()
        .map(vector_error_kind)
        .or_else(|| error.downcast_ref::<GraphReadError>().map(graph_error_kind))
        .unwrap_or("sync_failed");
    ProjectionSyncError {
        kind: kind.to_string(),
        message: error.to_string(),
    }
}

fn graph_error_kind(error: &GraphReadError) -> &'static str {
    match error {
        GraphReadError::NotConfigured => "missing_falkordb_config",
        GraphReadError::Unreachable { .. } => "falkordb_unreachable",
        GraphReadError::QueryFailed { .. } => "falkordb_query_failed",
        GraphReadError::InvalidTarget { .. } => "invalid_graph_target",
    }
}

fn vector_error_kind(error: &VectorLifecycleError) -> &'static str {
    match error {
        VectorLifecycleError::MissingQdrantConfig => "missing_qdrant_config",
        VectorLifecycleError::MissingEmbeddingConfig => "missing_embedding_config",
        VectorLifecycleError::EmbeddingHttp { .. } => "embedding_http",
        VectorLifecycleError::EmbeddingResponse(_) => "embedding_response",
        VectorLifecycleError::QdrantHttp { .. } => "qdrant_http",
        VectorLifecycleError::QdrantOperation(_) => "qdrant_operation",
        VectorLifecycleError::InvalidCollectionName(_) => "invalid_collection_name",
        VectorLifecycleError::DimensionMismatch { .. } => "dimension_mismatch",
    }
}

#[cfg(test)]
mod tests {
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
        let reports =
            run_projection_phase_bounded(Duration::from_millis(100), &mut progress, |sink| {
                // Simulate a wedged backend: emit one heartbeat, then block
                // forever with no further progress. The held sender keeps the
                // receiver from ever disconnecting, so this recv never returns —
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
        let reports =
            run_projection_phase_bounded(Duration::from_millis(300), &mut progress, |sink| {
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
        let reports =
            run_projection_phase_bounded(Duration::from_secs(5), &mut progress, |_sink| {
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
            code_vectors: crate::config::CodeVectorSettings { vector_dim: None },
            indexing: gobby_core::config::IndexingConfig::default(),
            daemon_url: None,
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
}
