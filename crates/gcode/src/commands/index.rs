use crate::config::Context;
use crate::db;
use crate::index::indexer;
use crate::neo4j::Neo4jClient;

pub fn run(ctx: &Context, path: Option<String>, files: Option<Vec<String>>) -> anyhow::Result<()> {
    // Auto-init: ensure identity file exists before indexing
    crate::project::ensure_gcode_json(&ctx.project_root)?;

    let conn = db::open_readwrite(&ctx.db_path)?;
    let root = path
        .as_deref()
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| ctx.project_root.clone());

    // Create Neo4j client if configured
    let neo4j_client = ctx.neo4j.as_ref().map(Neo4jClient::from_config);
    let neo4j_ref = neo4j_client.as_ref();
    let qdrant_ref = ctx.qdrant.as_ref();

    if let Some(file_list) = files {
        let result = indexer::index_files(
            &conn,
            &root,
            &ctx.project_id,
            &file_list,
            neo4j_ref,
            qdrant_ref,
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
            &ctx.project_id,
            true,
            neo4j_ref,
            qdrant_ref,
            ctx.quiet,
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
