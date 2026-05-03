//! FTS5 query sanitization and execution against SQLite.
//! Ports logic from src/gobby/code_index/storage.py and searcher.py.

use std::collections::HashSet;

use rusqlite::Connection;

use crate::models::{ContentSearchHit, SearchResult, Symbol};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedGraphSymbol {
    pub id: String,
    pub display_name: String,
}

#[derive(Debug, Clone, Copy, Default)]
struct SymbolFilters<'a> {
    kind: Option<&'a str>,
    language: Option<&'a str>,
    path: Option<&'a str>,
}

/// Escape LIKE wildcards (`%`, `_`) and the backslash escape char itself.
/// Must be paired with `ESCAPE '\'` in the SQL for SQLite to honor it.
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
/// Returns the literal prefix before the first wildcard character, or None if empty.
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

fn push_symbol_filters(
    conditions: &mut Vec<String>,
    params: &mut Vec<Box<dyn rusqlite::types::ToSql>>,
    alias: &str,
    filters: SymbolFilters<'_>,
) {
    if let Some(k) = filters.kind {
        conditions.push(format!("{alias}.kind = ?"));
        params.push(Box::new(k.to_string()));
    }
    if let Some(lang) = filters.language {
        conditions.push(format!("{alias}.language = ?"));
        params.push(Box::new(lang.to_string()));
    }
    if let Some(like) = filters.path.and_then(glob_to_like_prefix) {
        conditions.push(format!("{alias}.file_path LIKE ?"));
        params.push(Box::new(like));
    }
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

fn query_symbols_by_name_predicate(
    conn: &Connection,
    project_id: &str,
    predicate: &str,
    predicate_values: Vec<String>,
    filters: SymbolFilters<'_>,
    limit: usize,
) -> Vec<Symbol> {
    let mut conditions = vec!["cs.project_id = ?".to_string(), predicate.to_string()];
    let mut params: Vec<Box<dyn rusqlite::types::ToSql>> = vec![Box::new(project_id.to_string())];
    for value in predicate_values {
        params.push(Box::new(value));
    }
    push_symbol_filters(&mut conditions, &mut params, "cs", filters);
    params.push(Box::new(limit as i64));

    let where_clause = conditions.join(" AND ");
    let sql = format!(
        "SELECT cs.* FROM code_symbols cs \
         JOIN code_indexed_files cf \
              ON cf.project_id = cs.project_id AND cf.file_path = cs.file_path \
         WHERE {where_clause} \
         ORDER BY cs.file_path, cs.line_start \
         LIMIT ?"
    );

    let param_refs: Vec<&dyn rusqlite::types::ToSql> = params.iter().map(|p| p.as_ref()).collect();
    let mut stmt = match conn.prepare(&sql) {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };
    stmt.query_map(param_refs.as_slice(), Symbol::from_row)
        .ok()
        .map(|rows| rows.filter_map(|r| r.ok()).collect())
        .unwrap_or_default()
}

/// Sanitize user input for FTS5 queries.
/// Strips special characters and quotes each token for safe matching.
pub fn sanitize_fts_query(query: &str) -> String {
    let cleaned: String = query
        .chars()
        .filter(|c| c.is_alphanumeric() || *c == ' ' || *c == '_')
        .collect();
    let tokens: Vec<&str> = cleaned
        .split_whitespace()
        .filter(|t| !t.is_empty())
        .collect();
    if tokens.is_empty() {
        return String::new();
    }
    tokens
        .iter()
        .map(|t| format!("\"{t}\""))
        .collect::<Vec<_>>()
        .join(" ")
}

/// FTS5 search across symbol names, signatures, docstrings, and summaries.
pub fn search_symbols_fts(
    conn: &Connection,
    query: &str,
    project_id: &str,
    kind: Option<&str>,
    language: Option<&str>,
    path: Option<&str>,
    limit: usize,
) -> Vec<Symbol> {
    let fts_query = sanitize_fts_query(query);
    if fts_query.is_empty() {
        return Vec::new();
    }

    let mut conditions = vec!["cs.project_id = ?".to_string()];
    let mut params: Vec<Box<dyn rusqlite::types::ToSql>> =
        vec![Box::new(fts_query), Box::new(project_id.to_string())];
    let filters = SymbolFilters {
        kind,
        language,
        path,
    };
    push_symbol_filters(&mut conditions, &mut params, "cs", filters);
    params.push(Box::new(limit as i64));

    let where_clause = conditions.join(" AND ");
    let sql = format!(
        "SELECT cs.* FROM code_symbols_fts fts \
         JOIN code_symbols cs ON cs.rowid = fts.rowid \
         JOIN code_indexed_files cf \
              ON cf.project_id = cs.project_id AND cf.file_path = cs.file_path \
         WHERE code_symbols_fts MATCH ? AND {where_clause} \
         ORDER BY rank LIMIT ?"
    );

    let param_refs: Vec<&dyn rusqlite::types::ToSql> = params.iter().map(|p| p.as_ref()).collect();
    let mut stmt = match conn.prepare(&sql) {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };
    stmt.query_map(param_refs.as_slice(), Symbol::from_row)
        .ok()
        .map(|rows| rows.filter_map(|r| r.ok()).collect::<Vec<_>>())
        .unwrap_or_default()
}

/// Fallback LIKE search on symbol names.
pub fn search_symbols_by_name(
    conn: &Connection,
    query: &str,
    project_id: &str,
    kind: Option<&str>,
    language: Option<&str>,
    path: Option<&str>,
    limit: usize,
) -> Vec<Symbol> {
    let escaped_query = escape_like(query);
    let pattern = format!("%{escaped_query}%");
    let mut conditions = vec![
        "cs.project_id = ?".to_string(),
        "(cs.name LIKE ? ESCAPE '\\' OR cs.qualified_name LIKE ? ESCAPE '\\')".to_string(),
    ];
    let mut params: Vec<Box<dyn rusqlite::types::ToSql>> = vec![
        Box::new(project_id.to_string()),
        Box::new(pattern.clone()),
        Box::new(pattern),
    ];
    let filters = SymbolFilters {
        kind,
        language,
        path,
    };
    push_symbol_filters(&mut conditions, &mut params, "cs", filters);
    params.push(Box::new(limit as i64));

    let where_clause = conditions.join(" AND ");
    let sql = format!(
        "SELECT cs.* FROM code_symbols cs \
         JOIN code_indexed_files cf \
              ON cf.project_id = cs.project_id AND cf.file_path = cs.file_path \
         WHERE {where_clause} \
         ORDER BY cs.name, cs.file_path, cs.line_start LIMIT ?"
    );

    let param_refs: Vec<&dyn rusqlite::types::ToSql> = params.iter().map(|p| p.as_ref()).collect();
    let mut stmt = match conn.prepare(&sql) {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };
    stmt.query_map(param_refs.as_slice(), Symbol::from_row)
        .ok()
        .map(|rows| rows.filter_map(|r| r.ok()).collect())
        .unwrap_or_default()
}

pub fn search_symbols_exact_first(
    conn: &Connection,
    query: &str,
    project_id: &str,
    kind: Option<&str>,
    language: Option<&str>,
    path: Option<&str>,
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
        path,
    };

    let exact = query_symbols_by_name_predicate(
        conn,
        project_id,
        "(cs.name = ? OR cs.qualified_name = ?)",
        vec![query.to_string(), query.to_string()],
        filters,
        limit,
    );
    append_unique_symbols(&mut results, &mut seen, exact, limit);
    if results.len() >= limit {
        return results;
    }

    let ci_exact = query_symbols_by_name_predicate(
        conn,
        project_id,
        "(lower(cs.name) = lower(?) OR lower(cs.qualified_name) = lower(?))",
        vec![query.to_string(), query.to_string()],
        filters,
        limit,
    );
    append_unique_symbols(&mut results, &mut seen, ci_exact, limit);
    if results.len() >= limit {
        return results;
    }

    let prefix = format!("{}%", escape_like(query));
    let prefix_matches = query_symbols_by_name_predicate(
        conn,
        project_id,
        "(cs.name LIKE ? ESCAPE '\\' OR cs.qualified_name LIKE ? ESCAPE '\\')",
        vec![prefix.clone(), prefix],
        filters,
        limit,
    );
    append_unique_symbols(&mut results, &mut seen, prefix_matches, limit);
    if results.len() >= limit {
        return results;
    }

    let contains = search_symbols_by_name(conn, query, project_id, kind, language, path, limit);
    append_unique_symbols(&mut results, &mut seen, contains, limit);
    if results.len() >= limit {
        return results;
    }

    let fts = search_symbols_fts(conn, query, project_id, kind, language, path, limit);
    append_unique_symbols(&mut results, &mut seen, fts, limit);

    results
}

fn exact_symbol_matches(
    conn: &Connection,
    project_id: &str,
    column: &str,
    input: &str,
    limit: usize,
) -> Vec<Symbol> {
    let sql = format!(
        "SELECT * FROM code_symbols \
         WHERE project_id = ?1 AND {column} = ?2 \
         ORDER BY file_path, line_start \
         LIMIT ?3"
    );
    let mut stmt = match conn.prepare(&sql) {
        Ok(stmt) => stmt,
        Err(_) => return Vec::new(),
    };
    stmt.query_map(
        rusqlite::params![project_id, input, limit as i64],
        Symbol::from_row,
    )
    .ok()
    .map(|rows| rows.filter_map(|row| row.ok()).collect())
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
    conn: &Connection,
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

    let like_matches = search_symbols_by_name(conn, input, project_id, None, None, None, 6);
    let (resolved, suggestions) = resolve_from_candidates(like_matches);
    if resolved.is_some() || !suggestions.is_empty() {
        return (resolved, suggestions);
    }

    let fts_results = search_symbols_fts(conn, input, project_id, None, None, None, 6);
    resolve_from_candidates(fts_results)
}

/// Count matching symbols (FTS5 with LIKE fallback).
pub fn count_text(
    conn: &Connection,
    query: &str,
    project_id: &str,
    language: Option<&str>,
    path: Option<&str>,
) -> usize {
    let fts_query = sanitize_fts_query(query);
    if fts_query.is_empty() {
        return 0;
    }

    let mut conditions = vec!["cs.project_id = ?".to_string()];
    let mut params: Vec<Box<dyn rusqlite::types::ToSql>> = vec![
        Box::new(fts_query.clone()),
        Box::new(project_id.to_string()),
    ];
    push_symbol_filters(
        &mut conditions,
        &mut params,
        "cs",
        SymbolFilters {
            kind: None,
            language,
            path,
        },
    );
    let where_clause = conditions.join(" AND ");
    let sql = format!(
        "SELECT COUNT(*) FROM code_symbols_fts fts \
         JOIN code_symbols cs ON cs.rowid = fts.rowid \
         JOIN code_indexed_files cf \
              ON cf.project_id = cs.project_id AND cf.file_path = cs.file_path \
         WHERE code_symbols_fts MATCH ? AND {where_clause}"
    );
    let param_refs: Vec<&dyn rusqlite::types::ToSql> = params.iter().map(|p| p.as_ref()).collect();

    let count: Option<usize> = conn
        .query_row(&sql, param_refs.as_slice(), |row| row.get(0))
        .ok();

    if let Some(n) = count {
        if n > 0 {
            return n;
        }
    }

    // Fallback to LIKE count
    let escaped_query = escape_like(query);
    let pattern = format!("%{escaped_query}%");
    let mut conditions = vec![
        "cs.project_id = ?".to_string(),
        "(cs.name LIKE ? ESCAPE '\\' OR cs.qualified_name LIKE ? ESCAPE '\\')".to_string(),
    ];
    let mut params: Vec<Box<dyn rusqlite::types::ToSql>> = vec![
        Box::new(project_id.to_string()),
        Box::new(pattern.clone()),
        Box::new(pattern),
    ];
    push_symbol_filters(
        &mut conditions,
        &mut params,
        "cs",
        SymbolFilters {
            kind: None,
            language,
            path,
        },
    );
    let where_clause = conditions.join(" AND ");
    let sql = format!(
        "SELECT COUNT(*) FROM code_symbols cs \
         JOIN code_indexed_files cf \
              ON cf.project_id = cs.project_id AND cf.file_path = cs.file_path \
         WHERE {where_clause}"
    );
    let param_refs: Vec<&dyn rusqlite::types::ToSql> = params.iter().map(|p| p.as_ref()).collect();
    conn.query_row(&sql, param_refs.as_slice(), |row| row.get(0))
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;

    fn setup_conn() -> Connection {
        let conn = Connection::open_in_memory().expect("open sqlite");
        conn.execute_batch(
            "CREATE TABLE code_symbols (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                name TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                kind TEXT NOT NULL,
                language TEXT NOT NULL,
                byte_start INTEGER NOT NULL,
                byte_end INTEGER NOT NULL,
                line_start INTEGER NOT NULL,
                line_end INTEGER NOT NULL,
                signature TEXT,
                docstring TEXT,
                parent_symbol_id TEXT,
                content_hash TEXT,
                summary TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE code_indexed_files (
                project_id TEXT NOT NULL,
                file_path TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE code_symbols_fts USING fts5(name, signature, docstring, summary);",
        )
        .expect("create schema");
        conn
    }

    fn insert_symbol(
        conn: &Connection,
        id: &str,
        file_path: &str,
        name: &str,
        qualified_name: &str,
        summary: Option<&str>,
    ) {
        insert_symbol_with(
            conn,
            id,
            file_path,
            name,
            qualified_name,
            "function",
            "python",
            summary,
        );
    }

    fn insert_symbol_with(
        conn: &Connection,
        id: &str,
        file_path: &str,
        name: &str,
        qualified_name: &str,
        kind: &str,
        language: &str,
        summary: Option<&str>,
    ) {
        conn.execute(
            "INSERT INTO code_symbols (
                id, project_id, file_path, name, qualified_name, kind, language,
                byte_start, byte_end, line_start, line_end, signature, docstring,
                parent_symbol_id, content_hash, summary, created_at, updated_at
            ) VALUES (
                ?1, 'proj', ?2, ?3, ?4, ?5, ?6,
                0, 10, 1, 1, '', '', NULL, '', ?7, '', ''
            )",
            rusqlite::params![id, file_path, name, qualified_name, kind, language, summary],
        )
        .expect("insert symbol");
        let rowid = conn.last_insert_rowid();
        conn.execute(
            "INSERT INTO code_indexed_files (project_id, file_path) VALUES ('proj', ?1)",
            rusqlite::params![file_path],
        )
        .expect("insert indexed file");
        conn.execute(
            "INSERT INTO code_symbols_fts(rowid, name, signature, docstring, summary)
             VALUES (?1, ?2, '', '', ?3)",
            rusqlite::params![rowid, name, summary.unwrap_or_default()],
        )
        .expect("insert fts row");
    }

    #[test]
    fn resolve_graph_symbol_by_exact_name() {
        let conn = setup_conn();
        insert_symbol(&conn, "sym-1", "src/main.py", "foo", "foo", Some("helper"));

        let (resolved, suggestions) = resolve_graph_symbol(&conn, "foo", "proj");

        assert!(suggestions.is_empty());
        assert_eq!(
            resolved,
            Some(ResolvedGraphSymbol {
                id: "sym-1".to_string(),
                display_name: "foo".to_string(),
            })
        );
    }

    #[test]
    fn resolve_graph_symbol_uses_like_fallback() {
        let conn = setup_conn();
        insert_symbol(
            &conn,
            "sym-1",
            "src/mailer.py",
            "render_email",
            "render_email",
            Some("helper"),
        );

        let (resolved, suggestions) = resolve_graph_symbol(&conn, "render", "proj");

        assert!(suggestions.is_empty());
        assert_eq!(resolved.map(|symbol| symbol.id), Some("sym-1".to_string()));
    }

    #[test]
    fn resolve_graph_symbol_uses_fts_fallback() {
        let conn = setup_conn();
        insert_symbol(
            &conn,
            "sym-1",
            "src/mailer.py",
            "send_email",
            "send_email",
            Some("mailer helper"),
        );

        let (resolved, suggestions) = resolve_graph_symbol(&conn, "mailer helper", "proj");

        assert!(suggestions.is_empty());
        assert_eq!(resolved.map(|symbol| symbol.id), Some("sym-1".to_string()));
    }

    #[test]
    fn resolve_graph_symbol_is_fail_closed_on_ambiguity() {
        let conn = setup_conn();
        insert_symbol(&conn, "sym-1", "src/a.py", "foo", "foo", Some("a"));
        insert_symbol(&conn, "sym-2", "src/b.py", "foo", "foo", Some("b"));

        let (resolved, suggestions) = resolve_graph_symbol(&conn, "foo", "proj");

        assert!(resolved.is_none());
        assert_eq!(suggestions.len(), 2);
    }

    #[test]
    fn search_symbols_exact_first_prioritizes_exact_name() {
        let conn = setup_conn();
        insert_symbol(
            &conn,
            "sym-1",
            "src/outline_helpers.py",
            "outline_helper",
            "outline_helper",
            None,
        );
        insert_symbol(
            &conn,
            "sym-2",
            "src/commands.py",
            "outline",
            "outline",
            None,
        );

        let results = search_symbols_exact_first(&conn, "outline", "proj", None, None, None, 10);

        assert_eq!(results[0].id, "sym-2");
    }

    #[test]
    fn search_symbols_exact_first_respects_kind_language_and_path() {
        let conn = setup_conn();
        insert_symbol_with(
            &conn,
            "sym-1",
            "src/commands.py",
            "outline",
            "outline",
            "function",
            "python",
            None,
        );
        insert_symbol_with(
            &conn,
            "sym-2",
            "src/commands.rs",
            "outline",
            "outline",
            "function",
            "rust",
            None,
        );

        let results = search_symbols_exact_first(
            &conn,
            "outline",
            "proj",
            Some("function"),
            Some("rust"),
            Some("src/**/*.rs"),
            10,
        );

        assert_eq!(results.len(), 1);
        assert_eq!(results[0].id, "sym-2");
    }
}

/// Count matching content chunks (FTS5 with LIKE fallback).
pub fn count_content(
    conn: &Connection,
    query: &str,
    project_id: &str,
    language: Option<&str>,
    path: Option<&str>,
) -> usize {
    if query.trim().is_empty() {
        return 0;
    }

    let safe_query = query.replace('"', "\"\"");
    let fts_match = format!("\"{safe_query}\"");

    let mut conditions = vec!["c.project_id = ?".to_string()];
    let mut params: Vec<Box<dyn rusqlite::types::ToSql>> =
        vec![Box::new(fts_match), Box::new(project_id.to_string())];
    if let Some(lang) = language {
        conditions.push("c.language = ?".to_string());
        params.push(Box::new(lang.to_string()));
    }
    if let Some(like) = path.and_then(glob_to_like_prefix) {
        conditions.push("c.file_path LIKE ?".to_string());
        params.push(Box::new(like));
    }
    let where_clause = conditions.join(" AND ");
    let sql = format!(
        "SELECT COUNT(*) FROM code_content_fts fts \
         JOIN code_content_chunks c ON c.rowid = fts.rowid \
         JOIN code_indexed_files cf \
              ON cf.project_id = c.project_id AND cf.file_path = c.file_path \
         WHERE code_content_fts MATCH ? AND {where_clause}"
    );
    let param_refs: Vec<&dyn rusqlite::types::ToSql> = params.iter().map(|p| p.as_ref()).collect();

    let count: Option<usize> = conn
        .query_row(&sql, param_refs.as_slice(), |row| row.get(0))
        .ok();

    if let Some(n) = count {
        if n > 0 {
            return n;
        }
    }

    // Fallback to LIKE count
    let escaped_query = escape_like(query);
    let like_query = format!("%{escaped_query}%");
    let mut conditions = vec![
        "c.project_id = ?".to_string(),
        "c.content LIKE ? ESCAPE '\\'".to_string(),
    ];
    let mut params: Vec<Box<dyn rusqlite::types::ToSql>> =
        vec![Box::new(project_id.to_string()), Box::new(like_query)];
    if let Some(lang) = language {
        conditions.push("c.language = ?".to_string());
        params.push(Box::new(lang.to_string()));
    }
    if let Some(like) = path.and_then(glob_to_like_prefix) {
        conditions.push("c.file_path LIKE ?".to_string());
        params.push(Box::new(like));
    }
    let where_clause = conditions.join(" AND ");
    let sql = format!(
        "SELECT COUNT(*) FROM code_content_chunks c \
         JOIN code_indexed_files cf \
              ON cf.project_id = c.project_id AND cf.file_path = c.file_path \
         WHERE {where_clause}"
    );
    let param_refs: Vec<&dyn rusqlite::types::ToSql> = params.iter().map(|p| p.as_ref()).collect();
    conn.query_row(&sql, param_refs.as_slice(), |row| row.get(0))
        .unwrap_or(0)
}

/// Full-text search for symbols: FTS5 with LIKE fallback.
pub fn search_text(
    conn: &Connection,
    query: &str,
    project_id: &str,
    language: Option<&str>,
    path: Option<&str>,
    limit: usize,
) -> Vec<SearchResult> {
    let mut results = search_symbols_fts(conn, query, project_id, None, language, path, limit);
    if results.is_empty() {
        results = search_symbols_by_name(conn, query, project_id, None, language, path, limit);
    }
    results.into_iter().map(|s| s.to_brief()).collect()
}

/// Full-text search across file content chunks.
pub fn search_content(
    conn: &Connection,
    query: &str,
    project_id: &str,
    language: Option<&str>,
    path: Option<&str>,
    limit: usize,
) -> Vec<ContentSearchHit> {
    if query.trim().is_empty() {
        return Vec::new();
    }

    let safe_query = query.replace('"', "\"\"");
    let fts_match = format!("\"{safe_query}\"");

    // Try FTS5 first
    let mut conditions = vec!["c.project_id = ?".to_string()];
    let mut params: Vec<Box<dyn rusqlite::types::ToSql>> =
        vec![Box::new(fts_match), Box::new(project_id.to_string())];
    if let Some(lang) = language {
        conditions.push("c.language = ?".to_string());
        params.push(Box::new(lang.to_string()));
    }
    if let Some(like) = path.and_then(glob_to_like_prefix) {
        conditions.push("c.file_path LIKE ?".to_string());
        params.push(Box::new(like));
    }
    params.push(Box::new(limit as i64));
    let where_clause = conditions.join(" AND ");

    let sql = format!(
        "SELECT c.file_path, c.line_start, c.line_end, c.language, \
         snippet(code_content_fts, 0, '>>>', '<<<', '...', 40) as snippet \
         FROM code_content_fts fts \
         JOIN code_content_chunks c ON c.rowid = fts.rowid \
         JOIN code_indexed_files cf \
              ON cf.project_id = c.project_id AND cf.file_path = c.file_path \
         WHERE code_content_fts MATCH ? AND {where_clause} \
         ORDER BY rank LIMIT ?"
    );

    let param_refs: Vec<&dyn rusqlite::types::ToSql> = params.iter().map(|p| p.as_ref()).collect();
    let fts_result: Result<Vec<ContentSearchHit>, rusqlite::Error> = (|| {
        let mut stmt = conn.prepare(&sql)?;
        let rows = stmt.query_map(param_refs.as_slice(), |row| {
            Ok(ContentSearchHit {
                file_path: row.get("file_path")?,
                line_start: row.get::<_, i64>("line_start")? as usize,
                line_end: row.get::<_, i64>("line_end")? as usize,
                snippet: row.get("snippet")?,
                language: row.get("language")?,
            })
        })?;
        Ok(rows.filter_map(|r| r.ok()).collect())
    })();

    match fts_result {
        Ok(hits) if !hits.is_empty() => hits,
        _ => {
            // Fallback to LIKE search
            let escaped_query = escape_like(query);
            let like_query = format!("%{escaped_query}%");
            let mut conditions = vec![
                "c.project_id = ?".to_string(),
                "c.content LIKE ? ESCAPE '\\'".to_string(),
            ];
            let mut params: Vec<Box<dyn rusqlite::types::ToSql>> = vec![
                Box::new(query.to_string()),
                Box::new(project_id.to_string()),
                Box::new(like_query),
            ];
            if let Some(lang) = language {
                conditions.push("c.language = ?".to_string());
                params.push(Box::new(lang.to_string()));
            }
            if let Some(like) = path.and_then(glob_to_like_prefix) {
                conditions.push("c.file_path LIKE ?".to_string());
                params.push(Box::new(like));
            }
            params.push(Box::new(limit as i64));
            let where_clause = conditions.join(" AND ");
            let sql = format!(
                "SELECT c.file_path, c.line_start, c.line_end, c.language, \
                 substr(c.content, max(1, instr(c.content, ?) - 60), 120) as snippet \
                 FROM code_content_chunks c \
                 JOIN code_indexed_files cf \
                      ON cf.project_id = c.project_id AND cf.file_path = c.file_path \
                 WHERE {where_clause} LIMIT ?"
            );
            let param_refs: Vec<&dyn rusqlite::types::ToSql> =
                params.iter().map(|p| p.as_ref()).collect();
            let mut stmt = match conn.prepare(&sql) {
                Ok(s) => s,
                Err(_) => return Vec::new(),
            };
            stmt.query_map(param_refs.as_slice(), |row| {
                Ok(ContentSearchHit {
                    file_path: row.get("file_path")?,
                    line_start: row.get::<_, i64>("line_start")? as usize,
                    line_end: row.get::<_, i64>("line_end")? as usize,
                    snippet: row.get("snippet")?,
                    language: row.get("language")?,
                })
            })
            .ok()
            .map(|rows| rows.filter_map(|r| r.ok()).collect())
            .unwrap_or_default()
        }
    }
}

#[cfg(test)]
mod content_tests {
    use super::*;

    fn setup_conn() -> Connection {
        let conn = Connection::open_in_memory().expect("open sqlite");
        conn.execute_batch(
            "CREATE TABLE code_content_chunks (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                line_start INTEGER NOT NULL,
                line_end INTEGER NOT NULL,
                content TEXT NOT NULL,
                language TEXT,
                created_at TEXT
            );
            CREATE TABLE code_indexed_files (
                project_id TEXT NOT NULL,
                file_path TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE code_content_fts USING fts5(
                content, file_path, language,
                content='code_content_chunks', content_rowid='rowid'
            );",
        )
        .expect("create schema");
        conn
    }

    fn insert_chunk(conn: &Connection, file_path: &str, language: &str, content: &str) {
        conn.execute(
            "INSERT INTO code_content_chunks (
                id, project_id, file_path, chunk_index, line_start, line_end,
                content, language, created_at
             ) VALUES ('chunk-1', 'proj', ?1, 0, 1, 10, ?2, ?3, '')",
            rusqlite::params![file_path, content, language],
        )
        .expect("insert chunk");
        let rowid = conn.last_insert_rowid();
        conn.execute(
            "INSERT INTO code_content_fts(rowid, content, file_path, language)
             VALUES (?1, ?2, ?3, ?4)",
            rusqlite::params![rowid, content, file_path, language],
        )
        .expect("insert fts row");
    }

    #[test]
    fn search_content_excludes_orphan_chunks() {
        let conn = setup_conn();
        insert_chunk(&conn, "src/missing.rs", "rust", "pub fn outline() {}");

        let results = search_content(&conn, "outline", "proj", None, None, 10);

        assert!(results.is_empty());
    }

    #[test]
    fn search_content_returns_chunks_with_indexed_file_row() {
        let conn = setup_conn();
        insert_chunk(&conn, "src/lib.rs", "rust", "pub fn outline() {}");
        conn.execute(
            "INSERT INTO code_indexed_files (project_id, file_path) VALUES ('proj', 'src/lib.rs')",
            [],
        )
        .expect("insert indexed file");

        let results = search_content(&conn, "outline", "proj", Some("rust"), None, 10);

        assert_eq!(results.len(), 1);
        assert_eq!(results[0].file_path, "src/lib.rs");
    }
}
