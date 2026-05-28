use crate::config;
use crate::config::Context;
use crate::index::api::{self, IndexDegradation, IndexOutcome, IndexRequest};
use crate::output::{self, Format};
use crate::projection::sync::{self, ProjectionSyncReports};
use crate::utils::short_id;
use serde::Serialize;

pub fn run(
    ctx: &Context,
    path: Option<String>,
    files: Option<Vec<String>>,
    full: bool,
    require_cpp_semantics: bool,
    sync_projections: bool,
    format: Format,
) -> anyhow::Result<()> {
    let (target_ctx, path_filter) = resolve_index_context(ctx, path.as_deref())?;
    let explicit_files: Vec<std::path::PathBuf> = files
        .unwrap_or_default()
        .into_iter()
        .map(std::path::PathBuf::from)
        .collect();
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

    let outcome = api::index_files(request, &target_ctx)?;
    if sync_projections {
        let projections = sync::sync_after_index(&target_ctx, &outcome.indexed_file_paths)?;
        let payload = sync_projections_payload(&outcome, projections);
        return match format {
            Format::Json => output::print_json(&payload),
            Format::Text => output::print_text(&sync_projections_text(&payload)?),
        };
    }

    match format {
        Format::Json => output::print_json(&outcome),
        Format::Text => output::print_text(&format!(
            "Indexed {} files ({} skipped), {} symbols, {} chunks in {}ms",
            outcome.indexed_files,
            outcome.skipped_files,
            outcome.symbols_indexed,
            outcome.chunks_indexed,
            outcome.durations.total_ms
        )),
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub(crate) struct IndexSyncProjectionsOutput {
    pub indexed_files: usize,
    pub skipped_files: usize,
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
            clone_context(ctx, ctx.project_root.clone(), ctx.project_id.clone()),
            None,
        ));
    };

    // Resolve root and project_id. If the path belongs to a different project
    // than the CWD-derived context, re-resolve identity for that project.
    let target = std::path::PathBuf::from(p);
    let target_root = crate::config::detect_project_root_from(&target)?;
    let target_filter = path_filter_for(&target_root, &target);
    if target_root != ctx.project_root {
        let identity = crate::config::resolve_project_identity(
            &target_root,
            crate::config::MissingIdentity::Generate,
        )?;
        crate::config::warn_project_identity(&identity, ctx.quiet);
        if !ctx.quiet {
            eprintln!(
                "Warning: path '{}' belongs to project {} (not {}), re-resolving context",
                p,
                short_id(&identity.project_id),
                &ctx.project_id[..8]
            );
        }
        if identity.should_write_gcode_json {
            crate::project::ensure_gcode_json(&target_root)?;
        }
        Ok((
            clone_context(ctx, target_root, identity.project_id),
            target_filter,
        ))
    } else {
        Ok((
            clone_context(ctx, target_root, ctx.project_id.clone()),
            target_filter,
        ))
    }
}

fn clone_context(ctx: &Context, project_root: std::path::PathBuf, project_id: String) -> Context {
    config::Context {
        database_url: ctx.database_url.clone(),
        project_root,
        project_id,
        quiet: ctx.quiet,
        falkordb: ctx.falkordb.clone(),
        qdrant: ctx.qdrant.clone(),
        embedding: ctx.embedding.clone(),
        code_vectors: ctx.code_vectors.clone(),
        daemon_url: ctx.daemon_url.clone(),
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
mod tests {
    use super::*;
    use crate::index::api::IndexOutcome;
    use crate::projection::sync::{
        ProjectionStatus, ProjectionSyncError, ProjectionSyncReport, ProjectionSyncReports,
    };
    use serde_json::{Value, json};

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
                degraded: false,
                error: None,
            },
            vector: ProjectionSyncReport {
                status: ProjectionStatus::Degraded,
                synced_files: 0,
                synced_symbols: 0,
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
        assert_eq!(
            serde_json::to_value(&payload).expect("payload serializes"),
            json!({
                "indexed_files": 12,
                "skipped_files": 0,
                "symbols_indexed": 348,
                "chunks_indexed": 921,
                "projections": {
                    "graph": {
                        "status": "ok",
                        "synced_files": 12,
                        "synced_symbols": 348,
                        "degraded": false,
                        "error": null
                    },
                    "vector": {
                        "status": "degraded",
                        "synced_files": 0,
                        "synced_symbols": 0,
                        "degraded": true,
                        "error": {
                            "kind": "missing_qdrant_config",
                            "message": "Qdrant config is required"
                        }
                    }
                }
            })
        );
    }

    #[test]
    fn sync_projections_text_contract() {
        let payload = sync_projections_payload(&sample_outcome(), sample_reports());
        let text = sync_projections_text(&payload).expect("text payload");
        let parsed: Value = serde_json::from_str(&text).expect("text mode is structured JSON");
        assert_eq!(parsed["indexed_files"], 12);
        assert_eq!(parsed["projections"]["graph"]["status"], "ok");
        assert_eq!(parsed["projections"]["vector"]["status"], "degraded");
    }
}
