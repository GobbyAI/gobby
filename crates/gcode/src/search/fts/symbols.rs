use std::collections::HashSet;

use postgres::Client;

use crate::config::Context;
use crate::models::{SearchResult, Symbol};
use crate::visibility;

use super::common::{
    FILTERED_FETCH_CAP, PgParam, SymbolFilters, SymbolOrder, append_unique_symbols, escape_like,
    push_id_list_param, push_id_param, push_param, query_symbols_by_conditions,
    sanitize_pg_search_query,
};
use super::errors::{SYMBOL_INDEX, bm25_query_error, database_query_error};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VisibleSearchOutcome<T> {
    pub results: Vec<T>,
    pub degraded: bool,
}

impl<T> VisibleSearchOutcome<T> {
    fn ok(results: Vec<T>) -> Self {
        Self {
            results,
            degraded: false,
        }
    }
}

pub fn search_symbols_fts(
    conn: &mut Client,
    query: &str,
    project_id: &str,
    kind: Option<&str>,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> anyhow::Result<Vec<Symbol>> {
    let bm25_query = sanitize_pg_search_query(query);
    if bm25_query.is_empty() || limit == 0 {
        return Ok(Vec::new());
    }

    let mut params = Vec::new();
    let query_placeholder = push_param(&mut params, bm25_query);
    let project_placeholder = push_id_param(&mut params, project_id);
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
        SymbolOrder::Bm25Score,
    )
    .map_err(|error| bm25_query_error(SYMBOL_INDEX, &error))
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
) -> anyhow::Result<Vec<Symbol>> {
    if query.trim().is_empty() || limit == 0 {
        return Ok(Vec::new());
    }
    let escaped_query = escape_like(query);
    let pattern = format!("%{escaped_query}%");
    let mut params = Vec::new();
    let project_placeholder = push_id_param(&mut params, project_id);
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
        SymbolOrder::Name,
    )
    .map_err(|error| database_query_error("symbol name query", &error))
}

pub fn search_symbols_exact_first(
    conn: &mut Client,
    query: &str,
    project_id: &str,
    kind: Option<&str>,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> anyhow::Result<Vec<Symbol>> {
    if query.trim().is_empty() || limit == 0 {
        return Ok(Vec::new());
    }

    let mut results = Vec::new();
    let mut seen = HashSet::new();
    let filters = SymbolFilters {
        kind,
        language,
        paths,
    };

    let mut params = Vec::new();
    let project = push_id_param(&mut params, project_id);
    let query_param = push_param(&mut params, query.to_string());
    let order = SymbolOrder::ExactCaseFirst(query_param.clone());
    let exact = query_symbols_by_conditions(
        conn,
        vec![
            format!("cs.project_id = {project}"),
            format!(
                "(cs.name = {q} OR cs.qualified_name = {q} OR lower(cs.name) = lower({q}) OR lower(cs.qualified_name) = lower({q}))",
                q = query_param
            ),
        ],
        params,
        filters,
        limit,
        order,
    )
    .map_err(|error| database_query_error("exact symbol query", &error))?;
    append_unique_symbols(&mut results, &mut seen, exact, limit);
    if results.len() >= limit {
        return Ok(results);
    }

    let prefix_pattern = format!("{}%", escape_like(query));
    let mut params = Vec::new();
    let project = push_id_param(&mut params, project_id);
    let prefix = push_param(&mut params, prefix_pattern);
    let prefix_matches = query_symbols_by_conditions(
        conn,
        vec![
            format!("cs.project_id = {project}"),
            format!(
                "(cs.name LIKE {prefix} ESCAPE '\\' OR cs.qualified_name LIKE {prefix} ESCAPE '\\')"
            ),
        ],
        params,
        filters,
        limit,
        SymbolOrder::Name,
    )
    .map_err(|error| database_query_error("symbol prefix query", &error))?;
    append_unique_symbols(&mut results, &mut seen, prefix_matches, limit);
    if results.len() >= limit {
        return Ok(results);
    }

    let contains = search_symbols_by_name(conn, query, project_id, kind, language, paths, limit)?;
    append_unique_symbols(&mut results, &mut seen, contains, limit);
    if results.len() >= limit {
        return Ok(results);
    }

    let fts = search_symbols_fts(conn, query, project_id, kind, language, paths, limit)?;
    append_unique_symbols(&mut results, &mut seen, fts, limit);

    Ok(results)
}

pub fn search_symbols_fts_visible(
    conn: &mut Client,
    query: &str,
    ctx: &Context,
    kind: Option<&str>,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> anyhow::Result<VisibleSearchOutcome<Symbol>> {
    let bm25_query = sanitize_pg_search_query(query);
    if bm25_query.is_empty() || limit == 0 {
        return Ok(VisibleSearchOutcome::ok(Vec::new()));
    }

    let mut params = Vec::new();
    let query_placeholder = push_param(&mut params, bm25_query);
    let conditions = vec![format!(
        "(cs.name @@@ {q} OR cs.qualified_name @@@ {q} OR cs.signature @@@ {q} OR cs.docstring @@@ {q} OR cs.summary @@@ {q})",
        q = query_placeholder
    )];
    query_visible_symbols_by_conditions(
        conn,
        ctx,
        conditions,
        params,
        SymbolFilters {
            kind,
            language,
            paths,
        },
        limit,
        SymbolOrder::Bm25Score,
    )
}

pub fn search_symbols_by_name_visible(
    conn: &mut Client,
    query: &str,
    ctx: &Context,
    kind: Option<&str>,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> anyhow::Result<VisibleSearchOutcome<Symbol>> {
    if query.trim().is_empty() || limit == 0 {
        return Ok(VisibleSearchOutcome::ok(Vec::new()));
    }
    let escaped_query = escape_like(query);
    let pattern = format!("%{escaped_query}%");
    let mut params = Vec::new();
    let name_placeholder = push_param(&mut params, pattern.clone());
    let qualified_placeholder = push_param(&mut params, pattern);
    let conditions = vec![format!(
        "(cs.name LIKE {name_placeholder} ESCAPE '\\' OR cs.qualified_name LIKE {qualified_placeholder} ESCAPE '\\')"
    )];
    query_visible_symbols_by_conditions(
        conn,
        ctx,
        conditions,
        params,
        SymbolFilters {
            kind,
            language,
            paths,
        },
        limit,
        SymbolOrder::Name,
    )
}

pub fn search_symbols_exact_first_visible(
    conn: &mut Client,
    query: &str,
    ctx: &Context,
    kind: Option<&str>,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> anyhow::Result<VisibleSearchOutcome<Symbol>> {
    if query.trim().is_empty() || limit == 0 {
        return Ok(VisibleSearchOutcome::ok(Vec::new()));
    }

    let mut results = Vec::new();
    let mut seen = HashSet::new();
    let mut degraded = false;
    let filters = SymbolFilters {
        kind,
        language,
        paths,
    };

    let mut params = Vec::new();
    let query_param = push_param(&mut params, query.to_string());
    let order = SymbolOrder::ExactCaseFirst(query_param.clone());
    let exact = query_visible_symbols_by_conditions(
        conn,
        ctx,
        vec![format!(
            "(cs.name = {q} OR cs.qualified_name = {q} OR lower(cs.name) = lower({q}) OR lower(cs.qualified_name) = lower({q}))",
            q = query_param
        )],
        params,
        filters,
        limit,
        order,
    )?;
    degraded |= exact.degraded;
    append_unique_symbols(&mut results, &mut seen, exact.results, limit);
    if results.len() >= limit {
        return Ok(VisibleSearchOutcome { results, degraded });
    }

    let prefix_pattern = format!("{}%", escape_like(query));
    let mut params = Vec::new();
    let prefix = push_param(&mut params, prefix_pattern);
    let prefix_matches = query_visible_symbols_by_conditions(
        conn,
        ctx,
        vec![format!(
            "(cs.name LIKE {prefix} ESCAPE '\\' OR cs.qualified_name LIKE {prefix} ESCAPE '\\')"
        )],
        params,
        filters,
        limit,
        SymbolOrder::Name,
    )?;
    degraded |= prefix_matches.degraded;
    append_unique_symbols(&mut results, &mut seen, prefix_matches.results, limit);
    if results.len() >= limit {
        return Ok(VisibleSearchOutcome { results, degraded });
    }

    let contains = search_symbols_by_name_visible(conn, query, ctx, kind, language, paths, limit)?;
    degraded |= contains.degraded;
    append_unique_symbols(&mut results, &mut seen, contains.results, limit);
    if results.len() >= limit {
        return Ok(VisibleSearchOutcome { results, degraded });
    }

    let fts = search_symbols_fts_visible(conn, query, ctx, kind, language, paths, limit)?;
    degraded |= fts.degraded;
    append_unique_symbols(&mut results, &mut seen, fts.results, limit);

    Ok(VisibleSearchOutcome { results, degraded })
}

fn query_visible_symbols_by_conditions(
    conn: &mut Client,
    ctx: &Context,
    mut conditions: Vec<String>,
    mut params: Vec<PgParam>,
    filters: SymbolFilters<'_>,
    limit: usize,
    order: SymbolOrder,
) -> anyhow::Result<VisibleSearchOutcome<Symbol>> {
    let project_ids = visibility::visible_project_ids(ctx);
    if project_ids.is_empty() || limit == 0 {
        return Ok(VisibleSearchOutcome::ok(Vec::new()));
    }
    let project_placeholder = push_id_list_param(&mut params, &project_ids);
    conditions.push(format!("cs.project_id = ANY({project_placeholder})"));
    let bm25_index = matches!(&order, SymbolOrder::Bm25Score).then_some(SYMBOL_INDEX);
    let symbols = query_symbols_by_conditions(
        conn,
        conditions,
        params,
        filters,
        limit.max(FILTERED_FETCH_CAP),
        order,
    )
    .map_err(|error| match bm25_index {
        Some(index) => bm25_query_error(index, &error),
        None => database_query_error("visible symbol query", &error),
    })?;
    let mut symbols = visibility::filter_visible_symbols(conn, ctx, symbols)
        .map_err(|error| anyhow::anyhow!("visible symbol filtering failed: {error}"))?;
    symbols.truncate(limit);
    Ok(VisibleSearchOutcome::ok(symbols))
}

/// Full-text search for symbols using pg_search BM25.
pub fn search_text(
    conn: &mut Client,
    query: &str,
    project_id: &str,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> anyhow::Result<Vec<SearchResult>> {
    Ok(
        search_symbols_fts(conn, query, project_id, None, language, paths, limit)?
            .into_iter()
            .map(|s| s.to_brief())
            .collect(),
    )
}

pub fn search_text_visible(
    conn: &mut Client,
    query: &str,
    ctx: &Context,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> anyhow::Result<VisibleSearchOutcome<SearchResult>> {
    let results = search_symbols_fts_visible(conn, query, ctx, None, language, paths, limit)?;
    Ok(VisibleSearchOutcome {
        results: results.results.into_iter().map(|s| s.to_brief()).collect(),
        degraded: results.degraded,
    })
}
