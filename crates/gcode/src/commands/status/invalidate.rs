use crate::config::Context;
use crate::db;
use crate::graph::code_graph;
use crate::index::indexer;
use crate::output::Format;
use crate::vector::code_symbols;

pub fn invalidate(ctx: &Context, force: bool, _format: Format) -> anyhow::Result<()> {
    if !force {
        let project_name = ctx
            .project_root
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_else(|| ctx.project_id.clone());

        eprint!(
            "This will clear the entire code index for '{}'. Continue? [y/N] ",
            project_name
        );
        let _ = std::io::Write::flush(&mut std::io::stderr());

        let mut input = String::new();
        std::io::stdin().read_line(&mut input)?;
        if !input.trim().eq_ignore_ascii_case("y") {
            eprintln!("Aborted.");
            return Ok(());
        }
    }

    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    indexer::invalidate(&mut conn, &ctx.project_id, ctx.daemon_url.as_deref())?;
    cleanup_project_projections(ctx)
}

fn cleanup_project_projections(ctx: &Context) -> anyhow::Result<()> {
    if ctx.falkordb.is_some() {
        code_graph::clear_project(ctx)
            .map_err(|err| anyhow::anyhow!("failed to clear FalkorDB projection: {err}"))?;
    }
    if let Some(qdrant) = &ctx.qdrant {
        code_symbols::delete_project_collection(qdrant, &ctx.project_id)
            .map_err(|err| anyhow::anyhow!("failed to delete Qdrant projection: {err}"))?;
    }
    Ok(())
}
