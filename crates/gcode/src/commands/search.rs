use std::collections::HashMap;
use std::collections::HashSet;

use rusqlite::Connection;

use crate::config::Context;
use crate::db;
use crate::models::{PagedResponse, SearchResult, Symbol};
use crate::output::{self, Format};
use crate::search::{fts, graph_boost, rrf, semantic};

pub fn search(
    ctx: &Context,
    query: &str,
    limit: usize,
    offset: usize,
    kind: Option<&str>,
    format: Format,
) -> anyhow::Result<()> {
    let conn = db::open_readonly(&ctx.db_path)?;

    // Fetch generously for RRF. Total is a best-effort estimate bounded by fetch_limit
    // per source — exact counts aren't feasible because RRF merges results from FTS5,
    // Qdrant, and Neo4j with deduplication, so source counts aren't additive.
    let fetch_limit = ((offset + limit) * 3).max(200);

    // Source 1: FTS5 (with LIKE fallback)
    let mut fts_results = fts::search_symbols_fts(&conn, query, &ctx.project_id, kind, fetch_limit);
    if fts_results.is_empty() {
        fts_results = fts::search_symbols_by_name(&conn, query, &ctx.project_id, kind, fetch_limit);
    }
    let fts_ids: Vec<String> = fts_results.iter().map(|s| s.id.clone()).collect();

    // Source 2: Semantic search (Qdrant + embeddings)
    let semantic_results = semantic::semantic_search(ctx, query, fetch_limit);
    let semantic_ids: Vec<String> = semantic_results.iter().map(|(id, _)| id.clone()).collect();

    // Source 3: Graph boost (Neo4j callers + usages of query as symbol name)
    let graph_ids = graph_boost::graph_boost(ctx, query);

    // Source 4: Graph expand — seed from top FTS+semantic results, expand neighborhood
    let seed_names = extract_seed_names(&fts_results, &semantic_ids, &conn, 5);
    let expand_ids = graph_boost::graph_expand(ctx, seed_names);

    // Build RRF sources (only include non-empty sources)
    let mut sources: Vec<(&str, Vec<String>)> = vec![("fts", fts_ids)];
    if !semantic_ids.is_empty() {
        sources.push(("semantic", semantic_ids));
    }
    if !graph_ids.is_empty() {
        sources.push(("graph", graph_ids));
    }
    if !expand_ids.is_empty() {
        sources.push(("graph_expand", expand_ids));
    }

    let merged = rrf::merge(sources);

    // Build symbol cache from FTS results
    let mut symbol_cache: HashMap<String, Symbol> = HashMap::new();
    for sym in fts_results {
        symbol_cache.insert(sym.id.clone(), sym);
    }

    // Resolve ALL results first so total reflects resolvable symbols only
    let mut all_resolved: Vec<SearchResult> = Vec::new();
    for (sym_id, score, source_names) in &merged {
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
            all_resolved.push(result);
        }
    }

    let total = all_resolved.len();
    let results: Vec<_> = all_resolved.into_iter().skip(offset).take(limit).collect();

    if results.is_empty() && offset == 0 && !crate::project::has_identity_file(&ctx.project_root) {
        eprintln!("No index found for this project. Run `gcode index` first.");
    } else if results.is_empty() && offset > 0 {
        eprintln!("No results at offset {offset} (total {total})");
    }

    match format {
        Format::Json => output::print_json(&PagedResponse {
            project_id: ctx.project_id.clone(),
            total,
            offset,
            limit,
            results,
            hint: None,
        }),
        Format::Text => {
            for r in &results {
                let sources = r.sources.as_ref().map(|s| s.join("+")).unwrap_or_default();
                println!(
                    "{}:{} [{}] {} (score: {:.4}, via: {})",
                    r.file_path, r.line_start, r.kind, r.qualified_name, r.score, sources
                );
            }
            if total > offset + results.len() {
                eprintln!(
                    "-- {} of {} results (use --offset {} for more)",
                    results.len(),
                    total,
                    offset + results.len()
                );
            }
            Ok(())
        }
    }
}

pub fn search_text(
    ctx: &Context,
    query: &str,
    limit: usize,
    offset: usize,
    format: Format,
) -> anyhow::Result<()> {
    let conn = db::open_readonly(&ctx.db_path)?;
    let fetch_limit = offset + limit;
    let all_results = fts::search_text(&conn, query, &ctx.project_id, fetch_limit);
    let total = fts::count_text(&conn, query, &ctx.project_id);
    let results: Vec<_> = all_results.into_iter().skip(offset).take(limit).collect();

    if results.is_empty() && offset == 0 && !crate::project::has_identity_file(&ctx.project_root) {
        eprintln!("No index found for this project. Run `gcode index` first.");
    } else if results.is_empty() && offset > 0 {
        eprintln!("No results at offset {offset} (total {total})");
    }

    match format {
        Format::Json => output::print_json(&PagedResponse {
            project_id: ctx.project_id.clone(),
            total,
            offset,
            limit,
            results,
            hint: None,
        }),
        Format::Text => {
            for r in &results {
                println!(
                    "{}:{} [{}] {}",
                    r.file_path, r.line_start, r.kind, r.qualified_name
                );
            }
            if total > offset + results.len() {
                eprintln!(
                    "-- {} of {} results (use --offset {} for more)",
                    results.len(),
                    total,
                    offset + results.len()
                );
            }
            Ok(())
        }
    }
}

/// Extract unique symbol names from the top FTS and semantic results for graph expansion.
fn extract_seed_names(
    fts_results: &[Symbol],
    semantic_ids: &[String],
    conn: &Connection,
    per_source: usize,
) -> Vec<String> {
    let mut names = Vec::new();
    let mut seen = HashSet::new();

    // Top N from FTS (already have Symbol structs with names)
    for sym in fts_results.iter().take(per_source) {
        if !sym.name.is_empty() && seen.insert(sym.name.clone()) {
            names.push(sym.name.clone());
        }
    }

    // Top N from semantic (need to resolve IDs to names via DB)
    let sem_top: Vec<&String> = semantic_ids.iter().take(per_source).collect();
    if !sem_top.is_empty() {
        let placeholders: Vec<&str> = sem_top.iter().map(|_| "?").collect();
        let sql = format!(
            "SELECT name FROM code_symbols WHERE id IN ({})",
            placeholders.join(", ")
        );
        if let Ok(mut stmt) = conn.prepare(&sql) {
            let params: Vec<&dyn rusqlite::types::ToSql> = sem_top
                .iter()
                .map(|id| id as &dyn rusqlite::types::ToSql)
                .collect();
            if let Ok(rows) = stmt.query_map(params.as_slice(), |row| row.get::<_, String>(0)) {
                for name in rows.flatten() {
                    if !name.is_empty() && seen.insert(name.clone()) {
                        names.push(name);
                    }
                }
            }
        }
    }

    names
}

pub fn search_content(
    ctx: &Context,
    query: &str,
    limit: usize,
    offset: usize,
    format: Format,
) -> anyhow::Result<()> {
    let conn = db::open_readonly(&ctx.db_path)?;
    let fetch_limit = offset + limit;
    let all_results = fts::search_content(&conn, query, &ctx.project_id, fetch_limit);
    let total = fts::count_content(&conn, query, &ctx.project_id);
    let results: Vec<_> = all_results.into_iter().skip(offset).take(limit).collect();

    if results.is_empty() && offset == 0 && !crate::project::has_identity_file(&ctx.project_root) {
        eprintln!("No index found for this project. Run `gcode index` first.");
    } else if results.is_empty() && offset > 0 {
        eprintln!("No results at offset {offset} (total {total})");
    }

    match format {
        Format::Json => output::print_json(&PagedResponse {
            project_id: ctx.project_id.clone(),
            total,
            offset,
            limit,
            results,
            hint: None,
        }),
        Format::Text => {
            for r in &results {
                println!(
                    "{}:{}-{} {}",
                    r.file_path, r.line_start, r.line_end, r.snippet
                );
            }
            if total > offset + results.len() {
                eprintln!(
                    "-- {} of {} results (use --offset {} for more)",
                    results.len(),
                    total,
                    offset + results.len()
                );
            }
            Ok(())
        }
    }
}
