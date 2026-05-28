use crate::config::Context;
use crate::db;
use crate::graph::code_graph::{self, GraphReadError};
use crate::vector::code_symbols::{self, CodeSymbolVectorLifecycle, VectorLifecycleError};
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
    pub degraded: bool,
    pub error: Option<ProjectionSyncError>,
}

impl ProjectionSyncReport {
    pub fn ok(synced_files: usize, synced_symbols: usize) -> Self {
        Self {
            status: ProjectionStatus::Ok,
            synced_files,
            synced_symbols,
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
        Self {
            status: ProjectionStatus::Degraded,
            synced_files,
            synced_symbols,
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
        let typed = typed_projection_error(error);
        Self {
            status: ProjectionStatus::Degraded,
            synced_files,
            synced_symbols,
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
    file_paths: &[String],
    state: &mut S,
    mut sync_one: impl FnMut(&mut S, &str) -> anyhow::Result<usize>,
    mut mark_synced: impl FnMut(&mut S, &str) -> anyhow::Result<()>,
) -> ProjectionSyncReport {
    let mut synced_files = 0usize;
    let mut synced_symbols = 0usize;

    for file_path in file_paths {
        let symbols = match sync_one(state, file_path)
            .and_then(|symbols| mark_synced(state, file_path).map(|()| symbols))
        {
            Ok(symbols) => symbols,
            Err(error) => {
                return ProjectionSyncReport::degraded_from_error(
                    &error,
                    synced_files,
                    synced_symbols,
                );
            }
        };
        synced_files += 1;
        synced_symbols += symbols;
    }

    ProjectionSyncReport::ok(synced_files, synced_symbols)
}

fn sync_graph_files(ctx: &Context, file_paths: &[String]) -> anyhow::Result<ProjectionSyncReport> {
    if file_paths.is_empty() {
        return Ok(ProjectionSyncReport::ok(0, 0));
    }
    if let Err(error) = code_graph::require_graph_reads(ctx) {
        return Ok(ProjectionSyncReport::degraded_from_error(&error, 0, 0));
    }

    let conn = db::connect_readwrite(&ctx.database_url)?;
    let mut state = GraphProjectionState { ctx, conn };
    Ok(sync_files_with_state(
        file_paths,
        &mut state,
        GraphProjectionState::sync_file,
        GraphProjectionState::mark_synced,
    ))
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
        file_paths,
        &mut state,
        VectorProjectionState::sync_file,
        VectorProjectionState::mark_synced,
    ))
}

struct GraphProjectionState<'a> {
    ctx: &'a Context,
    conn: postgres::Client,
}

impl GraphProjectionState<'_> {
    fn sync_file(&mut self, file_path: &str) -> anyhow::Result<usize> {
        let facts = db::read_graph_file_facts(&mut self.conn, &self.ctx.project_id, file_path)?;
        if !db::mark_graph_sync_attempted(&mut self.conn, &self.ctx.project_id, file_path)? {
            anyhow::bail!(
                "indexed file `{file_path}` was not found for project {}",
                self.ctx.project_id
            );
        }
        code_graph::sync_file_graph(
            self.ctx,
            &facts.file_path,
            &facts.imports,
            &facts.definitions,
            &facts.calls,
        )?;
        Ok(facts.definitions.len())
    }

    fn mark_synced(&mut self, file_path: &str) -> anyhow::Result<()> {
        if db::mark_graph_synced(&mut self.conn, &self.ctx.project_id, file_path)? {
            Ok(())
        } else {
            anyhow::bail!(
                "indexed file `{file_path}` was not found for project {}",
                self.ctx.project_id
            )
        }
    }
}

struct VectorProjectionState<'a> {
    ctx: &'a Context,
    conn: postgres::Client,
    lifecycle: CodeSymbolVectorLifecycle,
}

impl VectorProjectionState<'_> {
    fn sync_file(&mut self, file_path: &str) -> anyhow::Result<usize> {
        if !db::indexed_file_exists(&mut self.conn, &self.ctx.project_id, file_path)? {
            anyhow::bail!(
                "indexed file `{file_path}` was not found for project {}",
                self.ctx.project_id
            );
        }
        let symbols =
            code_symbols::fetch_symbols_for_file(&mut self.conn, &self.ctx.project_id, file_path)?;
        let symbol_count = symbols.len();
        self.lifecycle.sync_file_symbols(file_path, &symbols)?;
        Ok(symbol_count)
    }

    fn mark_synced(&mut self, file_path: &str) -> anyhow::Result<()> {
        if db::mark_vectors_synced(&mut self.conn, &self.ctx.project_id, file_path)? {
            Ok(())
        } else {
            anyhow::bail!(
                "indexed file `{file_path}` was not found for project {}",
                self.ctx.project_id
            )
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
    let embedding = ctx
        .embedding
        .clone()
        .ok_or(VectorLifecycleError::MissingEmbeddingConfig)?;
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
        VectorLifecycleError::DimensionMismatch { .. } => "dimension_mismatch",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sync_state_tracks_projection_success() {
        let files = vec!["src/ok.rs".to_string(), "src/fail.rs".to_string()];
        #[derive(Default)]
        struct State {
            synced: Vec<String>,
            marked_synced: Vec<String>,
        }
        let mut state = State::default();

        let report = sync_files_with_state(
            &files,
            &mut state,
            |state, file_path| {
                state.synced.push(file_path.to_string());
                if file_path == "src/fail.rs" {
                    anyhow::bail!("projection write failed");
                }
                Ok(3)
            },
            |state, file_path| {
                state.marked_synced.push(file_path.to_string());
                Ok(())
            },
        );

        assert_eq!(state.synced, vec!["src/ok.rs", "src/fail.rs"]);
        assert_eq!(state.marked_synced, vec!["src/ok.rs"]);
        assert_eq!(report.status, ProjectionStatus::Degraded);
        assert_eq!(report.synced_files, 1);
        assert_eq!(report.synced_symbols, 3);
        assert!(report.degraded);
        assert_eq!(
            report.error.as_ref().map(|error| error.kind.as_str()),
            Some("sync_failed")
        );
    }
}
