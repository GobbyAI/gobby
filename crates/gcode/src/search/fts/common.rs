use std::collections::HashSet;

use postgres::Client;
use postgres::types::ToSql;

use crate::config::{Context, ProjectIndexScope};
use crate::db;
use crate::models::Symbol;
use crate::visibility;

// Keep BM25 query sanitation centralized in gobby-core so gcode and gwiki
// escape pg_search's DSL identically.
pub use gobby_core::search::sanitize_pg_search_query;

pub(super) type PgParam = Box<dyn ToSql + Sync>;

pub(super) const BM25_SCORE_FUNCTION: &str = "pdb.score";

pub(super) fn bm25_score_expr(row_id: &str) -> String {
    format!("{BM25_SCORE_FUNCTION}({row_id})")
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedGraphSymbol {
    pub id: String,
    pub display_name: String,
}

#[derive(Debug, Clone, Copy, Default)]
pub(super) struct SymbolFilters<'a> {
    pub(super) kind: Option<&'a str>,
    pub(super) language: Option<&'a str>,
    pub(super) paths: &'a [String],
}

#[derive(Debug, Clone)]
pub(super) enum SymbolOrder {
    Bm25Score,
    Name,
    ExactCaseFirst(String),
}

impl SymbolOrder {
    fn sql(&self) -> String {
        match self {
            Self::Bm25Score => format!("{} DESC, cs.id ASC", bm25_score_expr("cs.id")),
            Self::Name => "cs.name ASC, cs.file_path ASC, cs.line_start ASC".to_string(),
            Self::ExactCaseFirst(query_param) => format!(
                "CASE WHEN cs.name = {q} OR cs.qualified_name = {q} THEN 0 ELSE 1 END,
                 cs.file_path ASC,
                 cs.line_start ASC",
                q = query_param
            ),
        }
    }
}

pub const FILTERED_FETCH_CAP: usize = 10_000;

pub(super) fn push_param<T>(params: &mut Vec<PgParam>, value: T) -> String
where
    T: ToSql + Sync + 'static,
{
    params.push(Box::new(value));
    format!("${}", params.len())
}

pub(super) fn param_refs(params: &[PgParam]) -> Vec<&(dyn ToSql + Sync)> {
    params
        .iter()
        .map(|param| param.as_ref() as &(dyn ToSql + Sync))
        .collect()
}

pub(super) fn query_count(
    conn: &mut Client,
    sql: &str,
    params: &[PgParam],
) -> Result<usize, postgres::Error> {
    let refs = param_refs(params);
    let row = conn.query_one(sql, &refs)?;
    Ok(row.try_get::<_, i64>("count")? as usize)
}

pub(super) fn push_visible_project_file_filter(
    conditions: &mut Vec<String>,
    params: &mut Vec<PgParam>,
    row_alias: &str,
    indexed_file_alias: &str,
    ctx: &Context,
) {
    let tombstone = push_param(params, visibility::TOMBSTONE_LANGUAGE.to_string());
    conditions.push(format!("{indexed_file_alias}.language != {tombstone}"));

    match &ctx.index_scope {
        ProjectIndexScope::Single => {
            let project = push_param(params, ctx.project_id.clone());
            conditions.push(format!("{row_alias}.project_id = {project}"));
        }
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } => {
            let overlay = push_param(params, overlay_project_id.clone());
            let parent = push_param(params, parent_project_id.clone());
            conditions.push(format!(
                "({row_alias}.project_id = {overlay}
                  OR (
                      {row_alias}.project_id = {parent}
                      AND NOT EXISTS (
                          SELECT 1 FROM code_indexed_files shadow
                          WHERE shadow.project_id = {overlay}
                            AND shadow.file_path = {row_alias}.file_path
                      )
                  ))"
            ));
        }
    }
}

/// Escape LIKE wildcards (`%`, `_`) and the backslash escape char itself.
pub(super) fn escape_like(s: &str) -> String {
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
pub(super) fn glob_to_like_prefix(pattern: &str) -> Option<String> {
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

pub(super) fn has_glob_meta(path: &str) -> bool {
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

pub(super) fn path_like_prefixes(paths: &[String]) -> Option<Vec<String>> {
    if paths.is_empty() {
        return Some(Vec::new());
    }

    let mut prefixes = Vec::with_capacity(paths.len());
    for path in paths {
        prefixes.push(glob_to_like_prefix(path)?);
    }
    Some(prefixes)
}

pub fn path_filter_falls_back(paths: &[String]) -> bool {
    !paths.is_empty() && path_like_prefixes(paths).is_none()
}

pub(super) fn push_path_filter(
    conditions: &mut Vec<String>,
    params: &mut Vec<PgParam>,
    alias: &str,
    paths: &[String],
) -> bool {
    let requires_post_filter = !paths.is_empty();
    let Some(prefixes) = path_like_prefixes(paths) else {
        for path in paths
            .iter()
            .filter(|path| glob_to_like_prefix(path).is_none())
        {
            log::warn!(
                "omitting SQL path filter for alias `{alias}` because path filter `{path}` cannot be converted to a LIKE prefix; relying on post-query glob matching",
            );
        }
        return requires_post_filter;
    };
    if prefixes.is_empty() {
        return requires_post_filter;
    }

    let predicates = prefixes
        .into_iter()
        .map(|prefix| {
            let placeholder = push_param(params, prefix);
            format!("{alias}.file_path LIKE {placeholder} ESCAPE '\\'")
        })
        .collect::<Vec<_>>();
    conditions.push(format!("({})", predicates.join(" OR ")));
    requires_post_filter
}

pub(super) fn push_symbol_filters(
    conditions: &mut Vec<String>,
    params: &mut Vec<PgParam>,
    alias: &str,
    filters: SymbolFilters<'_>,
) -> bool {
    if let Some(kind) = filters.kind {
        let placeholder = push_param(params, kind.to_string());
        conditions.push(format!("{alias}.kind = {placeholder}"));
    }
    if let Some(language) = filters.language {
        let placeholder = push_param(params, language.to_string());
        conditions.push(format!("{alias}.language = {placeholder}"));
    }
    push_path_filter(conditions, params, alias, filters.paths)
}

pub(super) fn symbols_matching_paths(
    symbols: impl IntoIterator<Item = Symbol>,
    paths: &[String],
) -> Vec<Symbol> {
    let patterns = match compile_patterns(paths) {
        Ok(patterns) => patterns,
        Err(error) => {
            log::warn!("invalid post-query symbol path filter: {error}");
            return Vec::new();
        }
    };
    symbols
        .into_iter()
        .filter(|symbol| {
            patterns.is_empty()
                || patterns
                    .iter()
                    .any(|pattern| pattern.matches(&symbol.file_path))
        })
        .collect()
}

pub(super) fn append_unique_symbols(
    out: &mut Vec<Symbol>,
    seen: &mut HashSet<String>,
    symbols: Vec<Symbol>,
    limit: usize,
) {
    if limit == 0 {
        return;
    }
    for symbol in symbols {
        if seen.insert(symbol.id.clone()) {
            out.push(symbol);
            if out.len() >= limit {
                return;
            }
        }
    }
}

pub(super) fn query_symbols_by_conditions(
    conn: &mut Client,
    mut conditions: Vec<String>,
    mut params: Vec<PgParam>,
    filters: SymbolFilters<'_>,
    limit: usize,
    order: SymbolOrder,
) -> Vec<Symbol> {
    let path_filter_fallback = push_symbol_filters(&mut conditions, &mut params, "cs", filters);
    let query_limit = if path_filter_fallback {
        limit.max(FILTERED_FETCH_CAP)
    } else {
        limit
    };
    let limit_placeholder = push_param(&mut params, query_limit as i64);
    let where_clause = if conditions.is_empty() {
        "TRUE".to_string()
    } else {
        conditions.join(" AND ")
    };
    let columns = db::symbol_select_columns("cs");
    let sql = format!(
        "SELECT {columns}
         FROM code_symbols cs
         JOIN code_indexed_files cf
           ON cf.project_id = cs.project_id AND cf.file_path = cs.file_path
         WHERE {where_clause}
         ORDER BY {order_by}
         LIMIT {limit_placeholder}",
        order_by = order.sql()
    );
    let refs = param_refs(&params);
    let mut symbols = match conn.query(&sql, &refs) {
        Ok(rows) => rows
            .iter()
            .filter_map(|row| Symbol::from_row(row).ok())
            .collect(),
        Err(error) => {
            log::error!("symbol query failed: {error}");
            return Vec::new();
        }
    };
    if path_filter_fallback {
        symbols = symbols_matching_paths(symbols, filters.paths);
        symbols.truncate(limit);
    }
    symbols
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bm25_score_expression_uses_pdb_score() {
        let sql = bm25_score_expr("cs.id");

        assert_eq!(sql, "pdb.score(cs.id)");
        assert!(!sql.contains("pg_search.score"));
    }

    #[test]
    fn symbol_bm25_order_uses_pdb_score() {
        let sql = SymbolOrder::Bm25Score.sql();

        assert_eq!(sql, "pdb.score(cs.id) DESC, cs.id ASC");
        assert!(!sql.contains("pg_search.score"));
    }
}
