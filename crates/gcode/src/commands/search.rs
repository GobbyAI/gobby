use std::collections::HashMap;
use std::collections::HashSet;

use crate::commands::scope;
use crate::config::Context;
use crate::db;
use crate::models::{PagedResponse, SearchResult, Symbol};
use crate::output::{self, Format};
use crate::search::{fts, graph_boost, rrf, semantic};

pub struct SearchOptions<'a> {
    pub limit: usize,
    pub offset: usize,
    pub kind: Option<&'a str>,
    pub language: Option<&'a str>,
    pub path: Option<&'a str>,
    pub format: Format,
}

pub fn search(ctx: &Context, query: &str, options: SearchOptions<'_>) -> anyhow::Result<()> {
    let conn = db::open_readonly(&ctx.db_path)?;
    let path_pattern = options
        .path
        .map(glob::Pattern::new)
        .transpose()
        .map_err(|e| anyhow::anyhow!("invalid path glob: {e}"))?;

    // Fetch generously for RRF. Total is a best-effort estimate bounded by fetch_limit
    // per source — exact counts aren't feasible because RRF merges results from FTS5,
    // Qdrant, and Neo4j with deduplication, so source counts aren't additive.
    let fetch_limit = ((options.offset + options.limit) * 3).max(200);

    let exact_results = fts::search_symbols_exact_first(
        &conn,
        query,
        &ctx.project_id,
        options.kind,
        options.language,
        options.path,
        fetch_limit,
    );
    let exact_ids: Vec<String> = exact_results.iter().map(|s| s.id.clone()).collect();

    // Source 1: FTS5 (with LIKE fallback)
    let mut fts_results = fts::search_symbols_fts(
        &conn,
        query,
        &ctx.project_id,
        options.kind,
        options.language,
        options.path,
        fetch_limit,
    );
    if fts_results.is_empty() {
        fts_results = fts::search_symbols_by_name(
            &conn,
            query,
            &ctx.project_id,
            options.kind,
            options.language,
            options.path,
            fetch_limit,
        );
    }
    let fts_ids: Vec<String> = fts_results.iter().map(|s| s.id.clone()).collect();

    // Source 2: Semantic search (Qdrant + embeddings)
    let semantic_results = semantic::semantic_search(ctx, query, fetch_limit);
    let semantic_ids: Vec<String> = semantic_results.iter().map(|(id, _)| id.clone()).collect();

    // Source 3: Graph boost (Neo4j callers + usages of the resolved query symbol)
    let graph_ids = graph_boost::graph_boost(ctx, query);

    // Source 4: Graph expand — seed from top FTS+semantic results, expand neighborhood
    let seed_ids = extract_seed_ids(&fts_results, &semantic_ids, 5);
    let expand_ids = graph_boost::graph_expand(ctx, &seed_ids);

    // Build RRF sources (only include non-empty sources)
    let mut sources: Vec<(&str, Vec<String>)> = Vec::new();
    if !exact_ids.is_empty() {
        sources.push(("exact", exact_ids));
    }
    sources.push(("fts", fts_ids));
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
    for sym in exact_results {
        symbol_cache.insert(sym.id.clone(), sym);
    }
    for sym in fts_results {
        symbol_cache.insert(sym.id.clone(), sym);
    }

    // Resolve ALL results first so total reflects resolvable symbols only
    let mut all_resolved: Vec<(Symbol, f64, Vec<String>)> = Vec::new();
    for (sym_id, score, source_names) in &merged {
        let sym = symbol_cache.get(sym_id).cloned().or_else(|| {
            conn.query_row(
                "SELECT * FROM code_symbols WHERE id = ?1",
                rusqlite::params![sym_id],
                Symbol::from_row,
            )
            .ok()
        });

        if let Some(s) = sym
            && symbol_matches_filters(
                &conn,
                ctx,
                &s,
                options.kind,
                options.language,
                path_pattern.as_ref(),
            )
        {
            all_resolved.push((s, *score, source_names.clone()));
        }
    }

    all_resolved.sort_by(|a, b| {
        exact_tier(query, &a.0)
            .cmp(&exact_tier(query, &b.0))
            .then_with(|| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal))
            .then_with(|| a.0.file_path.cmp(&b.0.file_path))
            .then_with(|| a.0.line_start.cmp(&b.0.line_start))
    });

    let total = all_resolved.len();
    let results: Vec<_> = all_resolved
        .into_iter()
        .skip(options.offset)
        .take(options.limit)
        .map(|(s, score, sources)| {
            let mut result = s.to_brief();
            result.score = score;
            result.sources = Some(sources);
            result
        })
        .collect();

    print_empty_diagnostic(ctx, results.is_empty(), options.offset, total);

    match options.format {
        Format::Json => output::print_json(&PagedResponse {
            project_id: ctx.project_id.clone(),
            total,
            offset: options.offset,
            limit: options.limit,
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
            print_pagination_hint(total, options.offset, results.len());
            Ok(())
        }
    }
}

pub fn search_symbol(ctx: &Context, query: &str, options: SearchOptions<'_>) -> anyhow::Result<()> {
    let conn = db::open_readonly(&ctx.db_path)?;
    let path_pattern = options
        .path
        .map(glob::Pattern::new)
        .transpose()
        .map_err(|e| anyhow::anyhow!("invalid path glob: {e}"))?;
    let fetch_limit = ((options.offset + options.limit) * 3).max(200);
    let all_results: Vec<_> = fts::search_symbols_exact_first(
        &conn,
        query,
        &ctx.project_id,
        options.kind,
        options.language,
        options.path,
        fetch_limit,
    )
    .into_iter()
    .filter(|s| {
        symbol_matches_filters(
            &conn,
            ctx,
            s,
            options.kind,
            options.language,
            path_pattern.as_ref(),
        )
    })
    .collect();
    let total = all_results.len();
    let results: Vec<_> = all_results
        .into_iter()
        .skip(options.offset)
        .take(options.limit)
        .collect();

    print_empty_diagnostic(ctx, results.is_empty(), options.offset, total);

    match options.format {
        Format::Json => {
            let results: Vec<SearchResult> = results
                .iter()
                .map(|s| {
                    let mut result = s.to_brief();
                    result.score = match exact_tier(query, s) {
                        0 => 1.0,
                        1 => 0.9,
                        _ => 0.5,
                    };
                    result
                })
                .collect();
            output::print_json(&PagedResponse {
                project_id: ctx.project_id.clone(),
                total,
                offset: options.offset,
                limit: options.limit,
                results,
                hint: None,
            })
        }
        Format::Text => {
            for s in &results {
                println!("{}", format_symbol_lookup_text(s));
            }
            print_pagination_hint(total, options.offset, results.len());
            Ok(())
        }
    }
}

pub fn search_text(
    ctx: &Context,
    query: &str,
    limit: usize,
    offset: usize,
    language: Option<&str>,
    path: Option<&str>,
    format: Format,
) -> anyhow::Result<()> {
    let conn = db::open_readonly(&ctx.db_path)?;
    let path_pattern = path
        .map(glob::Pattern::new)
        .transpose()
        .map_err(|e| anyhow::anyhow!("invalid path glob: {e}"))?;
    let fetch_limit = ((offset + limit) * 3).max(200);
    let all_results = fts::search_text(&conn, query, &ctx.project_id, language, path, fetch_limit);
    let _raw_total = fts::count_text(&conn, query, &ctx.project_id, language, path);
    let all_results: Vec<_> = all_results
        .into_iter()
        .filter(|r| search_result_matches_filters(&conn, ctx, r, language, path_pattern.as_ref()))
        .collect();
    let total = all_results.len();
    let results: Vec<_> = all_results.into_iter().skip(offset).take(limit).collect();

    print_empty_diagnostic(ctx, results.is_empty(), offset, total);

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
                print_pagination_hint(total, offset, results.len());
            }
            Ok(())
        }
    }
}

/// Extract unique symbol IDs from the top FTS and semantic results for graph expansion.
fn extract_seed_ids(
    fts_results: &[Symbol],
    semantic_ids: &[String],
    per_source: usize,
) -> Vec<String> {
    let mut ids = Vec::new();
    let mut seen = HashSet::new();

    // Top N from FTS (already have Symbol structs with IDs)
    for sym in fts_results.iter().take(per_source) {
        if !sym.id.is_empty() && seen.insert(sym.id.clone()) {
            ids.push(sym.id.clone());
        }
    }

    // Top N from semantic (already canonical symbol IDs)
    for id in semantic_ids.iter().take(per_source) {
        if !id.is_empty() && seen.insert(id.clone()) {
            ids.push(id.clone());
        }
    }

    ids
}

pub fn search_content(
    ctx: &Context,
    query: &str,
    limit: usize,
    offset: usize,
    language: Option<&str>,
    path: Option<&str>,
    format: Format,
) -> anyhow::Result<()> {
    let conn = db::open_readonly(&ctx.db_path)?;
    let path_pattern = path
        .map(glob::Pattern::new)
        .transpose()
        .map_err(|e| anyhow::anyhow!("invalid path glob: {e}"))?;
    let fetch_limit = ((offset + limit) * 3).max(200);
    let all_results =
        fts::search_content(&conn, query, &ctx.project_id, language, path, fetch_limit);
    let _raw_total = fts::count_content(&conn, query, &ctx.project_id, language, path);
    let all_results: Vec<_> = all_results
        .into_iter()
        .filter(|r| {
            language.is_none_or(|lang| r.language.as_deref() == Some(lang))
                && path_pattern
                    .as_ref()
                    .is_none_or(|pat| pat.matches(&r.file_path))
                && scope::current_indexed_path_is_valid(&conn, ctx, &r.file_path)
        })
        .collect();
    let total = all_results.len();
    let results: Vec<_> = all_results.into_iter().skip(offset).take(limit).collect();

    print_empty_diagnostic(ctx, results.is_empty(), offset, total);

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
                print_pagination_hint(total, offset, results.len());
            }
            Ok(())
        }
    }
}

fn exact_tier(query: &str, symbol: &Symbol) -> u8 {
    if symbol.name == query || symbol.qualified_name == query {
        0
    } else if symbol.name.eq_ignore_ascii_case(query)
        || symbol.qualified_name.eq_ignore_ascii_case(query)
    {
        1
    } else {
        2
    }
}

fn symbol_matches_filters(
    conn: &rusqlite::Connection,
    ctx: &Context,
    symbol: &Symbol,
    kind: Option<&str>,
    language: Option<&str>,
    path_pattern: Option<&glob::Pattern>,
) -> bool {
    kind.is_none_or(|k| symbol.kind == k)
        && language.is_none_or(|lang| symbol.language == lang)
        && path_pattern.is_none_or(|pat| pat.matches(&symbol.file_path))
        && scope::current_indexed_path_is_valid(conn, ctx, &symbol.file_path)
}

fn search_result_matches_filters(
    conn: &rusqlite::Connection,
    ctx: &Context,
    result: &SearchResult,
    language: Option<&str>,
    path_pattern: Option<&glob::Pattern>,
) -> bool {
    language.is_none_or(|lang| result.language == lang)
        && path_pattern.is_none_or(|pat| pat.matches(&result.file_path))
        && scope::current_indexed_path_is_valid(conn, ctx, &result.file_path)
}

fn format_symbol_lookup_text(symbol: &Symbol) -> String {
    let mut line = format!(
        "{}:{}-{} [{}] {} id={}",
        symbol.file_path,
        symbol.line_start,
        symbol.line_end,
        symbol.kind,
        symbol.qualified_name,
        symbol.id
    );
    if let Some(sig) = symbol.signature.as_deref().filter(|sig| !sig.is_empty()) {
        line.push_str(" sig=");
        line.push_str(sig);
    }
    line
}

fn print_empty_diagnostic(ctx: &Context, is_empty: bool, offset: usize, total: usize) {
    if !is_empty || ctx.quiet {
        return;
    }
    if offset == 0 && !crate::project::has_identity_file(&ctx.project_root) {
        eprintln!("No index found for this project. Run `gcode index` first.");
    } else if offset > 0 {
        eprintln!("No results at offset {offset} (total {total})");
    } else {
        eprintln!("No results.");
    }
}

fn print_pagination_hint(total: usize, offset: usize, result_count: usize) {
    if total > offset + result_count {
        eprintln!(
            "-- {} of {} results (use --offset {} for more)",
            result_count,
            total,
            offset + result_count
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn context_for(root: &std::path::Path) -> Context {
        Context {
            db_path: root.join("index.db"),
            project_root: root.to_path_buf(),
            project_id: "proj".to_string(),
            quiet: false,
            neo4j: None,
            qdrant: None,
            embedding: None,
            daemon_url: None,
        }
    }

    fn setup_conn() -> rusqlite::Connection {
        let conn = rusqlite::Connection::open_in_memory().expect("open sqlite");
        conn.execute_batch(
            "CREATE TABLE code_indexed_files (
                project_id TEXT NOT NULL,
                file_path TEXT NOT NULL
            );",
        )
        .expect("create schema");
        conn
    }

    fn symbol(file_path: &str, kind: &str, language: &str) -> Symbol {
        Symbol {
            id: "sym-1".to_string(),
            project_id: "proj".to_string(),
            file_path: file_path.to_string(),
            name: "outline".to_string(),
            qualified_name: "outline".to_string(),
            kind: kind.to_string(),
            language: language.to_string(),
            byte_start: 0,
            byte_end: 10,
            line_start: 1,
            line_end: 2,
            signature: None,
            docstring: None,
            parent_symbol_id: None,
            content_hash: String::new(),
            summary: None,
            created_at: String::new(),
            updated_at: String::new(),
        }
    }

    #[test]
    fn symbol_filter_rejects_language_kind_path_and_missing_disk_file() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let src = tmp.path().join("src");
        std::fs::create_dir_all(&src).expect("create src");
        std::fs::write(src.join("lib.rs"), "fn outline() {}").expect("write file");
        let ctx = context_for(tmp.path());
        let conn = setup_conn();
        conn.execute(
            "INSERT INTO code_indexed_files (project_id, file_path) VALUES ('proj', 'src/lib.rs')",
            [],
        )
        .expect("insert indexed file");
        let pattern = glob::Pattern::new("src/*.rs").expect("glob");

        assert!(symbol_matches_filters(
            &conn,
            &ctx,
            &symbol("src/lib.rs", "function", "rust"),
            Some("function"),
            Some("rust"),
            Some(&pattern),
        ));
        assert!(!symbol_matches_filters(
            &conn,
            &ctx,
            &symbol("src/lib.rs", "class", "rust"),
            Some("function"),
            Some("rust"),
            Some(&pattern),
        ));
        assert!(!symbol_matches_filters(
            &conn,
            &ctx,
            &symbol("src/missing.rs", "function", "rust"),
            Some("function"),
            Some("rust"),
            Some(&pattern),
        ));
    }

    #[test]
    fn exact_tier_prefers_case_sensitive_match() {
        assert_eq!(
            exact_tier("outline", &symbol("src/lib.rs", "function", "rust")),
            0
        );

        let mut case_variant = symbol("src/lib.rs", "function", "rust");
        case_variant.name = "Outline".to_string();
        case_variant.qualified_name = "Outline".to_string();
        assert_eq!(exact_tier("outline", &case_variant), 1);

        case_variant.name = "outline_helper".to_string();
        case_variant.qualified_name = "outline_helper".to_string();
        assert_eq!(exact_tier("outline", &case_variant), 2);
    }
}
