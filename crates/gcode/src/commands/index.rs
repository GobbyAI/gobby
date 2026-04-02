use crate::config::Context;
use crate::db;
use crate::index::indexer;
use crate::neo4j::Neo4jClient;

pub fn run(
    ctx: &Context,
    path: Option<String>,
    files: Option<Vec<String>>,
    full: bool,
) -> anyhow::Result<()> {
    // Resolve root, project_id, and DB connection — re-resolve if path
    // belongs to a different project than the CWD-derived context.
    let (root, project_id, conn) = match path.as_deref() {
        Some(p) => {
            let target = std::path::PathBuf::from(p);
            let target_root =
                crate::project::find_project_root(&target).unwrap_or_else(|| target.clone());
            if target_root != ctx.project_root {
                // Path belongs to a different project — re-resolve everything
                let db_path = crate::config::resolve_db_path(&target_root)?;
                let project_id = crate::project::read_project_id(&target_root)
                    .or_else(|_| crate::project::read_gcode_json(&target_root))
                    .unwrap_or_else(|_| crate::project::generate_project_id(&target_root));
                if !ctx.quiet {
                    eprintln!(
                        "Warning: path '{}' belongs to project {} (not {}), re-resolving context",
                        p,
                        &project_id[..8],
                        &ctx.project_id[..8]
                    );
                }
                let conn = db::open_readwrite(&db_path)?;
                (target_root, project_id, conn)
            } else {
                let conn = db::open_readwrite(&ctx.db_path)?;
                (target, ctx.project_id.clone(), conn)
            }
        }
        None => {
            let conn = db::open_readwrite(&ctx.db_path)?;
            (ctx.project_root.clone(), ctx.project_id.clone(), conn)
        }
    };

    // Auto-init: ensure identity file exists before indexing
    crate::project::ensure_gcode_json(&root)?;

    // Create Neo4j client if configured
    let neo4j_client = ctx.neo4j.as_ref().map(Neo4jClient::from_config);
    let neo4j_ref = neo4j_client.as_ref();
    let qdrant_ref = ctx.qdrant.as_ref();

    if let Some(file_list) = files {
        let result = indexer::index_files(
            &conn,
            &root,
            &project_id,
            &file_list,
            neo4j_ref,
            qdrant_ref,
            ctx.daemon_url.as_deref(),
        )?;
        if !ctx.quiet {
            eprintln!(
                "Indexed {} files, {} symbols in {}ms",
                result.files_indexed, result.symbols_found, result.duration_ms
            );
        }
    } else {
        let result = indexer::index_directory(
            &conn,
            &root,
            &project_id,
            !full,
            neo4j_ref,
            qdrant_ref,
            ctx.quiet,
            ctx.daemon_url.as_deref(),
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
