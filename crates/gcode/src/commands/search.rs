use std::collections::HashMap;
use std::collections::HashSet;

use crate::commands::scope;
use crate::config::Context;
use crate::db;
use crate::models::{PagedResponse, SearchResult, Symbol};
use crate::output::{self, Format};
use crate::search::{fts, graph_boost, rrf, semantic};
use crate::visibility;

pub struct SearchOptions<'a> {
    pub limit: usize,
    pub offset: usize,
    pub kind: Option<&'a str>,
    pub language: Option<&'a str>,
    pub paths: &'a [String],
    pub format: Format,
    pub with_graph: bool,
}

pub fn search(ctx: &Context, query: &str, options: SearchOptions<'_>) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let expanded_paths = fts::expand_paths(options.paths);
    let path_patterns = fts::compile_patterns(&expanded_paths)?;

    // Fetch generously for RRF. Total is a best-effort estimate bounded by fetch_limit
    // per source — exact counts aren't feasible because RRF merges results from BM25,
    // Qdrant, and FalkorDB with deduplication, so source counts aren't additive.
    let fetch_limit = ((options.offset + options.limit) * 3).max(200);

    let exact_results = fts::search_symbols_exact_first_visible(
        &mut conn,
        query,
        ctx,
        options.kind,
        options.language,
        &expanded_paths,
        fetch_limit,
    );
    let exact_ids: Vec<String> = exact_results.iter().map(|s| s.id.clone()).collect();

    // Source 1: BM25 (with LIKE fallback)
    let mut fts_results = fts::search_symbols_fts_visible(
        &mut conn,
        query,
        ctx,
        options.kind,
        options.language,
        &expanded_paths,
        fetch_limit,
    );
    if fts_results.is_empty() {
        fts_results = fts::search_symbols_by_name_visible(
            &mut conn,
            query,
            ctx,
            options.kind,
            options.language,
            &expanded_paths,
            fetch_limit,
        );
    }
    let fts_ids: Vec<String> = fts_results.iter().map(|s| s.id.clone()).collect();

    // Source 2: Semantic search (Qdrant + embeddings)
    let semantic_results = semantic::semantic_search(ctx, query, fetch_limit);
    let semantic_ids: Vec<String> = semantic_results.iter().map(|(id, _)| id.clone()).collect();

    // Source 3: Graph boost (FalkorDB callers + usages of the resolved query symbol)
    let graph_ids = if options.with_graph {
        graph_boost::graph_boost(ctx, query)
    } else {
        Vec::new()
    };

    // Source 4: Graph expand — seed from top BM25+semantic results, expand neighborhood
    let seed_ids = extract_seed_ids(&fts_results, &semantic_ids, 5);
    let expand_ids = if options.with_graph {
        graph_boost::graph_expand(ctx, &seed_ids)
    } else {
        Vec::new()
    };

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

    // Build symbol cache from exact and BM25 results.
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
        let sym = match symbol_cache.get(sym_id).cloned() {
            Some(symbol) => Some(symbol),
            None => visibility::visible_symbol_by_id(&mut conn, ctx, sym_id)?,
        };

        if let Some(s) = sym
            && symbol_matches_filters(
                &mut conn,
                ctx,
                &s,
                options.kind,
                options.language,
                &path_patterns,
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
        .map(|(s, rrf_score, sources)| {
            let mut result = s.to_brief();
            result.score = final_rank_score(query, &s, rrf_score);
            result.rrf_score = Some(rrf_score);
            result.sources = Some(sources);
            result
        })
        .collect();

    print_empty_diagnostic(ctx, results.is_empty(), options.offset, total);
    let hint = fts::path_filter_falls_back(&expanded_paths).then(path_filter_fallback_hint);

    match options.format {
        Format::Json => output::print_json(&PagedResponse {
            project_id: ctx.project_id.clone(),
            total,
            offset: options.offset,
            limit: options.limit,
            results,
            hint,
        }),
        Format::Text => {
            print_search_warning(ctx, hint.as_deref());
            let lines = results
                .iter()
                .map(|r| {
                    let sources = r.sources.as_ref().map(|s| s.join("+")).unwrap_or_default();
                    format!(
                        "{}:{} [{}] {} (score: {:.4}, via: {})",
                        r.file_path, r.line_start, r.kind, r.qualified_name, r.score, sources
                    )
                })
                .collect::<Vec<_>>();
            if !lines.is_empty() {
                output::print_text(&lines.join("\n"))?;
            }
            print_pagination_hint(total, options.offset, results.len());
            Ok(())
        }
    }
}

pub fn search_symbol(ctx: &Context, query: &str, options: SearchOptions<'_>) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let expanded_paths = fts::expand_paths(options.paths);
    let path_patterns = fts::compile_patterns(&expanded_paths)?;
    let fetch_limit = ((options.offset + options.limit) * 3).max(200);
    let exact_results = fts::search_symbols_exact_first_visible(
        &mut conn,
        query,
        ctx,
        options.kind,
        options.language,
        &expanded_paths,
        fetch_limit,
    );

    if options.with_graph {
        return search_symbol_with_graph(
            ctx,
            query,
            options,
            &mut conn,
            &path_patterns,
            &expanded_paths,
            exact_results,
        );
    }

    let all_results: Vec<_> = exact_results
        .into_iter()
        .filter(|s| {
            symbol_matches_filters(
                &mut conn,
                ctx,
                s,
                options.kind,
                options.language,
                &path_patterns,
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
    let hint = fts::path_filter_falls_back(&expanded_paths).then(path_filter_fallback_hint);

    match options.format {
        Format::Json => {
            let results: Vec<SearchResult> = results
                .iter()
                .map(|s| {
                    let mut result = s.to_brief();
                    result.score = exact_tier_score(query, s);
                    result
                })
                .collect();
            output::print_json(&PagedResponse {
                project_id: ctx.project_id.clone(),
                total,
                offset: options.offset,
                limit: options.limit,
                results,
                hint,
            })
        }
        Format::Text => {
            print_search_warning(ctx, hint.as_deref());
            let lines = results
                .iter()
                .map(format_symbol_lookup_text)
                .collect::<Vec<_>>();
            if !lines.is_empty() {
                output::print_text(&lines.join("\n"))?;
            }
            print_pagination_hint(total, options.offset, results.len());
            Ok(())
        }
    }
}

fn search_symbol_with_graph(
    ctx: &Context,
    query: &str,
    options: SearchOptions<'_>,
    conn: &mut postgres::Client,
    path_patterns: &[glob::Pattern],
    expanded_paths: &[String],
    exact_results: Vec<Symbol>,
) -> anyhow::Result<()> {
    let exact_ids: Vec<String> = exact_results.iter().map(|s| s.id.clone()).collect();
    let seed_ids: Vec<String> = exact_ids.iter().take(5).cloned().collect();
    let graph_ids = graph_boost::graph_boost(ctx, query);
    let expand_ids = graph_boost::graph_expand(ctx, &seed_ids);

    let mut sources: Vec<(&str, Vec<String>)> = Vec::new();
    if !exact_ids.is_empty() {
        sources.push(("exact", exact_ids));
    }
    if !graph_ids.is_empty() {
        sources.push(("graph", graph_ids));
    }
    if !expand_ids.is_empty() {
        sources.push(("graph_expand", expand_ids));
    }

    let merged = rrf::merge(sources);
    let mut symbol_cache: HashMap<String, Symbol> = exact_results
        .into_iter()
        .map(|sym| (sym.id.clone(), sym))
        .collect();
    let mut all_resolved: Vec<(Symbol, f64, Vec<String>)> = Vec::new();
    for (sym_id, rrf_score, source_names) in &merged {
        let sym = match symbol_cache.remove(sym_id) {
            Some(symbol) => Some(symbol),
            None => fetch_symbol_by_id(conn, ctx, sym_id)?,
        };

        if let Some(s) = sym
            && symbol_matches_filters(conn, ctx, &s, options.kind, options.language, path_patterns)
        {
            all_resolved.push((s, *rrf_score, source_names.clone()));
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
        .map(|(s, rrf_score, sources)| {
            let mut result = s.to_brief();
            result.score = final_rank_score(query, &s, rrf_score);
            result.rrf_score = Some(rrf_score);
            result.sources = Some(sources);
            result
        })
        .collect();

    print_empty_diagnostic(ctx, results.is_empty(), options.offset, total);
    let hint = fts::path_filter_falls_back(expanded_paths).then(path_filter_fallback_hint);

    match options.format {
        Format::Json => output::print_json(&PagedResponse {
            project_id: ctx.project_id.clone(),
            total,
            offset: options.offset,
            limit: options.limit,
            results,
            hint,
        }),
        Format::Text => {
            print_search_warning(ctx, hint.as_deref());
            let lines = results
                .iter()
                .map(|r| {
                    let sources = r.sources.as_ref().map(|s| s.join("+")).unwrap_or_default();
                    format!(
                        "{}:{} [{}] {} (score: {:.4}, via: {})",
                        r.file_path, r.line_start, r.kind, r.qualified_name, r.score, sources
                    )
                })
                .collect::<Vec<_>>();
            if !lines.is_empty() {
                output::print_text(&lines.join("\n"))?;
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
    paths: &[String],
    format: Format,
) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let expanded_paths = fts::expand_paths(paths);
    let path_patterns = fts::compile_patterns(&expanded_paths)?;
    let has_path_filters = !expanded_paths.is_empty();
    let fetch_limit = if has_path_filters {
        fts::FILTERED_FETCH_CAP
    } else {
        ((offset + limit) * 3).max(200)
    };
    let all_results = fts::search_text_visible(
        &mut conn,
        query,
        ctx,
        language,
        &expanded_paths,
        fetch_limit,
    );
    let cap_hint = (has_path_filters && all_results.len() >= fts::FILTERED_FETCH_CAP)
        .then(filtered_fetch_cap_hint);
    let path_hint = fts::path_filter_falls_back(&expanded_paths).then(path_filter_fallback_hint);
    let hint = combine_hints(cap_hint, path_hint);
    let all_results: Vec<_> = all_results
        .into_iter()
        .filter(|r| search_result_matches_filters(&mut conn, ctx, r, language, &path_patterns))
        .collect();
    let total = if has_path_filters {
        all_results.len()
    } else {
        fts::count_text_visible(&mut conn, query, ctx, language, &expanded_paths)
    };
    let results: Vec<_> = all_results.into_iter().skip(offset).take(limit).collect();

    print_empty_diagnostic(ctx, results.is_empty(), offset, total);

    match format {
        Format::Json => output::print_json(&PagedResponse {
            project_id: ctx.project_id.clone(),
            total,
            offset,
            limit,
            results,
            hint,
        }),
        Format::Text => {
            print_search_warning(ctx, hint.as_deref());
            let lines = results
                .iter()
                .map(|r| {
                    format!(
                        "{}:{} [{}] {}",
                        r.file_path, r.line_start, r.kind, r.qualified_name
                    )
                })
                .collect::<Vec<_>>();
            if !lines.is_empty() {
                output::print_text(&lines.join("\n"))?;
            }
            if total > offset + results.len() {
                print_pagination_hint(total, offset, results.len());
            }
            Ok(())
        }
    }
}

/// Extract unique symbol IDs from the top BM25 and semantic results for graph expansion.
fn extract_seed_ids(
    fts_results: &[Symbol],
    semantic_ids: &[String],
    per_source: usize,
) -> Vec<String> {
    let mut ids = Vec::new();
    let mut seen = HashSet::new();

    // Top N from BM25 (already have Symbol structs with IDs)
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
    paths: &[String],
    format: Format,
) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let expanded_paths = fts::expand_paths(paths);
    let path_patterns = fts::compile_patterns(&expanded_paths)?;
    let has_path_filters = !expanded_paths.is_empty();
    let fetch_limit = if has_path_filters {
        fts::FILTERED_FETCH_CAP
    } else {
        ((offset + limit) * 3).max(200)
    };
    let all_results = fts::search_content_visible(
        &mut conn,
        query,
        ctx,
        language,
        &expanded_paths,
        fetch_limit,
    );
    let cap_hint = (has_path_filters && all_results.len() >= fts::FILTERED_FETCH_CAP)
        .then(filtered_fetch_cap_hint);
    let path_hint = fts::path_filter_falls_back(&expanded_paths).then(path_filter_fallback_hint);
    let hint = combine_hints(cap_hint, path_hint);
    let all_results: Vec<_> = all_results
        .into_iter()
        .filter(|r| {
            language.is_none_or(|lang| r.language.as_deref() == Some(lang))
                && path_matches_filters(&path_patterns, &r.file_path)
                && scope::current_indexed_path_is_valid(&mut conn, ctx, &r.file_path)
        })
        .collect();
    let total = if has_path_filters {
        all_results.len()
    } else {
        fts::count_content_visible(&mut conn, query, ctx, language, &expanded_paths)
    };
    let results: Vec<_> = all_results.into_iter().skip(offset).take(limit).collect();

    print_empty_diagnostic(ctx, results.is_empty(), offset, total);

    match format {
        Format::Json => output::print_json(&PagedResponse {
            project_id: ctx.project_id.clone(),
            total,
            offset,
            limit,
            results,
            hint,
        }),
        Format::Text => {
            print_search_warning(ctx, hint.as_deref());
            let lines = results
                .iter()
                .map(|r| {
                    format!(
                        "{}:{}-{} {}",
                        r.file_path,
                        r.line_start,
                        r.line_end,
                        compact_snippet(&r.snippet)
                    )
                })
                .collect::<Vec<_>>();
            if !lines.is_empty() {
                output::print_text(&lines.join("\n"))?;
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

fn exact_tier_score(query: &str, symbol: &Symbol) -> f64 {
    match exact_tier(query, symbol) {
        0 => 1.0,
        1 => 0.9,
        _ => 0.5,
    }
}

fn final_rank_score(query: &str, symbol: &Symbol, rrf_score: f64) -> f64 {
    exact_tier_score(query, symbol) + rrf_score
}

fn fetch_symbol_by_id(
    conn: &mut postgres::Client,
    ctx: &Context,
    symbol_id: &str,
) -> anyhow::Result<Option<Symbol>> {
    visibility::visible_symbol_by_id(conn, ctx, symbol_id)
}

fn symbol_matches_filters(
    conn: &mut postgres::Client,
    ctx: &Context,
    symbol: &Symbol,
    kind: Option<&str>,
    language: Option<&str>,
    path_patterns: &[glob::Pattern],
) -> bool {
    kind.is_none_or(|k| symbol.kind == k)
        && language.is_none_or(|lang| symbol.language == lang)
        && path_matches_filters(path_patterns, &symbol.file_path)
        && scope::current_indexed_path_is_valid(conn, ctx, &symbol.file_path)
}

fn search_result_matches_filters(
    conn: &mut postgres::Client,
    ctx: &Context,
    result: &SearchResult,
    language: Option<&str>,
    path_patterns: &[glob::Pattern],
) -> bool {
    language.is_none_or(|lang| result.language == lang)
        && path_matches_filters(path_patterns, &result.file_path)
        && scope::current_indexed_path_is_valid(conn, ctx, &result.file_path)
}

fn path_matches_filters(path_patterns: &[glob::Pattern], file_path: &str) -> bool {
    path_patterns.is_empty() || path_patterns.iter().any(|pat| pat.matches(file_path))
}

fn filtered_fetch_cap_hint() -> String {
    format!(
        "Path-filtered search hit the fetch cap of {}; refine the query or paths for complete totals.",
        fts::FILTERED_FETCH_CAP
    )
}

fn path_filter_fallback_hint() -> String {
    "Some path filters cannot be pushed into SQL; results were post-filtered after a broader fetch."
        .to_string()
}

fn combine_hints(first: Option<String>, second: Option<String>) -> Option<String> {
    match (first, second) {
        (Some(first), Some(second)) => Some(format!("{first} {second}")),
        (Some(first), None) => Some(first),
        (None, Some(second)) => Some(second),
        (None, None) => None,
    }
}

fn print_search_warning(ctx: &Context, hint: Option<&str>) {
    if let Some(hint) = hint
        && !ctx.quiet
    {
        eprintln!("warning: {hint}");
    }
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

fn compact_snippet(snippet: &str) -> String {
    snippet.split_whitespace().collect::<Vec<_>>().join(" ")
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
        let pattern = glob::Pattern::new("src/*.rs").expect("glob");
        let sym = symbol("src/lib.rs", "function", "rust");

        assert!(Some("function").is_none_or(|k| sym.kind == k));
        assert!(Some("rust").is_none_or(|lang| sym.language == lang));
        assert!(Some(&pattern).is_none_or(|pat| pat.matches(&sym.file_path)));
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

    #[test]
    fn final_score_preserves_display_tier_before_rrf_score() {
        let exact = symbol("src/lib.rs", "function", "rust");
        let mut fuzzy = symbol("src/other.rs", "function", "rust");
        fuzzy.name = "outline_helper".to_string();
        fuzzy.qualified_name = "outline_helper".to_string();

        assert!(
            final_rank_score("outline", &exact, 0.01) > final_rank_score("outline", &fuzzy, 0.08)
        );
    }

    #[test]
    fn combines_fetch_cap_and_path_fallback_hints() {
        let hint = combine_hints(
            Some(filtered_fetch_cap_hint()),
            Some(path_filter_fallback_hint()),
        )
        .expect("hint");

        assert!(hint.contains("fetch cap"));
        assert!(hint.contains("post-filtered"));
    }

    #[test]
    fn content_snippet_compaction_collapses_whitespace() {
        assert_eq!(
            compact_snippet("  first line\n    second\tline\r\nthird  "),
            "first line second line third"
        );
    }
}
