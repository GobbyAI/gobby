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
    pub graph_file_paths: Vec<String>,
    pub vector_file_paths: Vec<String>,
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
    pub graph_file_paths: Vec<String>,
    pub vector_file_paths: Vec<String>,
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
        graph_file_paths: request.graph_file_paths,
        vector_file_paths: request.vector_file_paths,
    }
}

pub fn sync_after_index(
    ctx: &Context,
    graph_file_paths: &[String],
    vector_file_paths: &[String],
    progress: &mut dyn ProjectionProgressSink,
) -> anyhow::Result<ProjectionSyncReports> {
    let graph = sync_graph_files(ctx, graph_file_paths, Some(progress))?;
    let vector = sync_vector_files(ctx, vector_file_paths, Some(progress))?;
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
    graph_file_paths: &[String],
    vector_file_paths: &[String],
    stall_timeout: Duration,
    progress: &mut dyn ProjectionProgressSink,
) -> ProjectionSyncReports {
    let worker_ctx = ctx.clone();
    let worker_graph = graph_file_paths.to_vec();
    let worker_vectors = vector_file_paths.to_vec();
    run_projection_phase_bounded(stall_timeout, progress, move |sink| {
        sync_after_index(&worker_ctx, &worker_graph, &worker_vectors, sink)
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
    let Some(attempt) = db::mark_graph_sync_attempted(conn, &ctx.project_id, file_path)? else {
        return Ok(ProjectionFileSyncOutcome::SkippedMissingIndexedFile);
    };
    let facts = db::read_graph_file_facts(conn, &ctx.project_id, file_path)?;
    graph.sync_file(
        &facts.file_path,
        &facts.content_hash,
        &facts.imports,
        &facts.definitions,
        &facts.calls,
        &facts.inheritance,
        false,
    )?;
    if !db::mark_graph_synced(
        conn,
        &ctx.project_id,
        file_path,
        &attempt.content_hash,
        attempt.attempted_at,
    )? {
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
    }
}

fn vector_error_kind(error: &VectorLifecycleError) -> &'static str {
    match error {
        VectorLifecycleError::MissingQdrantConfig => "missing_qdrant_config",
        VectorLifecycleError::MissingEmbeddingConfig => "missing_embedding_config",
        #[cfg(feature = "ai")]
        VectorLifecycleError::EmbeddingHttp { .. } => "embedding_http",
        VectorLifecycleError::EmbeddingResponse(_) => "embedding_response",
        VectorLifecycleError::QdrantHttp { .. } => "qdrant_http",
        VectorLifecycleError::QdrantOperation(_) => "qdrant_operation",
        VectorLifecycleError::InvalidCollectionName(_) => "invalid_collection_name",
        VectorLifecycleError::DimensionMismatch { .. } => "dimension_mismatch",
    }
}

#[cfg(test)]
mod tests;
