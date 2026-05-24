use crate::config::Context;
use crate::db;
use crate::index::indexer;

pub fn run(
    ctx: &Context,
    path: Option<String>,
    files: Option<Vec<String>>,
    full: bool,
    require_cpp_semantics: bool,
) -> anyhow::Result<()> {
    // Resolve root, project_id, and DB connection — re-resolve if path
    // belongs to a different project than the CWD-derived context.
    let (root, project_id, mut conn) = match path.as_deref() {
        Some(p) => {
            let target = std::path::PathBuf::from(p);
            let target_root = crate::config::detect_project_root_from(&target)?;
            if target_root != ctx.project_root {
                // Path belongs to a different project — re-resolve everything
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
                let conn = db::connect_readwrite(&ctx.database_url)?;
                if identity.should_write_gcode_json {
                    crate::project::ensure_gcode_json(&target_root)?;
                }
                (target_root, identity.project_id, conn)
            } else {
                let conn = db::connect_readwrite(&ctx.database_url)?;
                (target_root, ctx.project_id.clone(), conn)
            }
        }
        None => {
            let conn = db::connect_readwrite(&ctx.database_url)?;
            (ctx.project_root.clone(), ctx.project_id.clone(), conn)
        }
    };

    if let Some(file_list) = files {
        let result = indexer::index_files(
            &mut conn,
            &root,
            &project_id,
            &file_list,
            require_cpp_semantics,
        )?;
        if !ctx.quiet {
            eprintln!(
                "Indexed {} files, {} symbols in {}ms",
                result.files_indexed, result.symbols_found, result.duration_ms
            );
        }
    } else {
        let result = indexer::index_directory(
            &mut conn,
            &root,
            &project_id,
            !full,
            ctx.quiet,
            require_cpp_semantics,
        )?;
        if !ctx.quiet {
            eprintln!(
                "Indexed {} files ({} skipped), {} symbols in {}ms",
                result.files_indexed,
                result.files_skipped,
                result.symbols_found,
                result.duration_ms
            );
        }
    }

    Ok(())
}

fn short_id(id: &str) -> &str {
    id.get(..8).unwrap_or(id)
}
