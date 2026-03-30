use std::collections::HashMap;

use crate::config::Context;
use crate::db;
use crate::models::{SearchResult, Symbol};
use crate::output::{self, Format};
use crate::search::{fts, graph_boost, rrf, semantic};

pub fn search(
    ctx: &Context,
    query: &str,
    limit: usize,
    kind: Option<&str>,
    format: Format,
) -> anyhow::Result<()> {
    let conn = db::open_readonly(&ctx.db_path)?;

    // Source 1: FTS5 (with LIKE fallback)
    let mut fts_results = fts::search_symbols_fts(&conn, query, &ctx.project_id, kind, limit * 2);
    if fts_results.is_empty() {
        fts_results = fts::search_symbols_by_name(&conn, query, &ctx.project_id, kind, limit * 2);
    }
    let fts_ids: Vec<String> = fts_results.iter().map(|s| s.id.clone()).collect();

    // Source 2: Semantic search (Qdrant + embeddings)
    let semantic_results = semantic::semantic_search(ctx, query, limit * 2);
    let semantic_ids: Vec<String> = semantic_results.iter().map(|(id, _)| id.clone()).collect();

    // Source 3: Graph boost (Neo4j callers + usages)
    let graph_ids = graph_boost::graph_boost(ctx, query);

    // Build RRF sources (only include non-empty sources)
    let mut sources: Vec<(&str, Vec<String>)> = vec![("fts", fts_ids)];
    if !semantic_ids.is_empty() {
        sources.push(("semantic", semantic_ids));
    }
    if !graph_ids.is_empty() {
        sources.push(("graph", graph_ids));
    }

    let merged = rrf::merge(sources);

    // Build symbol cache from FTS results
    let mut symbol_cache: HashMap<String, Symbol> = HashMap::new();
    for sym in fts_results {
        symbol_cache.insert(sym.id.clone(), sym);
    }

    // Resolve results
    let mut results: Vec<SearchResult> = Vec::new();
    for (sym_id, score, source_names) in merged.iter().take(limit) {
        let sym = symbol_cache.get(sym_id).cloned().or_else(|| {
            conn.query_row(
                "SELECT * FROM code_symbols WHERE id = ?1",
                rusqlite::params![sym_id],
                Symbol::from_row,
            )
            .ok()
        });

        if let Some(s) = sym {
            let mut result = s.to_brief();
            result.score = *score;
            result.sources = Some(source_names.clone());
            results.push(result);
        }
    }

    if results.is_empty() && !crate::project::has_identity_file(&ctx.project_root) {
        eprintln!("No index found for this project. Run `gcode index` first.");
    }

    match format {
        Format::Json => output::print_json(&results),
        Format::Text => {
            for r in &results {
                let sources = r.sources.as_ref().map(|s| s.join("+")).unwrap_or_default();
                println!(
                    "{}:{} [{}] {} (score: {:.4}, via: {})",
                    r.file_path, r.line_start, r.kind, r.qualified_name, r.score, sources
                );
            }
            Ok(())
        }
    }
}

pub fn search_text(ctx: &Context, query: &str, limit: usize, format: Format) -> anyhow::Result<()> {
    let conn = db::open_readonly(&ctx.db_path)?;
    let results = fts::search_text(&conn, query, &ctx.project_id, limit);
    match format {
        Format::Json => output::print_json(&results),
        Format::Text => {
            for r in &results {
                println!(
                    "{}:{} [{}] {}",
                    r.file_path, r.line_start, r.kind, r.qualified_name
                );
            }
            Ok(())
        }
    }
}

pub fn search_content(
    ctx: &Context,
    query: &str,
    limit: usize,
    format: Format,
) -> anyhow::Result<()> {
    let conn = db::open_readonly(&ctx.db_path)?;
    let results = fts::search_content(&conn, query, &ctx.project_id, limit);
    match format {
        Format::Json => output::print_json(&results),
        Format::Text => {
            for r in &results {
                println!(
                    "{}:{}-{} {}",
                    r.file_path, r.line_start, r.line_end, r.snippet
                );
            }
            Ok(())
        }
    }
}
