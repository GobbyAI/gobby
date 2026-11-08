use crate::config::{CODE_SYMBOL_COLLECTION_PREFIX, Context};
use crate::db;
use crate::index_lock::{self, IndexLockPolicy, ProjectFileLockSet};
use crate::output::{self, Format};
use crate::projection::{
    self,
    sync::{ProjectionStatus, ProjectionSyncError, ProjectionSyncReport, pinned},
};
use crate::vector::code_symbols::{
    self, CodeSymbolVectorLifecycle, CodeSymbolVectorLifecycleAction,
    CodeSymbolVectorLifecycleOutput, EmbeddingSource, RebuiltSymbol, VectorLifecycleError,
    embedding_source_from_context,
};
use gobby_core::progress::ProgressBar;
use serde::Serialize;
use serde_json::Value;
use std::collections::HashMap;
use std::io::Write as _;
use std::path::PathBuf;

pub(crate) mod reconcile;

pub(crate) fn lifecycle_from_context(
    ctx: &Context,
) -> Result<CodeSymbolVectorLifecycle, VectorLifecycleError> {
    lifecycle_from_resolved_embedding_source(ctx, embedding_source_from_context(ctx))
}

fn lifecycle_from_resolved_embedding_source(
    ctx: &Context,
    embedding: Option<EmbeddingSource>,
) -> Result<CodeSymbolVectorLifecycle, VectorLifecycleError> {
    let qdrant = ctx
        .qdrant
        .clone()
        .ok_or(VectorLifecycleError::MissingQdrantConfig)?;
    let embedding = embedding.ok_or(VectorLifecycleError::MissingEmbeddingConfig)?;
    CodeSymbolVectorLifecycle::new(
        ctx.project_id.clone(),
        qdrant,
        embedding,
        ctx.code_vectors.clone(),
    )
    .map(|lifecycle| lifecycle.with_project_root(ctx.project_root.clone()))
}

pub fn sync_file(
    ctx: &Context,
    file_path: &str,
    allow_missing_indexed_file: bool,
    format: Format,
) -> anyhow::Result<()> {
    let locks = index_lock::lock_project_files(
        ctx,
        &[PathBuf::from(file_path)],
        IndexLockPolicy::brief_index_flush_try(),
    )?;
    if locks.acquired_files.is_empty() {
        print_file_locks_busy(&locks, format)?;
        std::io::stdout().flush()?;
        std::process::exit(3);
    }
    let file_path = locks.acquired_files[0].to_string_lossy().into_owned();

    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    let Some(attempt) = db::mark_vector_sync_attempted(&mut conn, &ctx.project_id, &file_path)?
    else {
        if let Some(pin) = pinned::begin_vector_attempt(&mut conn, &ctx.project_id, &file_path)? {
            return sync_pinned_vector_file(ctx, &mut conn, &file_path, &pin, format, &locks);
        }
        if allow_missing_indexed_file {
            return print_skipped_missing_indexed_file(ctx, &file_path, format, &locks);
        }
        anyhow::bail!(
            "indexed file `{file_path}` was not found for project {}",
            ctx.project_id
        );
    };
    let mut lifecycle = lifecycle_from_context(ctx)?;
    let symbols = code_symbols::fetch_symbols_for_file(&mut conn, &ctx.project_id, &file_path)?;
    let output = match lifecycle.sync_file_symbols(&file_path, &symbols) {
        Ok(output) => output,
        Err(VectorLifecycleError::IndexedSourceChanged { .. }) => {
            return print_skipped_sync_file(
                ctx,
                &file_path,
                format,
                &locks,
                SyncFileSkip::SourceChanged,
                None,
            );
        }
        Err(err) => return Err(err.into()),
    };
    code_symbols::prune_stale_file_points(
        &lifecycle,
        &mut conn,
        &ctx.project_id,
        &file_path,
        &symbols,
    )?;
    if !db::mark_vectors_synced(
        &mut conn,
        &ctx.project_id,
        &file_path,
        &attempt.content_hash,
        attempt.attempted_at,
    )? {
        return print_skipped_sync_file(
            ctx,
            &file_path,
            format,
            &locks,
            SyncFileSkip::Superseded,
            None,
        );
    }
    let report = ProjectionSyncReport::ok(1, output.symbols);
    print_file_lifecycle_output(&output, report, format, &locks)
}

fn sync_pinned_vector_file(
    ctx: &Context,
    conn: &mut postgres::Client,
    file_path: &str,
    pin: &pinned::PinAttempt,
    format: Format,
    locks: &ProjectFileLockSet,
) -> anyhow::Result<()> {
    let mut lifecycle = lifecycle_from_context(ctx)?;
    let output =
        match pinned::sync_vector_attempt(conn, &ctx.project_id, &mut lifecycle, file_path, pin)? {
            pinned::PinnedVectorSync::Synced(output) => output,
            pinned::PinnedVectorSync::Superseded => {
                return print_skipped_sync_file(
                    ctx,
                    file_path,
                    format,
                    locks,
                    SyncFileSkip::Superseded,
                    None,
                );
            }
            pinned::PinnedVectorSync::SourceChanged => {
                return print_skipped_sync_file(
                    ctx,
                    file_path,
                    format,
                    locks,
                    SyncFileSkip::SourceChanged,
                    None,
                );
            }
            pinned::PinnedVectorSync::SourceMissing => {
                return print_skipped_sync_file(
                    ctx,
                    file_path,
                    format,
                    locks,
                    SyncFileSkip::SourceMissing,
                    None,
                );
            }
        };
    let report = ProjectionSyncReport::ok(1, output.symbols);
    print_file_lifecycle_output(&output, report, format, locks)
}

enum SyncFileSkip {
    MissingIndexedFile,
    Superseded,
    SourceChanged,
    SourceMissing,
}

fn print_skipped_missing_indexed_file(
    ctx: &Context,
    file_path: &str,
    format: Format,
    locks: &ProjectFileLockSet,
) -> anyhow::Result<()> {
    let failures = projection::reconcile_deleted_file(ctx, file_path);
    let error = if failures.is_empty() {
        None
    } else {
        Some(ProjectionSyncError {
            kind: "projection_reconcile_failed".to_string(),
            message: failures
                .iter()
                .map(|failure| {
                    format!(
                        "failed to reconcile {:?} projection: {}",
                        failure.target, failure.message
                    )
                })
                .collect::<Vec<_>>()
                .join("; "),
        })
    };
    print_skipped_sync_file(
        ctx,
        file_path,
        format,
        locks,
        SyncFileSkip::MissingIndexedFile,
        error,
    )
}

/// Returns the wire reason, the summary, and the text line for a skipped sync.
fn skip_descriptor(skip: SyncFileSkip, file_path: &str) -> (&'static str, &'static str, String) {
    match skip {
        SyncFileSkip::MissingIndexedFile => (
            "indexed_file_not_found",
            "indexed file not found",
            format!("indexed file `{file_path}` was not found"),
        ),
        SyncFileSkip::Superseded => (
            "sync_superseded",
            "superseded by a newer index",
            format!("`{file_path}` was superseded by a newer index"),
        ),
        SyncFileSkip::SourceChanged => (
            "source_changed",
            "source changed after indexing",
            format!("`{file_path}` changed after indexing; its newer index re-syncs it"),
        ),
        SyncFileSkip::SourceMissing => (
            "source_missing",
            "source missing from checkout",
            format!("`{file_path}` is missing from the checkout; its pin was retired"),
        ),
    }
}

fn print_skipped_sync_file(
    ctx: &Context,
    file_path: &str,
    format: Format,
    locks: &ProjectFileLockSet,
    skip: SyncFileSkip,
    error: Option<ProjectionSyncError>,
) -> anyhow::Result<()> {
    let collection = code_symbols::collection_name(CODE_SYMBOL_COLLECTION_PREFIX, &ctx.project_id)?;
    let (reason, summary, text) = skip_descriptor(skip, file_path);
    let degraded = error.is_some();
    let payload = file_lock_payload(
        serde_json::json!({
            "success": true,
            "project_id": ctx.project_id,
            "projection": "vector",
            "action": "sync_file",
            "file_path": file_path,
            "collection": collection,
            "status": "skipped",
            "reason": reason,
            "synced_files": 0,
            "synced_symbols": 0,
            "skipped_files": 1,
            "failed_files": 0,
            "degraded": degraded,
            "error": error,
            "summary": format!("skipped vector sync for {file_path}: {summary}"),
        }),
        locks,
    );
    match format {
        Format::Json => output::print_json(&payload),
        Format::Text => {
            output::print_text(&format!(
                "Skipped code-symbol vector sync for project {}: {text}",
                ctx.project_id
            ))?;
            output::print_json(&payload)
        }
    }
}

pub fn clear(ctx: &Context, drop_collection: bool, format: Format) -> anyhow::Result<()> {
    index_lock::with_exclusive_project_lock(ctx, || {
        let mut lifecycle = lifecycle_from_context(ctx)?;
        let mut conn = db::connect_readwrite(&ctx.database_url)?;
        db::reset_vectors_sync_for_project(&mut conn, &ctx.project_id)?;
        let output = if drop_collection {
            lifecycle.drop_project_collection()?
        } else {
            lifecycle.clear_project_vectors()?
        };
        let report = ProjectionSyncReport::ok(0, 0);
        print_lifecycle_output(&output, report, format)
    })
}

/// Rebuild the project's code-symbol vectors.
///
/// Each file is marked synced once all its symbols are upserted, so a rerun
/// after an interruption embeds only the files still unsynced. A file with a
/// skipped symbol stays unsynced, so the next rebuild retries it. A full run
/// happens with `full`, when every file is already synced, or when the
/// collection is missing (its synced flags then describe nothing).
pub fn rebuild(ctx: &Context, full: bool, format: Format) -> anyhow::Result<()> {
    index_lock::with_exclusive_project_lock(ctx, || {
        let mut lifecycle = lifecycle_from_context(ctx)?;
        let mut conn = db::connect_readwrite(&ctx.database_url)?;
        let file_paths = db::list_indexed_file_paths(&mut conn, &ctx.project_id, &ctx.machine_id)?;
        let mut from_scratch = full || !lifecycle.collection_exists()?;
        if from_scratch {
            db::reset_vectors_sync_for_project(&mut conn, &ctx.project_id)?;
        }
        let mut attempts = db::mark_unsynced_vector_sync_attempted(&mut conn, &ctx.project_id)?;
        if attempts.is_empty() && !from_scratch {
            from_scratch = true;
            db::reset_vectors_sync_for_project(&mut conn, &ctx.project_id)?;
            attempts = db::mark_unsynced_vector_sync_attempted(&mut conn, &ctx.project_id)?;
        }
        let already_synced = file_paths.len().saturating_sub(attempts.len());
        let symbols: Vec<_> = code_symbols::fetch_symbols_for_project(&mut conn, &ctx.project_id)?
            .into_iter()
            .filter(|symbol| attempts.contains_key(&symbol.file_path))
            .collect();
        let mut sync = RebuildSync::new(
            attempts,
            symbols.iter().map(|symbol| symbol.file_path.as_str()),
            already_synced,
        );
        let mut bar = ProgressBar::new(symbols.len(), ctx.quiet);
        bar.draw("embedding");
        let output = lifecycle.rebuild_symbols(&symbols, &mut |symbol, outcome| {
            bar.tick(&symbol.file_path);
            sync.symbol_done(&mut conn, &ctx.project_id, &symbol.file_path, outcome);
        });
        bar.finish();
        let mut output = output?;
        sync.finish(&mut conn, &ctx.project_id);
        let files_synced = sync.files_synced;
        if !from_scratch && already_synced > 0 {
            output.summary.push_str(&format!(
                ", resumed with {already_synced} file(s) already synced (--full re-embeds all)"
            ));
        }
        let report = ProjectionSyncReport::ok(files_synced.min(file_paths.len()), output.symbols);
        print_lifecycle_output(&output, report, format)
    })
}

/// A rebuild's per-file sync bookkeeping: each attempted file is marked synced
/// once its last symbol is upserted, and never when one of its symbols was
/// skipped.
pub(crate) struct RebuildSync {
    attempts: HashMap<String, db::VectorSyncAttempt>,
    remaining: HashMap<String, usize>,
    /// Files synced so far, counting those already synced before this run.
    pub(crate) files_synced: usize,
}

impl RebuildSync {
    /// `symbol_files` holds one file path per symbol the rebuild will embed.
    pub(crate) fn new<'a>(
        attempts: HashMap<String, db::VectorSyncAttempt>,
        symbol_files: impl IntoIterator<Item = &'a str>,
        already_synced: usize,
    ) -> Self {
        let mut remaining: HashMap<String, usize> = HashMap::new();
        for file_path in symbol_files {
            *remaining.entry(file_path.to_string()).or_default() += 1;
        }
        Self {
            attempts,
            remaining,
            files_synced: already_synced,
        }
    }

    /// Record one finished symbol, marking its file synced when it completes.
    pub(crate) fn symbol_done(
        &mut self,
        conn: &mut postgres::Client,
        project_id: &str,
        file_path: &str,
        outcome: RebuiltSymbol,
    ) {
        if record_symbol_done(&mut self.remaining, file_path, outcome)
            && mark_file_synced(conn, project_id, &self.attempts, file_path)
        {
            self.files_synced += 1;
        }
    }

    /// After a successful run, mark the attempted files that had no symbols:
    /// they have nothing to embed.
    pub(crate) fn finish(&mut self, conn: &mut postgres::Client, project_id: &str) {
        for file_path in self.attempts.keys() {
            if !self.remaining.contains_key(file_path)
                && mark_file_synced(conn, project_id, &self.attempts, file_path)
            {
                self.files_synced += 1;
            }
        }
    }
}

/// Mark `file_path` synced against this run's attempt token; true when the
/// mark landed. A lost or failed mark only costs resume progress: the file
/// is embedded again by the next rebuild.
fn mark_file_synced(
    conn: &mut postgres::Client,
    project_id: &str,
    attempts: &HashMap<String, db::VectorSyncAttempt>,
    file_path: &str,
) -> bool {
    let Some(attempt) = attempts.get(file_path) else {
        return false;
    };
    db::mark_vectors_synced(
        conn,
        project_id,
        file_path,
        &attempt.content_hash,
        attempt.attempted_at,
    )
    .unwrap_or_else(|error| {
        log::warn!("could not record vector sync progress for {file_path}: {error}");
        false
    })
}

/// Record one finished symbol of `file_path`; true once its last symbol is
/// upserted. A skipped symbol leaves its file unsynced: the count drops to
/// zero without completing, so no later symbol can finish the file.
fn record_symbol_done(
    remaining: &mut HashMap<String, usize>,
    file_path: &str,
    outcome: RebuiltSymbol,
) -> bool {
    let Some(left) = remaining.get_mut(file_path) else {
        return false;
    };
    match outcome {
        RebuiltSymbol::Skipped => {
            *left = 0;
            false
        }
        RebuiltSymbol::Upserted if *left > 0 => {
            *left -= 1;
            *left == 0
        }
        RebuiltSymbol::Upserted => false,
    }
}

fn print_lifecycle_output(
    output: &CodeSymbolVectorLifecycleOutput,
    report: ProjectionSyncReport,
    format: Format,
) -> anyhow::Result<()> {
    let payload = lifecycle_json_payload(output, report);
    match format {
        Format::Json => output::print_json(&payload),
        Format::Text => output::print_text(&serde_json::to_string(&payload)?),
    }
}

fn print_file_lifecycle_output(
    output: &CodeSymbolVectorLifecycleOutput,
    report: ProjectionSyncReport,
    format: Format,
    locks: &ProjectFileLockSet,
) -> anyhow::Result<()> {
    let payload = file_lock_payload(
        serde_json::to_value(lifecycle_json_payload(output, report))?,
        locks,
    );
    match format {
        Format::Json => output::print_json(&payload),
        Format::Text => output::print_text(&serde_json::to_string(&payload)?),
    }
}

fn print_file_locks_busy(locks: &ProjectFileLockSet, format: Format) -> anyhow::Result<()> {
    let payload = file_lock_payload(
        serde_json::json!({
            "success": false,
            "status": "busy",
            "projection": "vector",
        }),
        locks,
    );
    match format {
        Format::Json => output::print_json(&payload),
        Format::Text => output::print_text(&format!(
            "Vector file lock busy\ncompleted_files: []\nbusy_files: {}",
            serde_json::to_string(&payload["busy_files"])?
        )),
    }
}

fn file_lock_payload(mut payload: Value, locks: &ProjectFileLockSet) -> Value {
    let Value::Object(fields) = &mut payload else {
        return payload;
    };
    fields.insert(
        "completed_files".to_string(),
        serde_json::json!(
            locks
                .acquired_files
                .iter()
                .map(|path| path.to_string_lossy().into_owned())
                .collect::<Vec<_>>()
        ),
    );
    fields.insert(
        "busy_files".to_string(),
        serde_json::json!(
            locks
                .busy_files
                .iter()
                .map(|path| path.to_string_lossy().into_owned())
                .collect::<Vec<_>>()
        ),
    );
    payload
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub(crate) struct VectorLifecycleJsonPayload {
    pub project_id: String,
    pub projection: &'static str,
    pub action: CodeSymbolVectorLifecycleAction,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub file_path: Option<String>,
    pub collection: String,
    pub status: ProjectionStatus,
    pub synced_files: usize,
    pub synced_symbols: usize,
    pub skipped_files: usize,
    pub failed_files: usize,
    pub degraded: bool,
    pub error: Option<crate::projection::sync::ProjectionSyncError>,
    pub symbols: usize,
    pub vectors_upserted: usize,
    pub delete_operations_issued: usize,
    pub summary: String,
}

pub(crate) fn lifecycle_json_payload(
    output: &CodeSymbolVectorLifecycleOutput,
    report: ProjectionSyncReport,
) -> VectorLifecycleJsonPayload {
    VectorLifecycleJsonPayload {
        project_id: output.project_id.clone(),
        projection: "vector",
        action: output.action,
        file_path: output.file_path.clone(),
        collection: output.collection.clone(),
        status: report.status,
        synced_files: report.synced_files,
        synced_symbols: report.synced_symbols,
        skipped_files: report.skipped_files,
        failed_files: report.failed_files,
        degraded: report.degraded,
        error: report.error,
        symbols: output.symbols,
        vectors_upserted: output.vectors_upserted,
        delete_operations_issued: output.delete_operations_issued,
        summary: output.summary.clone(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::projection::sync::{ProjectionStatus, ProjectionSyncError, ProjectionSyncReport};
    use serde_json::json;
    use std::path::PathBuf;

    fn make_ctx() -> Context {
        Context {
            database_url: "postgresql://localhost/nonexistent".to_string(),
            project_root: PathBuf::from("/nonexistent"),
            project_id: "project-1".to_string(),
            quiet: true,
            falkordb: None,
            qdrant: None,
            embedding: None,
            code_vectors: crate::config::CodeVectorSettings::default(),
            machine_id: gobby_core::machine::read_local_machine_id().expect("read machine id"),
            runtime_config_capture_degraded: false,
            indexing: gobby_core::config::IndexingConfig::default(),
            daemon_url: None,
            grant_ai: None,
            index_scope: crate::config::ProjectIndexScope::Single,
        }
    }

    fn qdrant_config() -> crate::config::QdrantConfig {
        crate::config::QdrantConfig {
            url: Some("http://localhost:6333".to_string()),
            api_key: None,
        }
    }

    fn daemon_embedding_source() -> EmbeddingSource {
        use gobby_core::ai_context::{
            AiConfigSource, AiContext, AiContextOptions, NoPrimaryAiConfigSource,
        };
        use gobby_core::config::AiRouting;

        let mut source = AiConfigSource::with_primary(NoPrimaryAiConfigSource);
        let context = AiContext::resolve_with_options(
            Some("project-1".to_string()),
            &mut source,
            AiContextOptions {
                no_ai: false,
                forced_routing: Some(AiRouting::Daemon),
            },
        );
        EmbeddingSource::Daemon(Box::new(context))
    }

    #[test]
    fn vector_lifecycle_requires_config() {
        let err = lifecycle_from_context(&make_ctx()).expect_err("missing config must fail");
        assert!(matches!(
            err,
            code_symbols::VectorLifecycleError::MissingQdrantConfig
        ));

        let ctx = Context {
            qdrant: Some(qdrant_config()),
            ..make_ctx()
        };
        let err = lifecycle_from_resolved_embedding_source(&ctx, None)
            .expect_err("missing embedding must fail");
        assert!(matches!(
            err,
            code_symbols::VectorLifecycleError::MissingEmbeddingConfig
        ));

        lifecycle_from_resolved_embedding_source(&ctx, Some(daemon_embedding_source()))
            .expect("daemon embedding source must satisfy lifecycle config");
    }

    #[test]
    fn lifecycle_json_contract() {
        let output = CodeSymbolVectorLifecycleOutput {
            project_id: "project-1".to_string(),
            collection: "gcode_code_symbols_project-1".to_string(),
            action: CodeSymbolVectorLifecycleAction::SyncFile,
            file_path: Some("src/lib.rs".to_string()),
            symbols: 2,
            vectors_upserted: 2,
            delete_operations_issued: 1,
            summary: "2 vector(s) upserted, 1 delete operation(s) issued".to_string(),
        };

        let payload = lifecycle_json_payload(
            &output,
            ProjectionSyncReport {
                status: ProjectionStatus::Ok,
                synced_files: 1,
                synced_symbols: 2,
                skipped_files: 0,
                failed_files: 0,
                degraded: false,
                error: None,
            },
        );
        assert_eq!(
            serde_json::to_value(&payload).expect("payload serializes"),
            json!({
                "project_id": "project-1",
                "projection": "vector",
                "action": "sync_file",
                "file_path": "src/lib.rs",
                "collection": "gcode_code_symbols_project-1",
            "status": "ok",
            "synced_files": 1,
            "synced_symbols": 2,
            "skipped_files": 0,
            "failed_files": 0,
            "degraded": false,
            "error": null,
                "symbols": 2,
                "vectors_upserted": 2,
                "delete_operations_issued": 1,
                "summary": "2 vector(s) upserted, 1 delete operation(s) issued"
            })
        );

        let degraded = lifecycle_json_payload(
            &output,
            ProjectionSyncReport {
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
        );
        let degraded = serde_json::to_value(&degraded).expect("payload serializes");
        assert_eq!(degraded["status"], "degraded");
        assert_eq!(degraded["error"]["kind"], "missing_qdrant_config");
    }

    #[test]
    fn rebuild_marks_a_file_only_after_its_last_symbol_across_rounds() {
        let mut remaining = HashMap::from([("a.rs".to_string(), 2), ("b.rs".to_string(), 1)]);

        let upserted = RebuiltSymbol::Upserted;

        assert!(!record_symbol_done(&mut remaining, "a.rs", upserted));
        assert!(record_symbol_done(&mut remaining, "b.rs", upserted));
        assert!(record_symbol_done(&mut remaining, "a.rs", upserted));
        assert!(!record_symbol_done(&mut remaining, "a.rs", upserted));
        assert!(!record_symbol_done(&mut remaining, "unknown.rs", upserted));
    }

    #[test]
    fn source_changed_skip_has_its_own_reason_and_summary() {
        let (reason, summary, text) = skip_descriptor(SyncFileSkip::SourceChanged, "src/lib.rs");

        assert_eq!(reason, "source_changed");
        assert_eq!(summary, "source changed after indexing");
        assert_eq!(
            text,
            "`src/lib.rs` changed after indexing; its newer index re-syncs it"
        );
    }

    #[test]
    fn rebuild_never_marks_a_file_with_a_skipped_symbol() {
        let mut remaining = HashMap::from([("a.rs".to_string(), 2), ("b.rs".to_string(), 2)]);
        let (upserted, skipped) = (RebuiltSymbol::Upserted, RebuiltSymbol::Skipped);

        // Skip after an upsert, and skip before the last upsert.
        assert!(!record_symbol_done(&mut remaining, "a.rs", upserted));
        assert!(!record_symbol_done(&mut remaining, "a.rs", skipped));
        assert!(!record_symbol_done(&mut remaining, "b.rs", skipped));
        assert!(!record_symbol_done(&mut remaining, "b.rs", upserted));
        assert!(!record_symbol_done(&mut remaining, "b.rs", upserted));
        // A skipped file stays known, so the no-symbol pass leaves it unsynced.
        assert!(remaining.contains_key("a.rs") && remaining.contains_key("b.rs"));
    }
}
