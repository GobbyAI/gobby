use crate::config;
use crate::config::Context;
use crate::index::api::{self, IndexDegradation, IndexOutcome, IndexRequest, UnsupportedFileType};
use crate::index_lock::{self, IndexLockPolicy, IndexLockResult};
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

    let outcome = match index_lock::with_project_lock(&target_ctx, IndexLockPolicy::Wait, || {
        api::index_files(request, &target_ctx)
    })? {
        IndexLockResult::Acquired(outcome) => outcome,
        IndexLockResult::Busy => anyhow::bail!(
            "index lock is busy for project {}; wait policy did not acquire it",
            target_ctx.project_id
        ),
    };
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
        Format::Text => output::print_text(&index_text(&outcome)),
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
            clone_context(
                ctx,
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
                short_id(&ctx.project_id)
            );
        }
        if identity.should_write_gcode_json {
            crate::project::ensure_gcode_json(&target_root)?;
        }
        let mut conn = crate::db::connect_readonly(&ctx.database_url)?;
        crate::config::validate_parent_code_index(&mut conn, &identity.index_scope)?;
        Ok((
            clone_context(ctx, target_root, identity.project_id, identity.index_scope),
            target_filter,
        ))
    } else {
        Ok((
            clone_context(
                ctx,
                target_root,
                ctx.project_id.clone(),
                ctx.index_scope.clone(),
            ),
            target_filter,
        ))
    }
}

fn clone_context(
    ctx: &Context,
    project_root: std::path::PathBuf,
    project_id: String,
    index_scope: config::ProjectIndexScope,
) -> Context {
    config::Context {
        database_url: ctx.database_url.clone(),
        project_root,
        project_id,
        quiet: ctx.quiet,
        falkordb: ctx.falkordb.clone(),
        qdrant: ctx.qdrant.clone(),
        embedding: ctx.embedding.clone(),
        code_vectors: ctx.code_vectors.clone(),
        indexing: ctx.indexing,
        daemon_url: ctx.daemon_url.clone(),
        index_scope,
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
