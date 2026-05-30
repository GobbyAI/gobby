use crate::config::Context;
use crate::db;
use crate::graph::code_graph::{self, GraphLifecycleAction, GraphLifecycleOutput};
use crate::output::{self, Format};
use crate::projection::sync::ProjectionSyncReport;
use serde_json::{Value, json};

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
    SkippedMissingIndexedFile,
}

pub(super) fn skipped_missing_indexed_file_payload(ctx: &Context, file_path: &str) -> Value {
    json!({
        "project_id": ctx.project_id,
        "file_path": file_path,
        "status": "skipped",
        "reason": "indexed_file_not_found",
    })
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
    if !db::indexed_file_exists(&mut conn, &ctx.project_id, file_path)? {
        if allow_missing_indexed_file {
            return Ok(GraphFileSyncOutcome::SkippedMissingIndexedFile);
        }
        return Err(GraphSyncContractError::indexed_file_not_found(ctx, file_path).into());
    }

    code_graph::require_graph_reads(ctx)?;
    let facts = db::read_graph_file_facts(&mut conn, &ctx.project_id, file_path)?;
    if !db::mark_graph_sync_attempted(&mut conn, &ctx.project_id, file_path)? {
        if allow_missing_indexed_file {
            return Ok(GraphFileSyncOutcome::SkippedMissingIndexedFile);
        }
        return Err(GraphSyncContractError::indexed_file_not_found(ctx, file_path).into());
    }
    let relationships_written = code_graph::sync_file_graph(
        ctx,
        &facts.file_path,
        &facts.imports,
        &facts.definitions,
        &facts.calls,
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
    code_graph::clear_project(ctx)?;
    db::reset_graph_sync_for_project(&mut conn, &ctx.project_id)?;

    let mut files_synced = 0usize;
    let mut symbols_synced = 0usize;
    let mut errors = Vec::new();
    for file_path in &file_paths {
        let synced_symbols =
            match db::mark_graph_sync_attempted(&mut conn, &ctx.project_id, file_path)
                .and_then(|updated| {
                    if updated {
                        Ok(())
                    } else {
                        anyhow::bail!("indexed file no longer exists")
                    }
                })
                .and_then(|_| {
                    let facts = db::read_graph_file_facts(&mut conn, &ctx.project_id, file_path)?;
                    code_graph::sync_file_graph(
                        ctx,
                        &facts.file_path,
                        &facts.imports,
                        &facts.definitions,
                        &facts.calls,
                    )?;
                    db::mark_graph_synced(&mut conn, &ctx.project_id, file_path)?;
                    Ok(facts.definitions.len())
                }) {
                Ok(symbols) => symbols,
                Err(err) => {
                    errors.push(format!("{file_path}: {err}"));
                    continue;
                }
            };
        files_synced += 1;
        symbols_synced += synced_symbols;
    }

    let report = if errors.is_empty() {
        ProjectionSyncReport::ok(files_synced, symbols_synced)
    } else {
        ProjectionSyncReport::degraded(
            "sync_failed",
            errors.join("; "),
            files_synced,
            symbols_synced,
        )
    };
    Ok(lifecycle_output(
        GraphLifecycleAction::Rebuild,
        ctx,
        json!({
            "success": true,
            "project_id": ctx.project_id,
            "status": report.status,
            "synced_files": report.synced_files,
            "synced_symbols": report.synced_symbols,
            "degraded": report.degraded,
            "error": report.error,
            "files_processed": file_paths.len(),
            "files_synced": files_synced,
            "files_failed": errors.len(),
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

pub fn sync_file(
    ctx: &Context,
    file_path: &str,
    allow_missing_indexed_file: bool,
    format: Format,
) -> anyhow::Result<()> {
    let sync = sync_file_graph(ctx, file_path, allow_missing_indexed_file)?;
    let GraphFileSyncOutcome::Synced {
        relationships_written,
        symbols_synced,
    } = sync
    else {
        let payload = skipped_missing_indexed_file_payload(ctx, file_path);
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
