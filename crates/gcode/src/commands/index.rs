use crate::commands::vector::reconcile as vector_reconcile;
use crate::config::Context;
use crate::graph::report::hotspot_snapshot;
use crate::index::api::{
    self, IndexDegradation, IndexOutcome, IndexProgressSink, IndexRequest, UnsupportedFileType,
};
use crate::index_lock::{self, IndexLockPolicy, IndexLockResult, LockWaitTimeout};
use crate::index_pending;
use crate::output::{self, Format};
use crate::projection::sync::{
    self, ProjectionProgressSink, ProjectionSyncReports, ProjectionTarget,
};
use crate::utils::short_id;
use gobby_core::progress::ProgressBar;
use serde::Serialize;
use serde_json::Value;
use std::io::Write as _;
use std::path::PathBuf;
use std::time::{Duration, Instant};

/// How one index run ended. `Skipped` means a busy lock was not waited on and no index work ran.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum IndexRunStatus {
    Completed,
    Skipped,
}

pub(crate) enum RunIndexLockedOutput {
    IndexOnly(IndexOutcome),
    Projections(IndexSyncProjectionsOutput),
}

/// Lock policy for an index run: `skip_if_locked` yields at once, `lock_wait_seconds`
/// bounds the wait, and an unbounded run keeps the default wait.
fn index_lock_policy(skip_if_locked: bool, lock_wait_seconds: Option<u64>) -> IndexLockPolicy {
    if skip_if_locked {
        IndexLockPolicy::brief_index_flush_try()
    } else if let Some(seconds) = lock_wait_seconds {
        IndexLockPolicy::Wait {
            max_wait: Duration::from_secs(seconds),
        }
    } else {
        IndexLockPolicy::wait()
    }
}

// Args map 1:1 to the `gcode index` CLI flags; a wrapper struct would only add
// indirection between clap and this entry point.
#[allow(clippy::too_many_arguments)]
pub fn run(
    ctx: &Context,
    cwd: &std::path::Path,
    path: Option<String>,
    files: Option<Vec<String>>,
    full: bool,
    require_cpp_semantics: bool,
    sync_projections: bool,
    skip_if_locked: bool,
    lock_wait_seconds: Option<u64>,
    format: Format,
) -> anyhow::Result<()> {
    let mut timings = crate::index::indexer::IndexTimings::new("command.context");
    let (target_ctx, path_filter) = resolve_index_context(ctx, path.as_deref())?;
    let explicit_files: Vec<std::path::PathBuf> = files
        .unwrap_or_default()
        .into_iter()
        .map(|file| {
            crate::commands::scope::resolve_path_input(
                &target_ctx,
                cwd,
                crate::commands::scope::ScopedPathInput::ExactFile(&file),
            )
            .map(std::path::PathBuf::from)
        })
        .collect::<Result<_, _>>()?;
    let request = IndexRequest {
        project_root: target_ctx.project_root.clone(),
        path_filter: if explicit_files.is_empty() {
            path_filter
        } else {
            None
        },
        explicit_files,
        full,
        require_cpp_semantics,
        sync_projections,
    };

    let lock_policy = index_lock_policy(skip_if_locked, lock_wait_seconds);
    match run_with_lock_policy(
        &target_ctx,
        request,
        lock_policy,
        skip_if_locked,
        format,
        &mut timings,
    )? {
        IndexRunStatus::Completed => Ok(()),
        // The lock was busy and the caller did not ask to wait: no index work ran.
        IndexRunStatus::Skipped => std::process::exit(3),
    }
}

/// Runs one index request under `lock_policy`. `Skipped` means a busy lock was not waited on
/// and no index work ran; the caller chooses the exit status.
fn run_with_lock_policy(
    target_ctx: &Context,
    request: IndexRequest,
    lock_policy: IndexLockPolicy,
    skip_if_locked: bool,
    format: Format,
    timings: &mut crate::index::indexer::IndexTimings,
) -> anyhow::Result<IndexRunStatus> {
    // Paths queued by earlier timed-out runs go first. The drain and this run share one wait
    // bound, so this run gets only what the drain left of it.
    let drain_started = Instant::now();
    drain_queued_paths(target_ctx, &request, lock_policy)?;
    let lock_policy = index_lock::remaining_policy(lock_policy, drain_started.elapsed());
    let explicit_request = !request.explicit_files.is_empty();
    timings.phase("command.locks");
    let (run_output, completed_files, busy_files) = if explicit_request {
        let locks = match index_lock::lock_project_files(
            target_ctx,
            &request.explicit_files,
            lock_policy,
        ) {
            Ok(locks) => locks,
            Err(error) => {
                return Err(queue_on_lock_timeout(
                    target_ctx,
                    &request.explicit_files,
                    error,
                ));
            }
        };
        if locks.acquired_files.is_empty() {
            print_all_files_busy(&locks.busy_files, format)?;
            std::io::stdout().flush()?;
            return Ok(IndexRunStatus::Skipped);
        }
        let mut acquired_request = request;
        acquired_request.explicit_files = locks.acquired_files.clone();
        timings.phase("command.index");
        let run_output = run_index_locked(target_ctx, acquired_request)?;
        (
            run_output,
            locks.acquired_files.clone(),
            locks.busy_files.clone(),
        )
    } else {
        let run_output = index_lock::with_project_lock(target_ctx, lock_policy, || {
            timings.phase("command.index");
            run_index_locked(target_ctx, request)
        })?;
        let run_output = match run_output {
            IndexLockResult::Acquired(run_output) => run_output,
            IndexLockResult::Busy(holder) => {
                let holder = holder
                    .map(|holder| format!("; {holder}"))
                    .unwrap_or_default();
                if skip_if_locked {
                    if !target_ctx.quiet {
                        eprintln!(
                            "index lock busy for project {}; skipped (another indexer is running{})",
                            target_ctx.project_id, holder
                        );
                    }
                    return Ok(IndexRunStatus::Skipped);
                }
                anyhow::bail!(
                    "index lock is busy for project {}; wait policy did not acquire it{}",
                    target_ctx.project_id,
                    holder
                )
            }
        };
        (run_output, Vec::new(), Vec::new())
    };

    timings.phase("command.output");
    match run_output {
        RunIndexLockedOutput::Projections(payload) => {
            match format {
                Format::Json if explicit_request => output::print_json(
                    &explicit_index_json_payload(&payload, &completed_files, &busy_files)?,
                ),
                Format::Json => output::print_json(&payload),
                Format::Text if explicit_request => output::print_text(&explicit_index_text(
                    sync_projections_text(&payload)?,
                    &completed_files,
                    &busy_files,
                )?),
                Format::Text => output::print_text(&sync_projections_text(&payload)?),
            }
        }
        RunIndexLockedOutput::IndexOnly(outcome) => {
            match format {
                Format::Json if explicit_request => output::print_json(
                    &explicit_index_json_payload(&outcome, &completed_files, &busy_files)?,
                ),
                Format::Json => output::print_json(&outcome),
                Format::Text if explicit_request => output::print_text(&explicit_index_text(
                    index_text(&outcome),
                    &completed_files,
                    &busy_files,
                )?),
                Format::Text => output::print_text(&index_text(&outcome)),
            }
        }
    }?;
    Ok(IndexRunStatus::Completed)
}

/// Indexes the paths that earlier timed-out runs queued, then deletes each batch whose paths
/// are no longer busy. A batch the lock still holds stays queued for the next run.
fn drain_queued_paths(
    target_ctx: &Context,
    request: &IndexRequest,
    lock_policy: IndexLockPolicy,
) -> anyhow::Result<()> {
    let batches = index_pending::take(&index_pending::pending_dir(&target_ctx.project_id)?)?;
    if batches.is_empty() {
        return Ok(());
    }
    let mut paths: Vec<PathBuf> = batches
        .iter()
        .flat_map(|batch| batch.paths.iter().cloned())
        .collect();
    paths.sort();
    paths.dedup();
    let locks = match index_lock::lock_project_files(target_ctx, &paths, lock_policy) {
        Ok(locks) => locks,
        Err(error) if error.is::<LockWaitTimeout>() => {
            eprintln!(
                "index lock wait timed out; {} queued path(s) stay queued for the next index run",
                paths.len()
            );
            return Ok(());
        }
        Err(error) => return Err(error),
    };
    if !locks.acquired_files.is_empty() {
        run_index_locked(
            target_ctx,
            IndexRequest {
                project_root: target_ctx.project_root.clone(),
                path_filter: None,
                explicit_files: locks.acquired_files.clone(),
                full: false,
                require_cpp_semantics: request.require_cpp_semantics,
                sync_projections: request.sync_projections,
            },
        )?;
    }
    for batch in &batches {
        if batch
            .paths
            .iter()
            .all(|path| !locks.busy_files.contains(path))
        {
            index_pending::settle(batch)?;
        }
    }
    Ok(())
}

/// Queues the paths of an explicit run whose bounded lock wait ran out, then reports how many
/// were queued. Any other lock error passes through unchanged.
fn queue_on_lock_timeout(
    target_ctx: &Context,
    paths: &[PathBuf],
    error: anyhow::Error,
) -> anyhow::Error {
    if !error.is::<LockWaitTimeout>() {
        return error;
    }
    match queue_paths(target_ctx, paths) {
        Ok(count) => {
            eprintln!("index lock wait timed out; queued {count} path(s) for the next index run");
            error
        }
        Err(queue_error) => error.context(format!(
            "could not queue timed-out index paths: {queue_error:#}"
        )),
    }
}

fn queue_paths(target_ctx: &Context, paths: &[PathBuf]) -> anyhow::Result<usize> {
    let mut relative = paths
        .iter()
        .map(|path| index_lock::normalize_file_lock_path(&target_ctx.project_root, path))
        .collect::<anyhow::Result<Vec<_>>>()?;
    relative.sort();
    relative.dedup();
    index_pending::persist(
        &index_pending::pending_dir(&target_ctx.project_id)?,
        &relative,
    )?;
    Ok(relative.len())
}

fn explicit_index_json_payload(
    output: &impl Serialize,
    completed_files: &[PathBuf],
    busy_files: &[PathBuf],
) -> anyhow::Result<Value> {
    let mut payload = serde_json::to_value(output)?;
    let Value::Object(fields) = &mut payload else {
        anyhow::bail!("gcode index result did not serialize as an object");
    };
    fields.insert(
        "completed_files".to_string(),
        path_list_json(completed_files),
    );
    fields.insert("busy_files".to_string(), path_list_json(busy_files));
    Ok(payload)
}

fn explicit_index_text(
    output: String,
    completed_files: &[PathBuf],
    busy_files: &[PathBuf],
) -> anyhow::Result<String> {
    Ok(format!(
        "{output}\ncompleted_files: {}\nbusy_files: {}",
        serde_json::to_string(&path_list_json(completed_files))?,
        serde_json::to_string(&path_list_json(busy_files))?,
    ))
}

fn print_all_files_busy(paths: &[PathBuf], format: Format) -> anyhow::Result<()> {
    let payload = serde_json::json!({
        "status": "busy",
        "completed_files": [],
        "busy_files": path_list_json(paths),
    });
    match format {
        Format::Json => output::print_json(&payload),
        Format::Text => output::print_text(&format!(
            "All {} requested file(s) are busy\ncompleted_files: []\nbusy_files: {}",
            paths.len(),
            serde_json::to_string(&path_list_json(paths))?,
        )),
    }
}

fn path_list_json(paths: &[PathBuf]) -> Value {
    Value::Array(
        paths
            .iter()
            .map(|path| Value::String(path.to_string_lossy().into_owned()))
            .collect(),
    )
}

pub(crate) fn run_index_locked(
    ctx: &Context,
    request: IndexRequest,
) -> anyhow::Result<RunIndexLockedOutput> {
    let sync_projections = request.sync_projections;
    // An explicit-file run repairs only its own files; any other run repairs the whole
    // collection. Both reset versions that lost symbol points for the pending-sync worker.
    let repair_scope = (!request.explicit_files.is_empty()).then(|| {
        crate::index::indexer::explicit_file_storage_paths(
            &request.project_root,
            &request.explicit_files,
        )
    });
    let mut index_progress = StderrIndexProgress::new(ctx.quiet);
    let mut outcome = api::index_files(
        request,
        ctx,
        api::IndexOptions::with_progress(&mut index_progress),
    )?;
    if !sync_projections {
        return Ok(RunIndexLockedOutput::IndexOnly(outcome));
    }

    let mut projection_progress = StderrProjectionProgress::new(ctx.quiet);
    // Bound the projection phase so a wedged FalkorDB/Qdrant/embedding backend
    // cannot pin the project lock. Failures fold into degraded reports.
    let projections = sync::sync_after_index_bounded(
        ctx,
        &outcome.graph_file_paths,
        &outcome.vector_file_paths,
        sync::DEFAULT_PROJECTION_SYNC_STALL_TIMEOUT,
        &mut projection_progress,
    );
    outcome
        .degraded
        .extend(vector_reconcile::repair_after_index(
            ctx,
            repair_scope.as_deref(),
        ));
    // Refresh the stored hotspot snapshot once the graph is in sync. A failed refresh
    // is a visible degradation; the index itself still succeeds.
    outcome
        .degraded
        .extend(hotspot_snapshot::refresh_after_index(ctx));
    Ok(RunIndexLockedOutput::Projections(sync_projections_payload(
        &outcome,
        projections,
    )))
}

struct StderrProgress {
    quiet: bool,
    bar: Option<ProgressBar>,
}

impl StderrProgress {
    fn new(quiet: bool) -> Self {
        Self { quiet, bar: None }
    }

    fn start(&mut self, label: impl AsRef<str>, total: usize) {
        let mut bar = ProgressBar::new(total, self.quiet);
        bar.draw(label);
        self.bar = Some(bar);
    }

    fn advance(&mut self, item: impl AsRef<str>) {
        if let Some(bar) = self.bar.as_mut() {
            bar.tick(item);
        }
    }

    fn finish(&mut self) {
        if let Some(bar) = self.bar.as_mut() {
            bar.finish();
        }
        self.bar = None;
    }
}

struct StderrIndexProgress {
    progress: StderrProgress,
}

impl StderrIndexProgress {
    fn new(quiet: bool) -> Self {
        Self {
            progress: StderrProgress::new(quiet),
        }
    }
}

impl IndexProgressSink for StderrIndexProgress {
    fn start(&mut self, total: usize) {
        self.progress.start("indexing", total);
    }

    fn advance(&mut self, file_path: &str) {
        self.progress.advance(file_path);
    }

    fn finish(&mut self) {
        self.progress.finish();
    }
}

struct StderrProjectionProgress {
    progress: StderrProgress,
}

impl StderrProjectionProgress {
    fn new(quiet: bool) -> Self {
        Self {
            progress: StderrProgress::new(quiet),
        }
    }
}

impl ProjectionProgressSink for StderrProjectionProgress {
    fn start(&mut self, target: ProjectionTarget, total: usize) {
        self.progress
            .start(format!("{} sync", projection_label(target)), total);
    }

    fn advance(&mut self, target: ProjectionTarget, file_path: &str) {
        self.progress
            .advance(format!("{} {file_path}", projection_label(target)));
    }

    fn finish(&mut self, _target: ProjectionTarget) {
        self.progress.finish();
    }
}

fn projection_label(target: ProjectionTarget) -> &'static str {
    match target {
        ProjectionTarget::Graph => "graph",
        ProjectionTarget::Vectors => "vectors",
    }
}

fn index_text(outcome: &IndexOutcome) -> String {
    let mut text = format!(
        "Indexed {} files ({} skipped), {} symbols, {} chunks in {}ms",
        outcome.indexed_files,
        outcome.skipped_files,
        outcome.symbols_indexed,
        outcome.chunks_indexed,
        outcome.durations.total_ms
    );

    if !outcome.unsupported_file_types.is_empty() {
        text.push_str("\nUnsupported file types indexed as text only (no AST symbols):");
        for file_type in &outcome.unsupported_file_types {
            text.push_str(&format!(
                "\n  {}: {} {}",
                file_type.extension,
                file_type.files,
                pluralize(file_type.files, "file")
            ));
            if !file_type.examples.is_empty() {
                text.push_str(&format!(
                    " ({}: {})",
                    pluralize(file_type.examples.len(), "example"),
                    file_type.examples.join(", ")
                ));
            }
        }
    }

    text
}

/// Pluralizes only the status nouns emitted by this command; unknown nouns are
/// returned unchanged so callers opt in deliberately.
fn pluralize(count: usize, singular: &str) -> &str {
    match (count, singular) {
        (1, "file") => "file",
        (_, "file") => "files",
        (1, "example") => "example",
        (_, "example") => "examples",
        _ => singular,
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub(crate) struct IndexSyncProjectionsOutput {
    pub indexed_files: usize,
    pub skipped_files: usize,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub unsupported_file_types: Vec<UnsupportedFileType>,
    pub symbols_indexed: usize,
    pub chunks_indexed: usize,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub degraded: Vec<IndexDegradation>,
    pub projections: ProjectionSyncReports,
}

pub(crate) fn sync_projections_payload(
    outcome: &IndexOutcome,
    projections: ProjectionSyncReports,
) -> IndexSyncProjectionsOutput {
    IndexSyncProjectionsOutput {
        indexed_files: outcome.indexed_files,
        skipped_files: outcome.skipped_files,
        unsupported_file_types: outcome.unsupported_file_types.clone(),
        symbols_indexed: outcome.symbols_indexed,
        chunks_indexed: outcome.chunks_indexed,
        degraded: outcome.degraded.clone(),
        projections,
    }
}

pub(crate) fn sync_projections_text(
    payload: &IndexSyncProjectionsOutput,
) -> anyhow::Result<String> {
    Ok(serde_json::to_string(payload)?)
}

fn resolve_index_context(
    ctx: &Context,
    path: Option<&str>,
) -> anyhow::Result<(Context, Option<std::path::PathBuf>)> {
    let Some(p) = path else {
        return Ok((
            ctx.rebased(
                ctx.project_root.clone(),
                ctx.project_id.clone(),
                ctx.index_scope.clone(),
            ),
            None,
        ));
    };

    // Resolve root and project_id. If the path belongs to a different project
    // than the CWD-derived context, re-resolve identity for that project.
    let target = std::path::PathBuf::from(p);
    let target_root = crate::config::detect_project_root_from(&target)?;
    let target_filter = path_filter_for(&target_root, &target);
    if target_root != ctx.project_root {
        let identity = crate::config::resolve_project_identity(&target_root)?;
        ctx.validate_project_scope(&identity.project_id, &target_root)?;
        crate::config::warn_project_identity(&identity, ctx.quiet);
        if !ctx.quiet {
            eprintln!(
                "Warning: path '{}' belongs to project {} (not {}), re-resolving context",
                p,
                short_id(&identity.project_id),
                short_id(&ctx.project_id)
            );
        }
        Ok((
            ctx.rebased(target_root, identity.project_id, identity.index_scope),
            target_filter,
        ))
    } else {
        Ok((
            ctx.rebased(target_root, ctx.project_id.clone(), ctx.index_scope.clone()),
            target_filter,
        ))
    }
}

fn path_filter_for(
    project_root: &std::path::Path,
    target: &std::path::Path,
) -> Option<std::path::PathBuf> {
    let target_abs = if target.is_absolute() {
        target.to_path_buf()
    } else {
        std::env::current_dir()
            .map(|cwd| cwd.join(target))
            .unwrap_or_else(|_| project_root.join(target))
    };

    let root_abs = project_root
        .canonicalize()
        .unwrap_or_else(|_| project_root.to_path_buf());
    let target_abs = target_abs.canonicalize().unwrap_or(target_abs);

    if target_abs == root_abs {
        None
    } else {
        Some(target_abs)
    }
}

#[cfg(test)]
#[path = "index/explicit_run_tests.rs"]
mod explicit_run_tests;

#[cfg(test)]
#[path = "index/lock_policy_tests.rs"]
mod lock_policy_tests;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::index::api::{IndexDurations, IndexOutcome};
    use crate::projection::sync::{
        ProjectionStatus, ProjectionSyncError, ProjectionSyncReport, ProjectionSyncReports,
    };
    use serde_json::Value;

    #[test]
    fn pluralize_handles_index_status_nouns() {
        assert_eq!(pluralize(1, "file"), "file");
        assert_eq!(pluralize(2, "file"), "files");
        assert_eq!(pluralize(1, "example"), "example");
        assert_eq!(pluralize(0, "example"), "examples");
    }

    #[test]
    fn pluralize_leaves_unknown_nouns_unchanged() {
        assert_eq!(pluralize(2, "symbol"), "symbol");
    }

    fn sample_outcome() -> IndexOutcome {
        IndexOutcome {
            indexed_files: 12,
            skipped_files: 0,
            symbols_indexed: 348,
            chunks_indexed: 921,
            ..IndexOutcome::default()
        }
    }

    fn sample_reports() -> ProjectionSyncReports {
        ProjectionSyncReports {
            graph: ProjectionSyncReport {
                status: ProjectionStatus::Ok,
                synced_files: 12,
                synced_symbols: 348,
                skipped_files: 1,
                failed_files: 0,
                degraded: false,
                error: None,
            },
            vector: ProjectionSyncReport {
                status: ProjectionStatus::Degraded,
                synced_files: 0,
                synced_symbols: 0,
                skipped_files: 0,
                failed_files: 0,
                degraded: true,
                error: Some(ProjectionSyncError {
                    kind: "missing_qdrant_config".to_string(),
                    message: "Qdrant config is required".to_string(),
                }),
            },
        }
    }

    #[test]
    fn sync_projections_json_contract() {
        let payload = sync_projections_payload(&sample_outcome(), sample_reports());

        insta::assert_json_snapshot!("sync_projections_payload", payload);
    }

    #[test]
    fn sync_projections_text_contract() {
        let payload = sync_projections_payload(&sample_outcome(), sample_reports());
        let text = sync_projections_text(&payload).expect("text payload");

        insta::assert_snapshot!("sync_projections_text", text);
    }

    #[test]
    fn stderr_index_progress_lifecycle_clears_bar_on_finish() {
        let mut progress = StderrIndexProgress::new(true);

        progress.start(2);
        progress.advance("src/lib.rs");
        assert!(progress.progress.bar.is_some());

        progress.finish();

        assert!(progress.progress.bar.is_none());
    }

    #[test]
    fn stderr_projection_progress_lifecycle_clears_bar_on_finish() {
        let mut progress = StderrProjectionProgress::new(true);

        progress.start(ProjectionTarget::Graph, 2);
        progress.advance(ProjectionTarget::Graph, "src/lib.rs");
        assert!(progress.progress.bar.is_some());

        progress.finish(ProjectionTarget::Graph);

        assert!(progress.progress.bar.is_none());
    }

    #[test]
    fn index_outcome_json_contract_redacts_durations() {
        let mut outcome = sample_outcome();
        outcome.project_id = "project-1".to_string();
        outcome.scanned_files = 14;
        outcome.imports_indexed = 41;
        outcome.calls_indexed = 73;
        outcome.unresolved_targets_indexed = 5;
        outcome.indexed_file_paths = vec!["src/main.rs".to_string(), "src/lib.rs".to_string()];
        outcome.durations = IndexDurations {
            discovery_ms: 11,
            indexing_ms: 22,
            stats_ms: 33,
            total_ms: 66,
        };
        let mut redacted = serde_json::to_value(outcome).expect("outcome serializes");
        let Value::Object(durations) = &mut redacted["durations"] else {
            panic!("durations serialize as object");
        };
        for field in ["discovery_ms", "indexing_ms", "stats_ms", "total_ms"] {
            durations.insert(
                field.to_string(),
                Value::String("[duration-ms]".to_string()),
            );
        }

        insta::assert_json_snapshot!("index_outcome", redacted);
    }

    #[test]
    fn explicit_index_outputs_report_completed_and_busy_files() -> anyhow::Result<()> {
        let completed = [std::path::PathBuf::from("src/completed.rs")];
        let busy = [std::path::PathBuf::from("src/busy.rs")];
        let outcome = sample_outcome();

        let payload = explicit_index_json_payload(&outcome, &completed, &busy)?;
        assert_eq!(
            payload["completed_files"],
            serde_json::json!(["src/completed.rs"])
        );
        assert_eq!(payload["busy_files"], serde_json::json!(["src/busy.rs"]));
        assert_eq!(payload["indexed_files"], 12);

        let text = explicit_index_text(index_text(&outcome), &completed, &busy)?;
        assert!(text.contains("completed_files: [\"src/completed.rs\"]"));
        assert!(text.contains("busy_files: [\"src/busy.rs\"]"));
        Ok(())
    }

    #[test]
    fn index_promotion_projects_owner_on_graph_only() {
        let source = include_str!("index.rs");
        assert!(
            source.contains("&outcome.graph_file_paths"),
            "gcode index must graph-sync promoted owners"
        );
        assert!(
            source.contains("&outcome.vector_file_paths"),
            "gcode index must vector-sync only this run's indexed files"
        );
        let sync_call = source
            .split("let projections = sync::sync_after_index_bounded(")
            .nth(1)
            .and_then(|rest| rest.split(");").next())
            .expect("sync_after_index_bounded call");
        assert!(
            !sync_call.contains("indexed_file_paths"),
            "gcode index must not pass indexed_file_paths into dual-backend sync"
        );
    }

    #[test]
    fn index_text_reports_unsupported_file_types() {
        let mut outcome = sample_outcome();
        outcome.unsupported_file_types = vec![
            UnsupportedFileType {
                extension: ".md".to_string(),
                files: 1,
                examples: vec!["README.md".to_string()],
            },
            UnsupportedFileType {
                extension: ".txt".to_string(),
                files: 2,
                examples: vec!["notes.txt".to_string(), "docs/tasks.txt".to_string()],
            },
            UnsupportedFileType {
                extension: "extensionless".to_string(),
                files: 1,
                examples: vec!["Dockerfile".to_string()],
            },
        ];

        let text = index_text(&outcome);

        insta::assert_snapshot!("index_text_unsupported_file_types", text);
    }
}
