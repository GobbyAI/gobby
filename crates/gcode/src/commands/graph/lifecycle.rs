use crate::config::Context;
use crate::db;
use crate::graph::code_graph::{self, GraphLifecycleAction, GraphLifecycleOutput};
use crate::output::{self, Format};
use crate::projection::{self, ProjectionReconcileFailure, sync::ProjectionSyncReport};
use serde_json::{Value, json};
use std::collections::HashSet;

pub const GRAPH_SYNC_CONTRACT_EXIT_CODE: u8 = 2;

#[derive(Debug)]
pub struct GraphSyncContractError {
    payload: Value,
}

impl GraphSyncContractError {
    pub(super) fn project_not_indexed(ctx: &Context, file_path: &str) -> Self {
        Self {
            payload: json!({
                "success": false,
                "project_id": ctx.project_id,
                "file_path": file_path,
                "status": "error",
                "reason": "project_not_indexed",
                "error": format!("project {} is not indexed", ctx.project_id),
            }),
        }
    }

    pub(super) fn indexed_file_not_found(ctx: &Context, file_path: &str) -> Self {
        Self {
            payload: json!({
                "success": false,
                "project_id": ctx.project_id,
                "file_path": file_path,
                "status": "error",
                "reason": "indexed_file_not_found",
                "error": format!("indexed file `{file_path}` was not found for project {}", ctx.project_id),
            }),
        }
    }

    pub fn exit_code(&self) -> u8 {
        GRAPH_SYNC_CONTRACT_EXIT_CODE
    }

    pub fn print(&self) -> anyhow::Result<()> {
        output::print_json(&self.payload)
    }

    pub fn payload(&self) -> &Value {
        &self.payload
    }
}

impl std::fmt::Display for GraphSyncContractError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let reason = self
            .payload
            .get("reason")
            .and_then(Value::as_str)
            .unwrap_or("graph_sync_contract_error");
        write!(f, "graph sync-file contract error: {reason}")
    }
}

impl std::error::Error for GraphSyncContractError {}

pub(super) fn format_success_text(output: &GraphLifecycleOutput) -> String {
    format!(
        "{} for project {}: {}",
        output.action.success_prefix(),
        output.project_id,
        output.summary
    )
}

pub(super) trait LifecycleBackend {
    fn run(
        &self,
        ctx: &Context,
        action: GraphLifecycleAction,
    ) -> anyhow::Result<GraphLifecycleOutput>;
}

struct CodeGraphLifecycleBackend;

impl LifecycleBackend for CodeGraphLifecycleBackend {
    fn run(
        &self,
        ctx: &Context,
        action: GraphLifecycleAction,
    ) -> anyhow::Result<GraphLifecycleOutput> {
        match action {
            GraphLifecycleAction::Clear => clear_project_graph(ctx),
            GraphLifecycleAction::Rebuild => rebuild_project_graph(ctx),
        }
    }
}

pub(super) fn run_lifecycle_action_with_backend(
    ctx: &Context,
    action: GraphLifecycleAction,
    format: Format,
    backend: &impl LifecycleBackend,
) -> anyhow::Result<()> {
    let output = backend.run(ctx, action)?;
    match format {
        Format::Json => output::print_json(&output.payload),
        Format::Text => {
            output::print_text(&format_success_text(&output))?;
            output::print_json_compact(&output.payload)
        }
    }
}

fn lifecycle_output(
    action: GraphLifecycleAction,
    ctx: &Context,
    payload: Value,
) -> GraphLifecycleOutput {
    let summary = code_graph::extract_summary_text(&payload).unwrap_or_else(|| payload.to_string());
    GraphLifecycleOutput {
        project_id: ctx.project_id.clone(),
        action,
        summary,
        payload,
    }
}

enum GraphFileSyncOutcome {
    Synced {
        relationships_written: usize,
        symbols_synced: usize,
    },
    SkippedNoGraphFacts,
    SkippedMissingIndexedFile {
        reconcile_failures: Vec<ProjectionReconcileFailure>,
    },
}

pub(super) fn skipped_missing_indexed_file_payload(
    ctx: &Context,
    file_path: &str,
    reconcile_failures: &[ProjectionReconcileFailure],
) -> Value {
    let error = if reconcile_failures.is_empty() {
        None
    } else {
        Some(serde_json::json!({
            "kind": "projection_reconcile_failed",
            "message": reconcile_failures
                .iter()
                .map(|failure| {
                    format!(
                        "failed to reconcile {:?} projection: {}",
                        failure.target, failure.message
                    )
                })
                .collect::<Vec<_>>()
                .join("; "),
        }))
    };
    json!({
        "success": true,
        "project_id": ctx.project_id,
        "file_path": file_path,
        "status": "skipped",
        "reason": "indexed_file_not_found",
        "synced_files": 0,
        "synced_symbols": 0,
        "skipped_files": 1,
        "failed_files": 0,
        "relationships_written": 0,
        "degraded": error.is_some(),
        "error": error,
        "summary": format!("skipped graph sync for {file_path}: indexed file not found"),
    })
}

pub(super) fn skipped_no_graph_facts_payload(ctx: &Context, file_path: &str) -> Value {
    json!({
        "success": true,
        "project_id": ctx.project_id,
        "file_path": file_path,
        "status": "skipped",
        "reason": "no_graph_facts",
        "synced_files": 1,
        "synced_symbols": 0,
        "skipped_files": 1,
        "failed_files": 0,
        "relationships_written": 0,
        "degraded": false,
        "error": null,
        "summary": format!("skipped graph sync for {file_path}: no graph facts"),
    })
}

pub(super) fn has_no_graph_facts<I, D, C>(imports: &[I], definitions: &[D], calls: &[C]) -> bool {
    imports.is_empty() && definitions.is_empty() && calls.is_empty()
}

fn sync_file_graph(
    ctx: &Context,
    file_path: &str,
    allow_missing_indexed_file: bool,
) -> anyhow::Result<GraphFileSyncOutcome> {
    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    if !db::indexed_project_exists(&mut conn, &ctx.project_id)? {
        return Err(GraphSyncContractError::project_not_indexed(ctx, file_path).into());
    }
    code_graph::require_graph_reads(ctx)?;
    if !db::mark_graph_sync_attempted(&mut conn, &ctx.project_id, file_path)? {
        if allow_missing_indexed_file {
            return Ok(GraphFileSyncOutcome::SkippedMissingIndexedFile {
                reconcile_failures: projection::reconcile_deleted_file(ctx, file_path),
            });
        }
        return Err(GraphSyncContractError::indexed_file_not_found(ctx, file_path).into());
    }
    let facts = db::read_graph_file_facts(&mut conn, &ctx.project_id, file_path)?;
    if has_no_graph_facts(&facts.imports, &facts.definitions, &facts.calls) {
        code_graph::with_code_graph(ctx, |graph| {
            graph.delete_file_graph(&facts.file_path, &[])?;
            graph.delete_file_node(&facts.file_path)
        })?;
        db::mark_graph_synced(&mut conn, &ctx.project_id, file_path)?;
        return Ok(GraphFileSyncOutcome::SkippedNoGraphFacts);
    }
    // Per-file sync intentionally does NOT run project-wide orphan cleanup:
    // `cleanup_orphans` performs O(project graph size) anti-join sweeps that are
    // unbounded relative to this one file and caused the daemon's per-file sync to
    // time out on large graphs. File-scoped stale records are still removed by
    // `delete_stale_file_graph` inside the sync. Project-wide orphan GC runs in
    // `graph rebuild` and the dedicated `graph cleanup-orphans` command instead.
    let relationships_written = code_graph::sync_file_graph(
        ctx,
        &facts.file_path,
        &facts.imports,
        &facts.definitions,
        &facts.calls,
        false,
    )?;
    db::mark_graph_synced(&mut conn, &ctx.project_id, file_path)?;
    Ok(GraphFileSyncOutcome::Synced {
        relationships_written,
        symbols_synced: facts.definitions.len(),
    })
}

fn clear_project_graph(ctx: &Context) -> anyhow::Result<GraphLifecycleOutput> {
    code_graph::require_graph_reads(ctx)?;
    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    let files_marked_pending = db::reset_graph_sync_for_project(&mut conn, &ctx.project_id)?;
    code_graph::clear_project(ctx)?;
    let report = ProjectionSyncReport::ok(0, 0);
    Ok(lifecycle_output(
        GraphLifecycleAction::Clear,
        ctx,
        json!({
            "success": true,
            "project_id": ctx.project_id,
            "status": report.status,
            "synced_files": report.synced_files,
            "synced_symbols": report.synced_symbols,
            "skipped_files": report.skipped_files,
            "failed_files": report.failed_files,
            "degraded": report.degraded,
            "error": report.error,
            "files_marked_pending": files_marked_pending,
            "summary": format!("marked {files_marked_pending} files pending and cleared graph projection"),
        }),
    ))
}

fn rebuild_project_graph(ctx: &Context) -> anyhow::Result<GraphLifecycleOutput> {
    code_graph::require_graph_reads(ctx)?;
    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    let file_paths = db::list_indexed_file_paths(&mut conn, &ctx.project_id)?;

    let mut files_synced = 0usize;
    let mut symbols_synced = 0usize;
    let mut files_skipped = 0usize;
    let mut files_failed = 0usize;
    let mut errors = Vec::new();
    let mut error_kind = None;
    code_graph::with_code_graph(ctx, |graph| {
        for file_path in &file_paths {
            match db::mark_graph_sync_attempted(&mut conn, &ctx.project_id, file_path) {
                Ok(true) => {}
                Ok(false) => {
                    files_skipped += 1;
                    for failure in projection::reconcile_deleted_file(ctx, file_path) {
                        error_kind.get_or_insert_with(|| "projection_reconcile_failed".to_string());
                        errors.push(format!(
                            "{file_path}: failed to reconcile {:?} projection: {}",
                            failure.target, failure.message
                        ));
                    }
                    continue;
                }
                Err(err) => {
                    files_failed += 1;
                    error_kind.get_or_insert_with(|| "sync_failed".to_string());
                    errors.push(format!("{file_path}: {err}"));
                    continue;
                }
            }

            let synced_symbols = match (|| -> anyhow::Result<usize> {
                let facts = db::read_graph_file_facts(&mut conn, &ctx.project_id, file_path)?;
                if has_no_graph_facts(&facts.imports, &facts.definitions, &facts.calls) {
                    graph.delete_file_graph(&facts.file_path, &[])?;
                    graph.delete_file_node(&facts.file_path)?;
                    db::mark_graph_synced(&mut conn, &ctx.project_id, file_path)?;
                    return Ok(0);
                }
                graph.sync_file(
                    &facts.file_path,
                    &facts.imports,
                    &facts.definitions,
                    &facts.calls,
                    false,
                )?;
                db::mark_graph_synced(&mut conn, &ctx.project_id, file_path)?;
                Ok(facts.definitions.len())
            })() {
                Ok(symbols) => symbols,
                Err(err) => {
                    files_failed += 1;
                    error_kind.get_or_insert_with(|| "sync_failed".to_string());
                    errors.push(format!("{file_path}: {err}"));
                    continue;
                }
            };
            files_synced += 1;
            symbols_synced += synced_symbols;
        }
        Ok(())
    })?;
    if errors.is_empty()
        && files_synced > 0
        && let Err(err) = code_graph::cleanup_orphans(ctx)
    {
        error_kind.get_or_insert_with(|| "sync_failed".to_string());
        errors.push(format!("cleanup_orphans: {err}"));
    }

    let report = if errors.is_empty() {
        ProjectionSyncReport::ok_with_counts(
            files_synced,
            symbols_synced,
            files_skipped,
            files_failed,
        )
    } else {
        ProjectionSyncReport::degraded_with_counts(
            error_kind.unwrap_or_else(|| "sync_failed".to_string()),
            errors.join("; "),
            files_synced,
            symbols_synced,
            files_skipped,
            files_failed,
        )
    };
    Ok(lifecycle_output(
        GraphLifecycleAction::Rebuild,
        ctx,
        json!({
            "success": errors.is_empty(),
            "project_id": ctx.project_id,
            "status": report.status,
            "synced_files": report.synced_files,
            "synced_symbols": report.synced_symbols,
            "skipped_files": report.skipped_files,
            "failed_files": report.failed_files,
            "degraded": report.degraded,
            "error": report.error,
            "files_processed": file_paths.len(),
            "files_synced": files_synced,
            "files_skipped": files_skipped,
            "files_failed": files_failed,
            "errors": errors,
            "summary": format!("synced {files_synced}/{} files", file_paths.len()),
        }),
    ))
}

pub fn clear(ctx: &Context, format: Format) -> anyhow::Result<()> {
    run_lifecycle_action_with_backend(
        ctx,
        GraphLifecycleAction::Clear,
        format,
        &CodeGraphLifecycleBackend,
    )
}

pub fn rebuild(ctx: &Context, format: Format) -> anyhow::Result<()> {
    run_lifecycle_action_with_backend(
        ctx,
        GraphLifecycleAction::Rebuild,
        format,
        &CodeGraphLifecycleBackend,
    )
}

/// Run project-wide orphan cleanup as a standalone maintenance command.
///
/// This is the same sweep that `graph rebuild` performs at the end, exposed on
/// its own so the daemon can schedule it periodically instead of paying its
/// O(project graph size) cost on every per-file `graph sync-file`.
pub fn cleanup_orphans(ctx: &Context, format: Format) -> anyhow::Result<()> {
    code_graph::require_graph_reads(ctx)?;
    let cleanup = cleanup_deleted_project_graph(ctx)?;
    let payload = json!({
        "status": "ok",
        "action": "cleanup_orphans",
        "project_id": ctx.project_id.clone(),
        "stale_graph_files_deleted": cleanup.stale_files_deleted,
        "graph_nodes_deleted": cleanup.graph_nodes_deleted,
    });
    match format {
        Format::Json => output::print_json(&payload),
        Format::Text => {
            output::print_text(&format!(
                "Removed {} stale code-graph file(s) and {} file-scoped graph node(s)",
                cleanup.stale_files_deleted, cleanup.graph_nodes_deleted
            ))?;
            output::print_json_compact(&payload)
        }
    }
}

pub(crate) fn cleanup_deleted_project_graph(
    ctx: &Context,
) -> anyhow::Result<code_graph::GraphOrphanCleanup> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let indexed_file_paths = db::list_indexed_file_paths(&mut conn, &ctx.project_id)?
        .into_iter()
        .collect::<HashSet<_>>();
    code_graph::cleanup_deleted_files(ctx, &indexed_file_paths)
}

pub fn sync_file(
    ctx: &Context,
    file_path: &str,
    allow_missing_indexed_file: bool,
    format: Format,
) -> anyhow::Result<()> {
    let sync = sync_file_graph(ctx, file_path, allow_missing_indexed_file)?;
    let (relationships_written, symbols_synced) = match sync {
        GraphFileSyncOutcome::Synced {
            relationships_written,
            symbols_synced,
        } => (relationships_written, symbols_synced),
        GraphFileSyncOutcome::SkippedNoGraphFacts => {
            let payload = skipped_no_graph_facts_payload(ctx, file_path);
            return match format {
                Format::Json => output::print_json(&payload),
                Format::Text => {
                    output::print_text(&format!(
                        "Skipped code-index graph sync for project {}: `{file_path}` has no graph facts",
                        ctx.project_id
                    ))?;
                    output::print_json_compact(&payload)
                }
            };
        }
        GraphFileSyncOutcome::SkippedMissingIndexedFile { reconcile_failures } => {
            let payload = skipped_missing_indexed_file_payload(ctx, file_path, &reconcile_failures);
            return match format {
                Format::Json => output::print_json(&payload),
                Format::Text => {
                    output::print_text(&format!(
                        "Skipped code-index graph sync for project {}: indexed file `{file_path}` was not found",
                        ctx.project_id
                    ))?;
                    output::print_json_compact(&payload)
                }
            };
        }
    };
    let report = ProjectionSyncReport::ok(1, symbols_synced);
    let summary = format!("synced {relationships_written} graph relationships for {file_path}");
    let payload = json!({
        "success": true,
        "project_id": ctx.project_id,
        "file_path": file_path,
        "status": report.status,
        "synced_files": report.synced_files,
        "synced_symbols": report.synced_symbols,
        "skipped_files": report.skipped_files,
        "failed_files": report.failed_files,
        "degraded": report.degraded,
        "error": report.error,
        "relationships_written": relationships_written,
        "summary": summary,
    });
    match format {
        Format::Json => output::print_json(&payload),
        Format::Text => {
            output::print_text(&format!(
                "Synced code-index graph for project {}: {summary}",
                ctx.project_id
            ))?;
            output::print_json_compact(&payload)
        }
    }
}
