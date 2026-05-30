use std::collections::HashSet;

use postgres::Client;

use crate::config::Context;
use crate::models::{SearchResult, Symbol};
use crate::visibility;

use super::common::{
    SymbolFilters, append_unique_symbols, append_visible_symbols, escape_like, push_param,
    query_symbols_by_conditions, sanitize_pg_search_query,
};

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

pub fn search_symbols_fts_visible(
    conn: &mut Client,
    query: &str,
    ctx: &Context,
    kind: Option<&str>,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> Vec<Symbol> {
    let mut results = Vec::new();
    let mut seen = HashSet::new();
    for project_id in visibility::visible_project_ids(ctx) {
        let symbols = search_symbols_fts(conn, query, &project_id, kind, language, paths, limit);
        append_visible_symbols(conn, ctx, &mut results, &mut seen, symbols, limit);
        if results.len() >= limit {
            break;
        }
    }
    results
}

pub fn search_symbols_by_name_visible(
    conn: &mut Client,
    query: &str,
    ctx: &Context,
    kind: Option<&str>,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> Vec<Symbol> {
    let mut results = Vec::new();
    let mut seen = HashSet::new();
    for project_id in visibility::visible_project_ids(ctx) {
        let symbols =
            search_symbols_by_name(conn, query, &project_id, kind, language, paths, limit);
        append_visible_symbols(conn, ctx, &mut results, &mut seen, symbols, limit);
        if results.len() >= limit {
            break;
        }
    }
    results
}

pub fn search_symbols_exact_first_visible(
    conn: &mut Client,
    query: &str,
    ctx: &Context,
    kind: Option<&str>,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> Vec<Symbol> {
    let mut results = Vec::new();
    let mut seen = HashSet::new();
    for project_id in visibility::visible_project_ids(ctx) {
        let symbols =
            search_symbols_exact_first(conn, query, &project_id, kind, language, paths, limit);
        append_visible_symbols(conn, ctx, &mut results, &mut seen, symbols, limit);
        if results.len() >= limit {
            break;
        }
    }
    results
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

pub fn search_text_visible(
    conn: &mut Client,
    query: &str,
    ctx: &Context,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> Vec<SearchResult> {
    let mut results = search_symbols_fts_visible(conn, query, ctx, None, language, paths, limit);
    if results.is_empty() {
        results = search_symbols_by_name_visible(conn, query, ctx, None, language, paths, limit);
    }
    results.into_iter().map(|s| s.to_brief()).collect()
}
