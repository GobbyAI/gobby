use crate::config::{CODE_SYMBOL_COLLECTION_PREFIX, Context};
use crate::db;
use crate::output::{self, Format};
use crate::projection::{
    self,
    sync::{ProjectionStatus, ProjectionSyncError, ProjectionSyncReport},
};
use crate::vector::code_symbols::{
    self, CodeSymbolVectorLifecycle, CodeSymbolVectorLifecycleAction,
    CodeSymbolVectorLifecycleOutput, CodeSymbolVectorLifecycleStatus, EmbeddingSource,
    VectorLifecycleError, VectorOrphanCleanup, embedding_source_from_context,
};
use serde::Serialize;
use std::collections::HashSet;

pub fn lifecycle_status(
    ctx: &Context,
    action: CodeSymbolVectorLifecycleAction,
) -> Result<CodeSymbolVectorLifecycleStatus, VectorLifecycleError> {
    let prefix = CODE_SYMBOL_COLLECTION_PREFIX;
    code_symbols::lifecycle_status(ctx.project_id.clone(), prefix, action)
}

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
}

pub fn sync_file(
    ctx: &Context,
    file_path: &str,
    allow_missing_indexed_file: bool,
    format: Format,
) -> anyhow::Result<()> {
    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    if !db::indexed_file_exists(&mut conn, &ctx.project_id, file_path)? {
        if allow_missing_indexed_file {
            return print_skipped_missing_indexed_file(ctx, file_path, format);
        }
        anyhow::bail!(
            "indexed file `{file_path}` was not found for project {}",
            ctx.project_id
        );
    }
    let mut lifecycle = lifecycle_from_context(ctx)?;
    let symbols = code_symbols::fetch_symbols_for_file(&mut conn, &ctx.project_id, file_path)?;
    let output = lifecycle.sync_file_symbols(file_path, &symbols)?;
    if !db::mark_vectors_synced(&mut conn, &ctx.project_id, file_path)? {
        if allow_missing_indexed_file {
            return print_skipped_missing_indexed_file(ctx, file_path, format);
        }
        anyhow::bail!(
            "indexed file `{file_path}` was not found for project {}",
            ctx.project_id
        );
    }
    let report = ProjectionSyncReport::ok(1, output.symbols);
    print_lifecycle_output(&output, report, format)
}

fn print_skipped_missing_indexed_file(
    ctx: &Context,
    file_path: &str,
    format: Format,
) -> anyhow::Result<()> {
    let failures = projection::reconcile_deleted_file(ctx, file_path);
    let collection = code_symbols::collection_name(CODE_SYMBOL_COLLECTION_PREFIX, &ctx.project_id)?;
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
    let degraded = error.is_some();
    let payload = serde_json::json!({
        "success": true,
        "project_id": ctx.project_id,
        "projection": "vector",
        "action": "sync_file",
        "file_path": file_path,
        "collection": collection,
        "status": "skipped",
        "reason": "indexed_file_not_found",
        "synced_files": 0,
        "synced_symbols": 0,
        "skipped_files": 1,
        "failed_files": 0,
        "degraded": degraded,
        "error": error,
        "summary": format!("skipped vector sync for {file_path}: indexed file not found"),
    });
    match format {
        Format::Json => output::print_json(&payload),
        Format::Text => {
            output::print_text(&format!(
                "Skipped code-symbol vector sync for project {}: indexed file `{file_path}` was not found",
                ctx.project_id
            ))?;
            output::print_json_compact(&payload)
        }
    }
}

pub fn clear(ctx: &Context, format: Format) -> anyhow::Result<()> {
    let mut lifecycle = lifecycle_from_context(ctx)?;
    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    db::reset_vectors_sync_for_project(&mut conn, &ctx.project_id)?;
    let output = lifecycle.clear_project_vectors()?;
    let report = ProjectionSyncReport::ok(0, 0);
    print_lifecycle_output(&output, report, format)
}

pub fn rebuild(ctx: &Context, format: Format) -> anyhow::Result<()> {
    let mut lifecycle = lifecycle_from_context(ctx)?;
    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    let file_paths = db::list_indexed_file_paths(&mut conn, &ctx.project_id)?;
    db::reset_vectors_sync_for_project(&mut conn, &ctx.project_id)?;
    let symbols = code_symbols::fetch_symbols_for_project(&mut conn, &ctx.project_id)?;
    let output = lifecycle.rebuild_symbols(&symbols)?;
    let files_synced = db::mark_project_vectors_synced(&mut conn, &ctx.project_id)? as usize;
    let report = ProjectionSyncReport::ok(files_synced.min(file_paths.len()), output.symbols);
    print_lifecycle_output(&output, report, format)
}

pub fn cleanup_orphans(ctx: &Context, format: Format) -> anyhow::Result<()> {
    let qdrant = ctx
        .qdrant
        .as_ref()
        .ok_or(VectorLifecycleError::MissingQdrantConfig)?;
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let indexed_file_paths = db::list_indexed_file_paths(&mut conn, &ctx.project_id)?
        .into_iter()
        .collect::<HashSet<_>>();
    let cleanup =
        code_symbols::cleanup_orphan_file_vectors(qdrant, &ctx.project_id, &indexed_file_paths)?;
    print_orphan_cleanup(&cleanup, format)
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

fn print_orphan_cleanup(cleanup: &VectorOrphanCleanup, format: Format) -> anyhow::Result<()> {
    let payload = serde_json::json!({
        "project_id": cleanup.project_id.as_str(),
        "projection": "vector",
        "action": "cleanup_orphans",
        "collection": cleanup.collection.as_str(),
        "status": ProjectionStatus::Ok,
        "vector_files_scanned": cleanup.vector_files_scanned,
        "orphan_files_deleted": cleanup.orphan_files_deleted,
        "vectors_deleted": cleanup.vectors_deleted,
        "summary": format!(
            "Removed {} orphaned vector file(s) and {} vector point(s)",
            cleanup.orphan_files_deleted, cleanup.vectors_deleted
        ),
    });
    match format {
        Format::Json => output::print_json(&payload),
        Format::Text => output::print_text(&serde_json::to_string(&payload)?),
    }
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
            code_vectors: crate::config::CodeVectorSettings { vector_dim: None },
            indexing: gobby_core::config::IndexingConfig::default(),
            daemon_url: None,
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

        let mut source = AiConfigSource::with_primary(NoPrimaryAiConfigSource, None);
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
}
