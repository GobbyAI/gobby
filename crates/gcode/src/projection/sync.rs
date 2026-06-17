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
) -> anyhow::Result<ProjectionSyncReports> {
    Ok(ProjectionSyncReports {
        graph: sync_graph_files(ctx, file_paths)?,
        vector: sync_vector_files(ctx, file_paths)?,
    })
}

pub(crate) fn sync_files_with_state<S>(
    ctx: &Context,
    file_paths: &[String],
    state: &mut S,
    mut sync_one: impl FnMut(&mut S, &str) -> anyhow::Result<ProjectionFileSyncOutcome>,
) -> ProjectionSyncReport {
    let mut synced_files = 0usize;
    let mut synced_symbols = 0usize;
    let mut skipped_files = 0usize;
    let mut failed_files = 0usize;
    let mut errors = Vec::new();
    let mut error_kind = None;

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

fn sync_graph_files(ctx: &Context, file_paths: &[String]) -> anyhow::Result<ProjectionSyncReport> {
    if file_paths.is_empty() {
        return Ok(ProjectionSyncReport::ok(0, 0));
    }
    if let Err(error) = code_graph::require_graph_reads(ctx) {
        return Ok(ProjectionSyncReport::degraded_from_error(&error, 0, 0));
    }

    let mut conn = db::connect_readwrite(&ctx.database_url)?;
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

fn sync_vector_files(ctx: &Context, file_paths: &[String]) -> anyhow::Result<ProjectionSyncReport> {
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
    ))
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
        if !db::indexed_file_exists(&mut self.conn, &self.ctx.project_id, file_path)? {
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

        let report =
            sync_files_with_state(&test_context(), &files, &mut state, |state, file_path| {
                state.synced.push(file_path.to_string());
                if file_path == "src/fail.rs" {
                    anyhow::bail!("projection write failed");
                }
                Ok(ProjectionFileSyncOutcome::Synced { symbols: 3 })
            });

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

        let report =
            sync_files_with_state(&test_context(), &files, &mut state, |state, file_path| {
                state.synced.push(file_path.to_string());
                if file_path == "src/missing.rs" {
                    return Ok(ProjectionFileSyncOutcome::SkippedMissingIndexedFile);
                }
                Ok(ProjectionFileSyncOutcome::Synced { symbols: 2 })
            });

        assert_eq!(state.synced, vec!["src/missing.rs", "src/ok.rs"]);
        assert_eq!(report.status, ProjectionStatus::Ok);
        assert_eq!(report.synced_files, 1);
        assert_eq!(report.synced_symbols, 2);
        assert_eq!(report.skipped_files, 1);
        assert_eq!(report.failed_files, 0);
        assert!(!report.degraded);
        assert!(report.error.is_none());
    }
}
