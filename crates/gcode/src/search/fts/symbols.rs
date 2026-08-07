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
) -> anyhow::Result<Vec<Symbol>> {
    let bm25_query = sanitize_pg_search_query(query);
    if bm25_query.is_empty() || limit == 0 {
        return Ok(Vec::new());
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
) -> anyhow::Result<Vec<Symbol>> {
    if query.trim().is_empty() || limit == 0 {
        return Ok(Vec::new());
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
    append_unique_symbols(&mut results, &mut seen, exact, limit);
    if results.len() >= limit {
        return Ok(results);
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
    append_unique_symbols(&mut results, &mut seen, prefix_matches, limit);
    if results.len() >= limit {
        return Ok(results);
    }

    let contains = search_symbols_by_name_visible(conn, query, ctx, kind, language, paths, limit)?;
    append_unique_symbols(&mut results, &mut seen, contains, limit);
    if results.len() >= limit {
        return Ok(results);
    }

    let fts = search_symbols_fts_visible(conn, query, ctx, kind, language, paths, limit)?;
    append_unique_symbols(&mut results, &mut seen, fts, limit);

    Ok(results)
}

fn query_visible_symbols_by_conditions(
    conn: &mut Client,
    ctx: &Context,
    mut conditions: Vec<String>,
    mut params: Vec<PgParam>,
    filters: SymbolFilters<'_>,
    limit: usize,
    order: SymbolOrder,
) -> anyhow::Result<Vec<Symbol>> {
    let project_ids = visibility::visible_project_ids(ctx);
    if project_ids.is_empty() || limit == 0 {
        return Ok(Vec::new());
    }
    if requires_explicit_project_filter(&ctx.database_url) {
        let project_placeholder = push_id_list_param(&mut params, &project_ids);
        conditions.push(format!("cs.project_id = ANY({project_placeholder})"));
    }
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
    Ok(symbols)
}

fn requires_explicit_project_filter(database_url: &str) -> bool {
    !gobby_core::postgres::is_managed_agent_connection(database_url)
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
) -> anyhow::Result<Vec<SearchResult>> {
    let results = search_symbols_fts_visible(conn, query, ctx, None, language, paths, limit)?;
    Ok(results.into_iter().map(|s| s.to_brief()).collect())
}

#[cfg(test)]
mod tests {
    use super::requires_explicit_project_filter;

    #[test]
    fn scoped_managed_connection_uses_authoritative_rls_project_filter() {
        let execution_id = "11111111-1111-4111-8111-111111112003";
        let compact_id = "11111111111141118111111111112003";
        let scoped = format!(
            "postgresql://gobby_agent_{compact_id}_1:secret@localhost/gobby?\
             application_name=gobby-agent-{execution_id}"
        );
        assert!(!requires_explicit_project_filter(&scoped));

        let operator = format!(
            "postgresql://gobby:secret@localhost/gobby?application_name=gobby-agent-{execution_id}"
        );
        assert!(requires_explicit_project_filter(&operator));
    }
}
