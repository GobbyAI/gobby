//! PostgreSQL pg_search BM25 query sanitization and execution.
//!
//! The module name stays `fts` to keep command wiring stable; runtime keyword
//! search is pg_search BM25 against Gobby's PostgreSQL hub.

use std::collections::HashSet;

use postgres::Client;
use postgres::types::ToSql;

use crate::db;
use crate::models::{ContentSearchHit, SearchResult, Symbol};

type PgParam = Box<dyn ToSql + Sync>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedGraphSymbol {
    pub id: String,
    pub display_name: String,
}

#[derive(Debug, Clone, Copy, Default)]
struct SymbolFilters<'a> {
    kind: Option<&'a str>,
    language: Option<&'a str>,
    paths: &'a [String],
}

pub const FILTERED_FETCH_CAP: usize = 10_000;

fn push_param<T>(params: &mut Vec<PgParam>, value: T) -> String
where
    T: ToSql + Sync + 'static,
{
    params.push(Box::new(value));
    format!("${}", params.len())
}

fn param_refs(params: &[PgParam]) -> Vec<&(dyn ToSql + Sync)> {
    params
        .iter()
        .map(|param| param.as_ref() as &(dyn ToSql + Sync))
        .collect()
}

/// Escape LIKE wildcards (`%`, `_`) and the backslash escape char itself.
fn escape_like(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        if matches!(c, '\\' | '%' | '_') {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

/// Extract a SQL LIKE prefix from a glob pattern for index-assisted pre-filtering.
fn glob_to_like_prefix(pattern: &str) -> Option<String> {
    let prefix: String = pattern
        .chars()
        .take_while(|c| !matches!(c, '*' | '?' | '['))
        .collect();
    if prefix.is_empty() {
        None
    } else {
        Some(format!("{}%", escape_like(&prefix)))
    }
}

fn has_glob_meta(path: &str) -> bool {
    path.chars().any(|c| matches!(c, '*' | '?' | '['))
}

pub fn expand_paths(paths: &[String]) -> Vec<String> {
    let mut expanded = Vec::new();
    let mut seen = HashSet::new();
    for path in paths {
        let trimmed = path.trim().trim_end_matches('/');
        if trimmed.is_empty() {
            continue;
        }

        let patterns = if has_glob_meta(trimmed) {
            vec![trimmed.to_string()]
        } else {
            vec![trimmed.to_string(), format!("{trimmed}/**")]
        };
        for pattern in patterns {
            if seen.insert(pattern.clone()) {
                expanded.push(pattern);
            }
        }
    }
    expanded
}

pub fn compile_patterns(paths: &[String]) -> anyhow::Result<Vec<glob::Pattern>> {
    paths
        .iter()
        .map(|path| {
            glob::Pattern::new(path).map_err(|e| anyhow::anyhow!("invalid path glob `{path}`: {e}"))
        })
        .collect()
}

fn path_like_prefixes(paths: &[String]) -> Option<Vec<String>> {
    if paths.is_empty() {
        return Some(Vec::new());
    }

    let mut prefixes = Vec::with_capacity(paths.len());
    for path in paths {
        prefixes.push(glob_to_like_prefix(path)?);
    }
    Some(prefixes)
}

fn push_path_filter(
    conditions: &mut Vec<String>,
    params: &mut Vec<PgParam>,
    alias: &str,
    paths: &[String],
) {
    let Some(prefixes) = path_like_prefixes(paths) else {
        log::debug!(
            "omitting SQL path filter for alias `{alias}` because path filters {:?} cannot be converted to LIKE prefixes; relying on post-query glob matching",
            paths
        );
        return;
    };
    if prefixes.is_empty() {
        return;
    }

    let predicates = prefixes
        .into_iter()
        .map(|prefix| {
            let placeholder = push_param(params, prefix);
            format!("{alias}.file_path LIKE {placeholder} ESCAPE '\\'")
        })
        .collect::<Vec<_>>();
    conditions.push(format!("({})", predicates.join(" OR ")));
}

fn push_symbol_filters(
    conditions: &mut Vec<String>,
    params: &mut Vec<PgParam>,
    alias: &str,
    filters: SymbolFilters<'_>,
) {
    if let Some(kind) = filters.kind {
        let placeholder = push_param(params, kind.to_string());
        conditions.push(format!("{alias}.kind = {placeholder}"));
    }
    if let Some(language) = filters.language {
        let placeholder = push_param(params, language.to_string());
        conditions.push(format!("{alias}.language = {placeholder}"));
    }
    push_path_filter(conditions, params, alias, filters.paths);
}

fn append_unique_symbols(
    out: &mut Vec<Symbol>,
    seen: &mut HashSet<String>,
    symbols: Vec<Symbol>,
    limit: usize,
) {
    for symbol in symbols {
        if seen.insert(symbol.id.clone()) {
            out.push(symbol);
            if out.len() >= limit {
                return;
            }
        }
    }
}

fn query_symbols_by_conditions(
    conn: &mut Client,
    mut conditions: Vec<String>,
    mut params: Vec<PgParam>,
    filters: SymbolFilters<'_>,
    limit: usize,
    order_by: &str,
) -> Vec<Symbol> {
    push_symbol_filters(&mut conditions, &mut params, "cs", filters);
    let limit_placeholder = push_param(&mut params, limit as i64);
    let where_clause = conditions.join(" AND ");
    let columns = db::symbol_select_columns("cs");
    let sql = format!(
        "SELECT {columns}
         FROM code_symbols cs
         JOIN code_indexed_files cf
           ON cf.project_id = cs.project_id AND cf.file_path = cs.file_path
         WHERE {where_clause}
         ORDER BY {order_by}
         LIMIT {limit_placeholder}"
    );
    let refs = param_refs(&params);
    conn.query(&sql, &refs)
        .ok()
        .map(|rows| {
            rows.iter()
                .filter_map(|row| Symbol::from_row(row).ok())
                .collect()
        })
        .unwrap_or_default()
}

/// Sanitize user input for pg_search's BM25 query DSL.
pub fn sanitize_pg_search_query(query: &str) -> String {
    let cleaned: String = query
        .chars()
        .map(|ch| {
            if ch.is_alphanumeric() || matches!(ch, ' ' | '_' | '-') {
                ch
            } else {
                ' '
            }
        })
        .collect();
    cleaned
        .split_whitespace()
        .filter(|token| !token.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

/// BM25 search across symbol names, qualified names, signatures, docstrings, and summaries.
pub fn search_symbols_fts(
    conn: &mut Client,
    query: &str,
    project_id: &str,
    kind: Option<&str>,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> Vec<Symbol> {
    let bm25_query = sanitize_pg_search_query(query);
    if bm25_query.is_empty() || limit == 0 {
        return Vec::new();
    }

    let mut params = Vec::new();
    let query_placeholder = push_param(&mut params, bm25_query);
    let project_placeholder = push_param(&mut params, project_id.to_string());
    let conditions = vec![
        format!(
            "(cs.name @@@ {q} OR cs.qualified_name @@@ {q} OR cs.signature @@@ {q} OR cs.docstring @@@ {q} OR cs.summary @@@ {q})",
            q = query_placeholder
        ),
        format!("cs.project_id = {project_placeholder}"),
    ];
    let filters = SymbolFilters {
        kind,
        language,
        paths,
    };
    query_symbols_by_conditions(
        conn,
        conditions,
        params,
        filters,
        limit,
        "pdb.score(cs.id) DESC, cs.id ASC",
    )
}

/// Fallback LIKE search on symbol names.
pub fn search_symbols_by_name(
    conn: &mut Client,
    query: &str,
    project_id: &str,
    kind: Option<&str>,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> Vec<Symbol> {
    if query.trim().is_empty() || limit == 0 {
        return Vec::new();
    }
    let escaped_query = escape_like(query);
    let pattern = format!("%{escaped_query}%");
    let mut params = Vec::new();
    let project_placeholder = push_param(&mut params, project_id.to_string());
    let name_placeholder = push_param(&mut params, pattern.clone());
    let qualified_placeholder = push_param(&mut params, pattern);
    let conditions = vec![
        format!("cs.project_id = {project_placeholder}"),
        format!(
            "(cs.name LIKE {name_placeholder} ESCAPE '\\' OR cs.qualified_name LIKE {qualified_placeholder} ESCAPE '\\')"
        ),
    ];
    query_symbols_by_conditions(
        conn,
        conditions,
        params,
        SymbolFilters {
            kind,
            language,
            paths,
        },
        limit,
        "cs.name ASC, cs.file_path ASC, cs.line_start ASC",
    )
}

pub fn search_symbols_exact_first(
    conn: &mut Client,
    query: &str,
    project_id: &str,
    kind: Option<&str>,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> Vec<Symbol> {
    if query.trim().is_empty() || limit == 0 {
        return Vec::new();
    }

    let mut results = Vec::new();
    let mut seen = HashSet::new();
    let filters = SymbolFilters {
        kind,
        language,
        paths,
    };

    let mut params = Vec::new();
    let project = push_param(&mut params, project_id.to_string());
    let name = push_param(&mut params, query.to_string());
    let qualified = push_param(&mut params, query.to_string());
    let exact = query_symbols_by_conditions(
        conn,
        vec![
            format!("cs.project_id = {project}"),
            format!("(cs.name = {name} OR cs.qualified_name = {qualified})"),
        ],
        params,
        filters,
        limit,
        "cs.file_path ASC, cs.line_start ASC",
    );
    append_unique_symbols(&mut results, &mut seen, exact, limit);
    if results.len() >= limit {
        return results;
    }

    let mut params = Vec::new();
    let project = push_param(&mut params, project_id.to_string());
    let name = push_param(&mut params, query.to_string());
    let qualified = push_param(&mut params, query.to_string());
    let ci_exact = query_symbols_by_conditions(
        conn,
        vec![
            format!("cs.project_id = {project}"),
            format!(
                "(lower(cs.name) = lower({name}) OR lower(cs.qualified_name) = lower({qualified}))"
            ),
        ],
        params,
        filters,
        limit,
        "cs.file_path ASC, cs.line_start ASC",
    );
    append_unique_symbols(&mut results, &mut seen, ci_exact, limit);
    if results.len() >= limit {
        return results;
    }

    let prefix_pattern = format!("{}%", escape_like(query));
    let mut params = Vec::new();
    let project = push_param(&mut params, project_id.to_string());
    let name = push_param(&mut params, prefix_pattern.clone());
    let qualified = push_param(&mut params, prefix_pattern);
    let prefix_matches = query_symbols_by_conditions(
        conn,
        vec![
            format!("cs.project_id = {project}"),
            format!(
                "(cs.name LIKE {name} ESCAPE '\\' OR cs.qualified_name LIKE {qualified} ESCAPE '\\')"
            ),
        ],
        params,
        filters,
        limit,
        "cs.name ASC, cs.file_path ASC, cs.line_start ASC",
    );
    append_unique_symbols(&mut results, &mut seen, prefix_matches, limit);
    if results.len() >= limit {
        return results;
    }

    let contains = search_symbols_by_name(conn, query, project_id, kind, language, paths, limit);
    append_unique_symbols(&mut results, &mut seen, contains, limit);
    if results.len() >= limit {
        return results;
    }

    let fts = search_symbols_fts(conn, query, project_id, kind, language, paths, limit);
    append_unique_symbols(&mut results, &mut seen, fts, limit);

    results
}

fn exact_symbol_matches(
    conn: &mut Client,
    project_id: &str,
    column: &str,
    input: &str,
    limit: usize,
) -> Vec<Symbol> {
    if !matches!(column, "id" | "qualified_name" | "name") {
        return Vec::new();
    }
    let columns = db::symbol_select_columns("");
    let sql = format!(
        "SELECT {columns}
         FROM code_symbols
         WHERE project_id = $1 AND {column} = $2
         ORDER BY file_path ASC, line_start ASC
         LIMIT $3"
    );
    conn.query(&sql, &[&project_id, &input, &(limit as i64)])
        .ok()
        .map(|rows| {
            rows.iter()
                .filter_map(|row| Symbol::from_row(row).ok())
                .collect()
        })
        .unwrap_or_default()
}

fn suggestion_label(symbol: &Symbol) -> String {
    format!(
        "{} ({}:{})",
        symbol.qualified_name, symbol.file_path, symbol.line_start
    )
}

fn resolved_symbol(symbol: &Symbol) -> ResolvedGraphSymbol {
    ResolvedGraphSymbol {
        id: symbol.id.clone(),
        display_name: symbol.name.clone(),
    }
}

fn resolve_from_candidates(candidates: Vec<Symbol>) -> (Option<ResolvedGraphSymbol>, Vec<String>) {
    match candidates.len() {
        0 => (None, vec![]),
        1 => (Some(resolved_symbol(&candidates[0])), vec![]),
        _ => {
            let mut suggestions = Vec::new();
            let mut seen = HashSet::new();
            for symbol in &candidates {
                let label = suggestion_label(symbol);
                if seen.insert(label.clone()) {
                    suggestions.push(label);
                }
            }
            (None, suggestions)
        }
    }
}

/// Resolve user input to a canonical symbol id for graph queries.
///
/// Resolution is fail-closed: ambiguous matches return `None` with suggestions.
pub fn resolve_graph_symbol(
    conn: &mut Client,
    input: &str,
    project_id: &str,
) -> (Option<ResolvedGraphSymbol>, Vec<String>) {
    let ids = exact_symbol_matches(conn, project_id, "id", input, 2);
    let (resolved, suggestions) = resolve_from_candidates(ids);
    if resolved.is_some() || !suggestions.is_empty() {
        return (resolved, suggestions);
    }

    let qualified = exact_symbol_matches(conn, project_id, "qualified_name", input, 6);
    let (resolved, suggestions) = resolve_from_candidates(qualified);
    if resolved.is_some() || !suggestions.is_empty() {
        return (resolved, suggestions);
    }

    let exact = exact_symbol_matches(conn, project_id, "name", input, 6);
    let (resolved, suggestions) = resolve_from_candidates(exact);
    if resolved.is_some() || !suggestions.is_empty() {
        return (resolved, suggestions);
    }

    let like_matches = search_symbols_by_name(conn, input, project_id, None, None, &[], 6);
    let (resolved, suggestions) = resolve_from_candidates(like_matches);
    if resolved.is_some() || !suggestions.is_empty() {
        return (resolved, suggestions);
    }

    let fts_results = search_symbols_fts(conn, input, project_id, None, None, &[], 6);
    resolve_from_candidates(fts_results)
}

/// Count matching symbols using pg_search BM25, with LIKE fallback.
pub fn count_text(
    conn: &mut Client,
    query: &str,
    project_id: &str,
    language: Option<&str>,
    paths: &[String],
) -> usize {
    if query.trim().is_empty() {
        return 0;
    }

    let bm25_query = sanitize_pg_search_query(query);
    // Intentional fallback: when BM25 sanitization empties the query, use
    // count_symbols_by_name_like, which may count LIKE matches BM25 filtered out.
    if bm25_query.is_empty() {
        return count_symbols_by_name_like(conn, query, project_id, language, paths);
    }

    let mut params = Vec::new();
    let query_placeholder = push_param(&mut params, bm25_query);
    let project_placeholder = push_param(&mut params, project_id.to_string());
    let mut conditions = vec![
        format!(
            "(cs.name @@@ {q} OR cs.qualified_name @@@ {q} OR cs.signature @@@ {q} OR cs.docstring @@@ {q} OR cs.summary @@@ {q})",
            q = query_placeholder
        ),
        format!("cs.project_id = {project_placeholder}"),
    ];
    push_symbol_filters(
        &mut conditions,
        &mut params,
        "cs",
        SymbolFilters {
            kind: None,
            language,
            paths,
        },
    );
    let refs = param_refs(&params);
    let sql = format!(
        "SELECT COUNT(*)::BIGINT AS count
         FROM code_symbols cs
         JOIN code_indexed_files cf
           ON cf.project_id = cs.project_id AND cf.file_path = cs.file_path
         WHERE {}",
        conditions.join(" AND ")
    );
    let count = conn
        .query_one(&sql, &refs)
        .ok()
        .and_then(|row| row.try_get::<_, i64>("count").ok())
        .unwrap_or(0);
    if count > 0 {
        return count as usize;
    }

    count_symbols_by_name_like(conn, query, project_id, language, paths)
}

fn count_symbols_by_name_like(
    conn: &mut Client,
    query: &str,
    project_id: &str,
    language: Option<&str>,
    paths: &[String],
) -> usize {
    let escaped_query = escape_like(query);
    let pattern = format!("%{escaped_query}%");
    let mut params = Vec::new();
    let project_placeholder = push_param(&mut params, project_id.to_string());
    let name_placeholder = push_param(&mut params, pattern.clone());
    let qualified_placeholder = push_param(&mut params, pattern);
    let mut conditions = vec![
        format!("cs.project_id = {project_placeholder}"),
        format!(
            "(cs.name LIKE {name_placeholder} ESCAPE '\\' OR cs.qualified_name LIKE {qualified_placeholder} ESCAPE '\\')"
        ),
    ];
    push_symbol_filters(
        &mut conditions,
        &mut params,
        "cs",
        SymbolFilters {
            kind: None,
            language,
            paths,
        },
    );
    let refs = param_refs(&params);
    let sql = format!(
        "SELECT COUNT(*)::BIGINT AS count
         FROM code_symbols cs
         JOIN code_indexed_files cf
           ON cf.project_id = cs.project_id AND cf.file_path = cs.file_path
         WHERE {}",
        conditions.join(" AND ")
    );
    conn.query_one(&sql, &refs)
        .ok()
        .and_then(|row| row.try_get::<_, i64>("count").ok())
        .unwrap_or(0) as usize
}

/// Count matching content chunks using pg_search BM25, with LIKE fallback.
pub fn count_content(
    conn: &mut Client,
    query: &str,
    project_id: &str,
    language: Option<&str>,
    paths: &[String],
) -> usize {
    if query.trim().is_empty() {
        return 0;
    }

    let bm25_query = sanitize_pg_search_query(query);
    if bm25_query.is_empty() {
        return count_content_like(conn, query, project_id, language, paths);
    }
    let mut params = Vec::new();
    let query_placeholder = push_param(&mut params, bm25_query);
    let project_placeholder = push_param(&mut params, project_id.to_string());
    let mut conditions = vec![
        format!("c.content @@@ {query_placeholder}"),
        format!("c.project_id = {project_placeholder}"),
    ];
    if let Some(lang) = language {
        let placeholder = push_param(&mut params, lang.to_string());
        conditions.push(format!("c.language = {placeholder}"));
    }
    push_path_filter(&mut conditions, &mut params, "c", paths);
    let refs = param_refs(&params);
    let sql = format!(
        "SELECT COUNT(*)::BIGINT AS count
         FROM code_content_chunks c
         JOIN code_indexed_files cf
           ON cf.project_id = c.project_id AND cf.file_path = c.file_path
         WHERE {}",
        conditions.join(" AND ")
    );
    let count = conn
        .query_one(&sql, &refs)
        .ok()
        .and_then(|row| row.try_get::<_, i64>("count").ok())
        .unwrap_or(0);
    if count > 0 {
        return count as usize;
    }

    count_content_like(conn, query, project_id, language, paths)
}

fn count_content_like(
    conn: &mut Client,
    query: &str,
    project_id: &str,
    language: Option<&str>,
    paths: &[String],
) -> usize {
    let escaped_query = escape_like(query);
    let like_query = format!("%{escaped_query}%");
    let mut params = Vec::new();
    let project_placeholder = push_param(&mut params, project_id.to_string());
    let like_placeholder = push_param(&mut params, like_query);
    let mut conditions = vec![
        format!("c.project_id = {project_placeholder}"),
        format!("c.content LIKE {like_placeholder} ESCAPE '\\'"),
    ];
    if let Some(lang) = language {
        let placeholder = push_param(&mut params, lang.to_string());
        conditions.push(format!("c.language = {placeholder}"));
    }
    push_path_filter(&mut conditions, &mut params, "c", paths);
    let refs = param_refs(&params);
    let sql = format!(
        "SELECT COUNT(*)::BIGINT AS count
         FROM code_content_chunks c
         JOIN code_indexed_files cf
           ON cf.project_id = c.project_id AND cf.file_path = c.file_path
         WHERE {}",
        conditions.join(" AND ")
    );
    conn.query_one(&sql, &refs)
        .ok()
        .and_then(|row| row.try_get::<_, i64>("count").ok())
        .unwrap_or(0) as usize
}

/// Full-text search for symbols: BM25 with LIKE fallback.
pub fn search_text(
    conn: &mut Client,
    query: &str,
    project_id: &str,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> Vec<SearchResult> {
    let mut results = search_symbols_fts(conn, query, project_id, None, language, paths, limit);
    if results.is_empty() {
        results = search_symbols_by_name(conn, query, project_id, None, language, paths, limit);
    }
    results.into_iter().map(|s| s.to_brief()).collect()
}

/// Full-text search across file content chunks.
pub fn search_content(
    conn: &mut Client,
    query: &str,
    project_id: &str,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> Vec<ContentSearchHit> {
    if query.trim().is_empty() || limit == 0 {
        return Vec::new();
    }

    let bm25_query = sanitize_pg_search_query(query);
    if bm25_query.is_empty() {
        return search_content_like(conn, query, project_id, language, paths, limit);
    }
    let mut params = Vec::new();
    let query_placeholder = push_param(&mut params, bm25_query);
    let project_placeholder = push_param(&mut params, project_id.to_string());
    let mut conditions = vec![
        format!("c.content @@@ {query_placeholder}"),
        format!("c.project_id = {project_placeholder}"),
    ];
    if let Some(lang) = language {
        let placeholder = push_param(&mut params, lang.to_string());
        conditions.push(format!("c.language = {placeholder}"));
    }
    push_path_filter(&mut conditions, &mut params, "c", paths);
    let limit_placeholder = push_param(&mut params, limit as i64);
    let refs = param_refs(&params);
    let sql = format!(
        "SELECT c.file_path,
                c.line_start::BIGINT AS line_start,
                c.line_end::BIGINT AS line_end,
                c.language,
                c.content
         FROM code_content_chunks c
         JOIN code_indexed_files cf
           ON cf.project_id = c.project_id AND cf.file_path = c.file_path
         WHERE {}
         ORDER BY pdb.score(c.id) DESC, c.id ASC
         LIMIT {limit_placeholder}",
        conditions.join(" AND ")
    );

    let hits: Vec<ContentSearchHit> = conn
        .query(&sql, &refs)
        .ok()
        .map(|rows| {
            rows.iter()
                .filter_map(|row| {
                    let content: String = row.try_get("content").ok()?;
                    Some(ContentSearchHit {
                        file_path: row.try_get("file_path").ok()?,
                        line_start: row.try_get::<_, i64>("line_start").ok()? as usize,
                        line_end: row.try_get::<_, i64>("line_end").ok()? as usize,
                        snippet: make_snippet(&content, query),
                        language: row.try_get("language").ok()?,
                    })
                })
                .collect()
        })
        .unwrap_or_default();

    if !hits.is_empty() {
        return hits;
    }

    search_content_like(conn, query, project_id, language, paths, limit)
}

fn search_content_like(
    conn: &mut Client,
    query: &str,
    project_id: &str,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> Vec<ContentSearchHit> {
    let escaped_query = escape_like(query);
    let like_query = format!("%{escaped_query}%");
    let mut params = Vec::new();
    let project_placeholder = push_param(&mut params, project_id.to_string());
    let like_placeholder = push_param(&mut params, like_query);
    let mut conditions = vec![
        format!("c.project_id = {project_placeholder}"),
        format!("c.content LIKE {like_placeholder} ESCAPE '\\'"),
    ];
    if let Some(lang) = language {
        let placeholder = push_param(&mut params, lang.to_string());
        conditions.push(format!("c.language = {placeholder}"));
    }
    push_path_filter(&mut conditions, &mut params, "c", paths);
    let limit_placeholder = push_param(&mut params, limit as i64);
    let refs = param_refs(&params);
    let sql = format!(
        "SELECT c.file_path,
                c.line_start::BIGINT AS line_start,
                c.line_end::BIGINT AS line_end,
                c.language,
                c.content
         FROM code_content_chunks c
         JOIN code_indexed_files cf
           ON cf.project_id = c.project_id AND cf.file_path = c.file_path
         WHERE {}
         ORDER BY c.file_path ASC, c.line_start ASC
         LIMIT {limit_placeholder}",
        conditions.join(" AND ")
    );

    conn.query(&sql, &refs)
        .ok()
        .map(|rows| {
            rows.iter()
                .filter_map(|row| {
                    let content: String = row.try_get("content").ok()?;
                    Some(ContentSearchHit {
                        file_path: row.try_get("file_path").ok()?,
                        line_start: row.try_get::<_, i64>("line_start").ok()? as usize,
                        line_end: row.try_get::<_, i64>("line_end").ok()? as usize,
                        snippet: make_snippet(&content, query),
                        language: row.try_get("language").ok()?,
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

fn make_snippet(content: &str, query: &str) -> String {
    let tokens: Vec<String> = query
        .split_whitespace()
        .map(str::to_lowercase)
        .filter(|token| !token.is_empty())
        .collect();
    let mut match_at = None;
    for token in tokens {
        if let Some(index) =
            content
                .char_indices()
                .enumerate()
                .find_map(|(char_index, (byte_index, _))| {
                    content[byte_index..]
                        .to_lowercase()
                        .starts_with(&token)
                        .then_some(char_index)
                })
        {
            match_at = Some(index);
            break;
        }
    }
    let match_at = match_at.unwrap_or(0);
    let start = match_at.saturating_sub(60);
    let end = (match_at + 120).min(content.chars().count());
    content.chars().skip(start).take(end - start).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sanitize_pg_search_query_matches_gobby_rules() {
        assert_eq!(
            sanitize_pg_search_query("foo::bar baz-qux _id + \"drop\""),
            "foo bar baz-qux _id drop"
        );
    }

    #[test]
    fn sanitize_pg_search_query_drops_empty_queries() {
        assert_eq!(sanitize_pg_search_query(":: + ()"), "");
    }

    #[test]
    fn glob_to_like_prefix_escapes_like_wildcards() {
        assert_eq!(
            glob_to_like_prefix("src/foo_bar/*.rs").as_deref(),
            Some("src/foo\\_bar/%")
        );
    }

    #[test]
    fn expand_paths_trims_skips_empty_and_expands_bare_paths() {
        let paths = vec![
            " src/gobby ".to_string(),
            "".to_string(),
            "crates/**/*.rs".to_string(),
            "src/gobby/".to_string(),
        ];

        assert_eq!(
            expand_paths(&paths),
            vec!["src/gobby", "src/gobby/**", "crates/**/*.rs"]
        );
    }

    #[test]
    fn compile_patterns_reports_invalid_glob() {
        let err = compile_patterns(&["src/[".to_string()])
            .expect_err("invalid glob should fail")
            .to_string();

        assert!(err.contains("invalid path glob `src/[`"));
    }

    #[test]
    fn path_like_prefixes_escape_and_require_all_patterns() {
        let paths = vec![
            "src/foo_bar".to_string(),
            "src/foo_bar/**".to_string(),
            "src/100%/**".to_string(),
        ];
        assert_eq!(
            path_like_prefixes(&paths).expect("prefixes"),
            vec!["src/foo\\_bar%", "src/foo\\_bar/%", "src/100\\%/%"]
        );

        let mixed = vec!["src/**".to_string(), "*.rs".to_string()];
        assert!(path_like_prefixes(&mixed).is_none());
    }

    #[test]
    fn snippet_centers_first_matching_token() {
        let content = "before ".repeat(20) + "target call here";
        let snippet = make_snippet(&content, "target");

        assert!(snippet.contains("target call here"));
        assert!(snippet.len() <= 180);
    }

    #[test]
    fn snippet_handles_unicode_before_match() {
        let content = "é".repeat(80) + " target call here";
        let snippet = make_snippet(&content, "target");

        assert!(snippet.contains("target call here"));
        assert!(snippet.chars().count() <= 180);

        let content = "\u{0130}".repeat(80) + " target call here";
        let snippet = make_snippet(&content, "target");

        assert!(snippet.contains("target call here"));
        assert!(snippet.chars().count() <= 180);
    }
}
