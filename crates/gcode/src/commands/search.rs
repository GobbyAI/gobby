use std::collections::{BTreeSet, HashMap, HashSet};
use std::ffi::OsStr;
use std::path::Path;

pub(crate) mod content_page;
pub(crate) mod output;
mod scoped_fetch;

pub use scoped_fetch::{search_content, search_symbol, search_text};

use crate::commands::{indexed_paths, scope, token_budget};
use crate::config::Context;
use crate::db;
use crate::models::{SearchWarning, SearchWarningCause, SearchWarningLane, Symbol};
use crate::output::Format;
use crate::search::{fts, graph_boost, rrf};
use crate::vector::code_symbols;
use crate::visibility;

pub struct SearchOptions<'a> {
    pub limit: usize,
    pub offset: usize,
    pub kind: Option<&'a str>,
    pub language: Option<&'a str>,
    pub paths: &'a [String],
    pub format: Format,
    pub with_graph: bool,
    pub token_budget: Option<usize>,
    pub verbose: bool,
}

pub struct TextSearchOptions<'a> {
    pub limit: usize,
    pub offset: usize,
    pub language: Option<&'a str>,
    pub paths: &'a [String],
    pub format: Format,
    pub token_budget: Option<usize>,
    pub verbose: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub(crate) enum SemanticLane {
    Hits(Vec<(String, f64)>),
    Degraded(SearchWarning),
}

#[derive(Debug, Clone)]
pub(crate) struct HybridSources {
    pub sources: Vec<(&'static str, Vec<String>)>,
    pub warnings: Vec<SearchWarning>,
}

pub(crate) fn semantic_lane_from_grant_outage() -> SemanticLane {
    SemanticLane::Degraded(SearchWarning {
        lane: SearchWarningLane::Semantic,
        cause: SearchWarningCause::DaemonUnreachable,
        message: "semantic search degraded: daemon unreachable; lexical and graph results only"
            .to_string(),
    })
}

pub(crate) fn assemble_hybrid_sources(
    exact: Vec<String>,
    fts: Vec<String>,
    semantic: SemanticLane,
    graph: Vec<String>,
    expand: Vec<String>,
) -> HybridSources {
    let mut sources: Vec<(&'static str, Vec<String>)> = Vec::new();
    let mut warnings = Vec::new();
    if !exact.is_empty() {
        sources.push(("exact", exact));
    }
    sources.push(("fts", fts));
    match semantic {
        SemanticLane::Hits(hits) if !hits.is_empty() => {
            sources.push(("semantic", hits.into_iter().map(|(id, _)| id).collect()));
        }
        SemanticLane::Hits(_) => {}
        SemanticLane::Degraded(warning) => warnings.push(warning),
    }
    if !graph.is_empty() {
        sources.push(("graph", graph));
    }
    if !expand.is_empty() {
        sources.push(("graph_expand", expand));
    }
    HybridSources { sources, warnings }
}

fn semantic_lane(ctx: &Context, query: &str, fetch_limit: usize) -> SemanticLane {
    if ctx
        .grant_ai
        .as_ref()
        .is_some_and(|grant| grant.unexpired && !grant.daemon_reachable)
    {
        return semantic_lane_from_grant_outage();
    }
    SemanticLane::Hits(code_symbols::semantic_search(ctx, query, fetch_limit))
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
    )?;
    let exact_ids: Vec<String> = exact_results.iter().map(|s| s.id.clone()).collect();

    // Source 1: BM25 via required pg_search indexes.
    let mut fts_results = fts::search_symbols_fts_visible(
        &mut conn,
        query,
        ctx,
        options.kind,
        options.language,
        &expanded_paths,
        fetch_limit,
    )?;
    if fts_results.is_empty() {
        fts_results = fts::search_symbols_by_name_visible(
            &mut conn,
            query,
            ctx,
            options.kind,
            options.language,
            &expanded_paths,
            fetch_limit,
        )?;
    }
    let fts_ids: Vec<String> = fts_results.iter().map(|s| s.id.clone()).collect();

    // Source 2: Semantic search (Qdrant + embeddings)
    let semantic = semantic_lane(ctx, query, fetch_limit);
    let semantic_ids: Vec<String> = match &semantic {
        SemanticLane::Hits(hits) => hits.iter().map(|(id, _)| id.clone()).collect(),
        SemanticLane::Degraded(_) => Vec::new(),
    };

    // Source 3: Graph boost (FalkorDB callers + usages of the resolved query symbol)
    let graph_ids = if options.with_graph {
        graph_boost::graph_boost(ctx, Some(&mut conn), query)
    } else {
        Vec::new()
    };

    // Source 4: Graph expand — seed from top BM25+semantic results, expand neighborhood
    let seed_ids = extract_seed_ids(&fts_results, &semantic_ids, 5);
    let expand_ids = if options.with_graph {
        graph_boost::graph_expand(ctx, Some(&mut conn), &seed_ids)
    } else {
        Vec::new()
    };

    let assembled = assemble_hybrid_sources(exact_ids, fts_ids, semantic, graph_ids, expand_ids);
    let merged = rrf::merge(assembled.sources);

    // Build symbol cache from exact and BM25 results.
    let mut symbol_cache: HashMap<String, Symbol> = HashMap::new();
    for sym in exact_results {
        symbol_cache.insert(sym.id.clone(), sym);
    }
    for sym in fts_results {
        symbol_cache.insert(sym.id.clone(), sym);
    }

    let missing_ids = merged
        .iter()
        .map(|(symbol_id, _, _)| symbol_id.clone())
        .filter(|symbol_id| !symbol_cache.contains_key(symbol_id))
        .collect::<Vec<_>>();
    for symbol in visibility::visible_symbols_by_ids(&mut conn, ctx, &missing_ids)? {
        symbol_cache.insert(symbol.id.clone(), symbol);
    }

    // Resolve ALL results first so total reflects resolvable symbols only
    let mut all_resolved = resolve_hybrid_symbols(
        &mut conn,
        ctx,
        &merged,
        &symbol_cache,
        options.kind,
        options.language,
        &path_patterns,
    );

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
    let is_empty = results.is_empty();
    let lane_hint = search_lane_hint(
        query,
        options.paths,
        options.kind,
        options.language,
        is_empty,
        true,
    );
    let path_hint =
        fts::path_filter_requires_post_filter(&expanded_paths).then(path_filter_post_filter_hint);
    let hint = token_budget::combine_hints(lane_hint, path_hint);
    output::print_empty_diagnostic(ctx, is_empty, options.offset, total, hint.as_deref());
    let meta = output::SearchPageMeta {
        total,
        offset: options.offset,
        limit: options.limit,
        hint: hint.as_deref(),
        warnings: &assembled.warnings,
    };
    let page = output::paginate(
        ctx,
        results,
        meta,
        options.token_budget,
        options.format,
        |rows| output::render_search_results(rows, options.verbose),
    );

    if matches!(options.format, Format::Text) {
        for warning in &assembled.warnings {
            output::print_search_warning(ctx, options.verbose, Some(&warning.message));
        }
        output::print_search_hint(ctx, is_empty, hint.as_deref());
    }
    output::print_page(ctx, &page, meta, options.format, |rows| {
        output::render_search_results(rows, options.verbose)
    })
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

fn symbol_matches_local_filters(
    ctx: &Context,
    symbol: &Symbol,
    kind: Option<&str>,
    language: Option<&str>,
    path_patterns: &[glob::Pattern],
) -> bool {
    kind.is_none_or(|k| symbol.kind == k)
        && language.is_none_or(|lang| symbol.language == lang)
        && path_matches_filters(path_patterns, &symbol.file_path)
        && scope::path_exists_in_current_project(ctx, &symbol.file_path)
}

/// Resolves graph-merged symbol IDs to visible symbols in merged order. IDs held
/// in `symbol_cache` are used as-is; all remaining misses share one visibility lookup.
fn resolve_graph_symbols(
    conn: &mut postgres::Client,
    ctx: &Context,
    merged: &[(String, f64, Vec<String>)],
    mut symbol_cache: HashMap<String, Symbol>,
    kind: Option<&str>,
    language: Option<&str>,
    path_patterns: &[glob::Pattern],
) -> anyhow::Result<Vec<(Symbol, f64, Vec<String>)>> {
    let misses: Vec<String> = merged
        .iter()
        .filter(|(sym_id, _, _)| !symbol_cache.contains_key(sym_id))
        .map(|(sym_id, _, _)| sym_id.clone())
        .collect();
    symbol_cache.extend(
        visibility::visible_symbols_by_ids(conn, ctx, &misses)?
            .into_iter()
            .map(|sym| (sym.id.clone(), sym)),
    );
    let mut resolved = Vec::new();
    for (sym_id, rrf_score, source_names) in merged {
        let Some(s) = symbol_cache.remove(sym_id) else {
            continue;
        };
        if symbol_matches_local_filters(ctx, &s, kind, language, path_patterns) {
            resolved.push((s, *rrf_score, source_names.clone()));
        }
    }
    Ok(resolved)
}

fn resolve_hybrid_symbols(
    conn: &mut postgres::Client,
    ctx: &Context,
    merged: &[(String, f64, Vec<String>)],
    symbol_cache: &HashMap<String, Symbol>,
    kind: Option<&str>,
    language: Option<&str>,
    path_patterns: &[glob::Pattern],
) -> Vec<(Symbol, f64, Vec<String>)> {
    let resolved: Vec<_> = merged
        .iter()
        .filter_map(|(symbol_id, score, sources)| {
            let symbol = symbol_cache.get(symbol_id)?.clone();
            symbol_matches_local_filters(ctx, &symbol, kind, language, path_patterns)
                .then(|| (symbol, *score, sources.clone()))
        })
        .collect();
    retain_indexed_results(conn, ctx, resolved, |r| r.0.file_path.as_str())
}

fn retain_indexed_results<T>(
    conn: &mut postgres::Client,
    ctx: &Context,
    results: Vec<T>,
    file_path: impl Fn(&T) -> &str,
) -> Vec<T> {
    let file_paths: BTreeSet<String> = results.iter().map(|r| file_path(r).to_string()).collect();
    let valid = indexed_paths::valid_indexed_paths(conn, ctx, &file_paths);
    results
        .into_iter()
        .filter(|r| valid.contains(file_path(r)))
        .collect()
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

fn path_filter_post_filter_hint() -> String {
    "Some path filters cannot be pushed into SQL; results were post-filtered after a broader fetch."
        .to_string()
}

fn search_lane_hint(
    query: &str,
    paths: &[String],
    kind: Option<&str>,
    language: Option<&str>,
    results_empty: bool,
    recommend_search_symbol: bool,
) -> Option<String> {
    let mut hints = Vec::new();

    if snake_case_identifier(query) {
        let grep = grep_command("-w", query, paths);
        if recommend_search_symbol {
            let exact = ranked_search_command("search-symbol", query, paths, kind, language);
            hints.push(format!(
                "For a known symbol, use `{exact}`; for exact identifier occurrences, use `{grep}`."
            ));
        } else {
            hints.push(format!("For exact identifier occurrences, use `{grep}`."));
        }
    } else if literal_like_query(query) {
        let grep = grep_command("-F", query, paths);
        hints.push(format!("For exact literal text, use `{grep}`."));
    }

    if results_empty || content_only_path_filters(paths) {
        let content = ranked_search_command("search-content", query, paths, None, language);
        hints.push(format!(
            "For repository text, docs, or config, use `{content}`."
        ));
    }

    (!hints.is_empty()).then(|| hints.join(" "))
}

fn ranked_search_command(
    subcommand: &str,
    query: &str,
    paths: &[String],
    kind: Option<&str>,
    language: Option<&str>,
) -> String {
    let mut parts = vec![
        "gcode".to_string(),
        subcommand.to_string(),
        token_budget::shell_quote(OsStr::new(query)),
    ];
    parts.extend(
        paths
            .iter()
            .map(|path| token_budget::shell_quote(OsStr::new(path))),
    );
    if let Some(kind) = kind {
        parts.extend([
            "--kind".to_string(),
            token_budget::shell_quote(OsStr::new(kind)),
        ]);
    }
    if let Some(language) = language {
        parts.extend([
            "--language".to_string(),
            token_budget::shell_quote(OsStr::new(language)),
        ]);
    }
    parts.join(" ")
}

fn grep_command(flag: &str, query: &str, paths: &[String]) -> String {
    let mut parts = vec![
        "gcode".to_string(),
        "grep".to_string(),
        flag.to_string(),
        token_budget::shell_quote(OsStr::new(query)),
    ];
    parts.extend(
        paths
            .iter()
            .map(|path| token_budget::shell_quote(OsStr::new(path))),
    );
    parts.extend(["-m".to_string(), "50".to_string()]);
    parts.join(" ")
}

fn snake_case_identifier(query: &str) -> bool {
    let query = query.trim();
    query.contains('_')
        && query.bytes().any(|byte| byte.is_ascii_alphabetic())
        && query
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
}

fn content_only_path_filters(paths: &[String]) -> bool {
    paths.iter().any(|path| {
        let trimmed = path.trim().trim_end_matches('/');
        let path = Path::new(trimmed);
        let file_name = path
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("");
        let looks_like_file = path.extension().is_some()
            || matches!(
                file_name.to_ascii_lowercase().as_str(),
                ".dockerignore"
                    | ".editorconfig"
                    | ".gitignore"
                    | ".npmrc"
                    | ".prettierrc"
                    | "dockerfile"
                    | "gemfile"
                    | "makefile"
                    | "procfile"
                    | "rakefile"
            );
        looks_like_file && crate::index::languages::detect_language(trimmed).is_none()
    })
}

fn literal_like_query(query: &str) -> bool {
    let query = query.trim();
    if query.is_empty() {
        return false;
    }

    contains_quoted_literal(query)
        || contains_call_site_syntax(query)
        || contains_path_separator(query)
        || is_dotted_literal(query)
}

fn contains_quoted_literal(query: &str) -> bool {
    query.contains('"')
        || query.contains('`')
        || (query.starts_with('\'') && query.ends_with('\'') && query.len() > 1)
}

fn contains_call_site_syntax(query: &str) -> bool {
    query.char_indices().any(|(idx, ch)| {
        if ch != '(' || idx == 0 {
            return false;
        }

        query[..idx]
            .chars()
            .next_back()
            .is_some_and(|prev| prev.is_ascii_alphanumeric() || matches!(prev, '_' | '.' | ':'))
    })
}

fn contains_path_separator(query: &str) -> bool {
    query.contains('/') || query.contains('\\')
}

fn is_dotted_literal(query: &str) -> bool {
    if query.chars().any(char::is_whitespace) || !query.contains('.') {
        return false;
    }

    query
        .split('.')
        .all(|part| !part.is_empty() && part.chars().all(is_dotted_literal_char))
}

fn is_dotted_literal_char(ch: char) -> bool {
    ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-')
}

#[cfg(test)]
#[path = "search_regression_tests.rs"]
mod regression_tests;

#[cfg(test)]
#[path = "search_unit_tests.rs"]
mod tests;
