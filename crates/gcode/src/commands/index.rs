use crate::config;
use crate::config::Context;
use crate::index::api::{self, IndexRequest};
use crate::output::{self, Format};
use crate::utils::short_id;

pub fn run(
    ctx: &Context,
    path: Option<String>,
    files: Option<Vec<String>>,
    full: bool,
    require_cpp_semantics: bool,
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
        sync_projections: false,
    };

    let outcome = api::index_files(request, &target_ctx)?;
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
