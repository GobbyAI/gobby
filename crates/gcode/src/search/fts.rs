//! FTS5 query sanitization and execution against SQLite.
//! Ports logic from src/gobby/code_index/storage.py and searcher.py.

use rusqlite::Connection;

use crate::models::{ContentSearchHit, SearchResult, Symbol};

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
    if let Some(k) = kind {
        conditions.push("cs.kind = ?".to_string());
        params.push(Box::new(k.to_string()));
    }
    if let Some(like) = path.and_then(glob_to_like_prefix) {
        conditions.push("cs.file_path LIKE ?".to_string());
        params.push(Box::new(like));
    }
    params.push(Box::new(limit as i64));

    let where_clause = conditions.join(" AND ");
    let sql = format!(
        "SELECT cs.* FROM code_symbols_fts fts \
         JOIN code_symbols cs ON cs.rowid = fts.rowid \
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
    path: Option<&str>,
    limit: usize,
) -> Vec<Symbol> {
    let escaped_query = escape_like(query);
    let pattern = format!("%{escaped_query}%");
    let mut conditions = vec![
        "project_id = ?".to_string(),
        "(name LIKE ? ESCAPE '\\' OR qualified_name LIKE ? ESCAPE '\\')".to_string(),
    ];
    let mut params: Vec<Box<dyn rusqlite::types::ToSql>> = vec![
        Box::new(project_id.to_string()),
        Box::new(pattern.clone()),
        Box::new(pattern),
    ];
    if let Some(k) = kind {
        conditions.push("kind = ?".to_string());
        params.push(Box::new(k.to_string()));
    }
    if let Some(like) = path.and_then(glob_to_like_prefix) {
        conditions.push("file_path LIKE ?".to_string());
        params.push(Box::new(like));
    }
    params.push(Box::new(limit as i64));

    let where_clause = conditions.join(" AND ");
    let sql = format!("SELECT * FROM code_symbols WHERE {where_clause} ORDER BY name LIMIT ?");

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

/// Resolve user input to a symbol name for graph queries.
///
/// Tries exact match first, then falls back to LIKE matching.
/// Returns `(resolved_name, suggestions)` — suggestions populated when no exact match.
pub fn resolve_symbol_name(
    conn: &Connection,
    input: &str,
    project_id: &str,
) -> (Option<String>, Vec<String>) {
    // 1. Exact match on name
    let exact: Option<String> = conn
        .query_row(
            "SELECT name FROM code_symbols WHERE project_id = ? AND name = ? LIMIT 1",
            rusqlite::params![project_id, input],
            |row| row.get(0),
        )
        .ok();
    if let Some(name) = exact {
        return (Some(name), vec![]);
    }

    // 2. LIKE fallback — collect top matches as suggestions.
    //    Escape %/_/\ in input and use ESCAPE clause so symbol names containing
    //    underscores (common in snake_case) match literally, not as wildcards.
    let pattern = format!("%{}%", escape_like(input));
    let mut stmt = match conn.prepare(
        "SELECT DISTINCT name FROM code_symbols \
         WHERE project_id = ? AND (name LIKE ? ESCAPE '\\' OR qualified_name LIKE ? ESCAPE '\\') \
         ORDER BY name LIMIT 5",
    ) {
        Ok(s) => s,
        Err(_) => return (None, vec![]),
    };
    let names: Vec<String> = stmt
        .query_map(rusqlite::params![project_id, &pattern, &pattern], |row| {
            row.get(0)
        })
        .ok()
        .map(|rows| rows.filter_map(|r| r.ok()).collect())
        .unwrap_or_default();

    if names.len() == 1 {
        return (Some(names[0].clone()), vec![]);
    } else if !names.is_empty() {
        let first = names[0].clone();
        return (Some(first), names[1..].to_vec());
    }

    // 3. FTS5 fallback — search across names, signatures, docstrings
    let fts_results = search_symbols_fts(conn, input, project_id, None, None, 5);
    let mut seen = std::collections::HashSet::new();
    let fts_names: Vec<String> = fts_results
        .iter()
        .filter_map(|s| {
            if seen.insert(s.name.clone()) {
                Some(s.name.clone())
            } else {
                None
            }
        })
        .collect();

    if fts_names.len() == 1 {
        (Some(fts_names[0].clone()), vec![])
    } else if fts_names.is_empty() {
        (None, vec![])
    } else {
        let first = fts_names[0].clone();
        (Some(first), fts_names[1..].to_vec())
    }
}

/// Count matching symbols (FTS5 with LIKE fallback).
pub fn count_text(conn: &Connection, query: &str, project_id: &str, path: Option<&str>) -> usize {
    let fts_query = sanitize_fts_query(query);
    if fts_query.is_empty() {
        return 0;
    }

    let mut conditions = vec!["cs.project_id = ?".to_string()];
    let mut params: Vec<Box<dyn rusqlite::types::ToSql>> = vec![
        Box::new(fts_query.clone()),
        Box::new(project_id.to_string()),
    ];
    if let Some(like) = path.and_then(glob_to_like_prefix) {
        conditions.push("cs.file_path LIKE ?".to_string());
        params.push(Box::new(like));
    }
    let where_clause = conditions.join(" AND ");
    let sql = format!(
        "SELECT COUNT(*) FROM code_symbols_fts fts \
         JOIN code_symbols cs ON cs.rowid = fts.rowid \
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
        "project_id = ?".to_string(),
        "(name LIKE ? ESCAPE '\\' OR qualified_name LIKE ? ESCAPE '\\')".to_string(),
    ];
    let mut params: Vec<Box<dyn rusqlite::types::ToSql>> = vec![
        Box::new(project_id.to_string()),
        Box::new(pattern.clone()),
        Box::new(pattern),
    ];
    if let Some(like) = path.and_then(glob_to_like_prefix) {
        conditions.push("file_path LIKE ?".to_string());
        params.push(Box::new(like));
    }
    let where_clause = conditions.join(" AND ");
    let sql = format!("SELECT COUNT(*) FROM code_symbols WHERE {where_clause}");
    let param_refs: Vec<&dyn rusqlite::types::ToSql> = params.iter().map(|p| p.as_ref()).collect();
    conn.query_row(&sql, param_refs.as_slice(), |row| row.get(0))
        .unwrap_or(0)
}

/// Count matching content chunks (FTS5 with LIKE fallback).
pub fn count_content(
    conn: &Connection,
    query: &str,
    project_id: &str,
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
    if let Some(like) = path.and_then(glob_to_like_prefix) {
        conditions.push("c.file_path LIKE ?".to_string());
        params.push(Box::new(like));
    }
    let where_clause = conditions.join(" AND ");
    let sql = format!(
        "SELECT COUNT(*) FROM code_content_fts fts \
         JOIN code_content_chunks c ON c.rowid = fts.rowid \
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
        "project_id = ?".to_string(),
        "content LIKE ? ESCAPE '\\'".to_string(),
    ];
    let mut params: Vec<Box<dyn rusqlite::types::ToSql>> =
        vec![Box::new(project_id.to_string()), Box::new(like_query)];
    if let Some(like) = path.and_then(glob_to_like_prefix) {
        conditions.push("file_path LIKE ?".to_string());
        params.push(Box::new(like));
    }
    let where_clause = conditions.join(" AND ");
    let sql = format!("SELECT COUNT(*) FROM code_content_chunks WHERE {where_clause}");
    let param_refs: Vec<&dyn rusqlite::types::ToSql> = params.iter().map(|p| p.as_ref()).collect();
    conn.query_row(&sql, param_refs.as_slice(), |row| row.get(0))
        .unwrap_or(0)
}

/// Full-text search for symbols: FTS5 with LIKE fallback.
pub fn search_text(
    conn: &Connection,
    query: &str,
    project_id: &str,
    path: Option<&str>,
    limit: usize,
) -> Vec<SearchResult> {
    let mut results = search_symbols_fts(conn, query, project_id, None, path, limit);
    if results.is_empty() {
        results = search_symbols_by_name(conn, query, project_id, None, path, limit);
    }
    results.into_iter().map(|s| s.to_brief()).collect()
}

/// Full-text search across file content chunks.
pub fn search_content(
    conn: &Connection,
    query: &str,
    project_id: &str,
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
                "project_id = ?".to_string(),
                "content LIKE ? ESCAPE '\\'".to_string(),
            ];
            let mut params: Vec<Box<dyn rusqlite::types::ToSql>> = vec![
                Box::new(query.to_string()),
                Box::new(project_id.to_string()),
                Box::new(like_query),
            ];
            if let Some(like) = path.and_then(glob_to_like_prefix) {
                conditions.push("file_path LIKE ?".to_string());
                params.push(Box::new(like));
            }
            params.push(Box::new(limit as i64));
            let where_clause = conditions.join(" AND ");
            let sql = format!(
                "SELECT file_path, line_start, line_end, language, \
                 substr(content, max(1, instr(content, ?) - 60), 120) as snippet \
                 FROM code_content_chunks \
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
