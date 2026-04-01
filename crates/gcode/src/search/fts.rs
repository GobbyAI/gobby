//! FTS5 query sanitization and execution against SQLite.
//! Ports logic from src/gobby/code_index/storage.py and searcher.py.

use rusqlite::Connection;

use crate::models::{ContentSearchHit, SearchResult, Symbol};

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
    limit: usize,
) -> Vec<Symbol> {
    let fts_query = sanitize_fts_query(query);
    if fts_query.is_empty() {
        return Vec::new();
    }

    let mut conditions = vec!["cs.project_id = ?2"];
    if kind.is_some() {
        conditions.push("cs.kind = ?3");
    }
    let where_clause = conditions.join(" AND ");

    let sql = format!(
        "SELECT cs.* FROM code_symbols_fts fts \
         JOIN code_symbols cs ON cs.rowid = fts.rowid \
         WHERE code_symbols_fts MATCH ?1 AND {where_clause} \
         ORDER BY rank \
         LIMIT ?4"
    );

    if let Some(k) = kind {
        let mut stmt = match conn.prepare(&sql) {
            Ok(s) => s,
            Err(_) => return Vec::new(),
        };
        stmt.query_map(
            rusqlite::params![&fts_query, project_id, k, limit as i64],
            Symbol::from_row,
        )
        .ok()
        .map(|rows| rows.filter_map(|r| r.ok()).collect::<Vec<_>>())
        .unwrap_or_default()
    } else {
        // Without kind filter, ?3 is limit
        let sql_no_kind = "SELECT cs.* FROM code_symbols_fts fts \
             JOIN code_symbols cs ON cs.rowid = fts.rowid \
             WHERE code_symbols_fts MATCH ?1 AND cs.project_id = ?2 \
             ORDER BY rank \
             LIMIT ?3"
            .to_string();
        let mut stmt = match conn.prepare(&sql_no_kind) {
            Ok(s) => s,
            Err(_) => return Vec::new(),
        };
        stmt.query_map(
            rusqlite::params![&fts_query, project_id, limit as i64],
            Symbol::from_row,
        )
        .ok()
        .map(|rows| rows.filter_map(|r| r.ok()).collect::<Vec<_>>())
        .unwrap_or_default()
    }
}

/// Fallback LIKE search on symbol names.
pub fn search_symbols_by_name(
    conn: &Connection,
    query: &str,
    project_id: &str,
    kind: Option<&str>,
    limit: usize,
) -> Vec<Symbol> {
    let pattern = format!("%{query}%");
    let mut sql = String::from(
        "SELECT * FROM code_symbols WHERE project_id = ?1 \
         AND (name LIKE ?2 OR qualified_name LIKE ?2)",
    );
    if kind.is_some() {
        sql.push_str(" AND kind = ?3");
    }
    sql.push_str(" ORDER BY name LIMIT ?4");

    if let Some(k) = kind {
        let mut stmt = match conn.prepare(&sql) {
            Ok(s) => s,
            Err(_) => return Vec::new(),
        };
        stmt.query_map(
            rusqlite::params![project_id, &pattern, k, limit as i64],
            Symbol::from_row,
        )
        .ok()
        .map(|rows| rows.filter_map(|r| r.ok()).collect())
        .unwrap_or_default()
    } else {
        let sql_no_kind = "SELECT * FROM code_symbols WHERE project_id = ?1 \
             AND (name LIKE ?2 OR qualified_name LIKE ?2) \
             ORDER BY name LIMIT ?3"
            .to_string();
        let mut stmt = match conn.prepare(&sql_no_kind) {
            Ok(s) => s,
            Err(_) => return Vec::new(),
        };
        stmt.query_map(
            rusqlite::params![project_id, &pattern, limit as i64],
            Symbol::from_row,
        )
        .ok()
        .map(|rows| rows.filter_map(|r| r.ok()).collect())
        .unwrap_or_default()
    }
}

/// Count matching symbols (FTS5 with LIKE fallback).
pub fn count_text(conn: &Connection, query: &str, project_id: &str) -> usize {
    let fts_query = sanitize_fts_query(query);
    if fts_query.is_empty() {
        return 0;
    }

    let count: Option<usize> = conn
        .query_row(
            "SELECT COUNT(*) FROM code_symbols_fts fts \
             JOIN code_symbols cs ON cs.rowid = fts.rowid \
             WHERE code_symbols_fts MATCH ?1 AND cs.project_id = ?2",
            rusqlite::params![&fts_query, project_id],
            |row| row.get(0),
        )
        .ok();

    if let Some(n) = count {
        if n > 0 {
            return n;
        }
    }

    // Fallback to LIKE count
    let pattern = format!("%{query}%");
    conn.query_row(
        "SELECT COUNT(*) FROM code_symbols WHERE project_id = ?1 \
         AND (name LIKE ?2 OR qualified_name LIKE ?2)",
        rusqlite::params![project_id, &pattern],
        |row| row.get(0),
    )
    .unwrap_or(0)
}

/// Count matching content chunks (FTS5 with LIKE fallback).
pub fn count_content(conn: &Connection, query: &str, project_id: &str) -> usize {
    if query.trim().is_empty() {
        return 0;
    }

    let safe_query = query.replace('"', "\"\"");

    let count: Option<usize> = conn
        .query_row(
            "SELECT COUNT(*) FROM code_content_fts fts \
             JOIN code_content_chunks c ON c.rowid = fts.rowid \
             WHERE code_content_fts MATCH ?1 AND c.project_id = ?2",
            rusqlite::params![format!("\"{safe_query}\""), project_id],
            |row| row.get(0),
        )
        .ok();

    if let Some(n) = count {
        if n > 0 {
            return n;
        }
    }

    // Fallback to LIKE count
    let like_query = format!("%{query}%");
    conn.query_row(
        "SELECT COUNT(*) FROM code_content_chunks \
         WHERE project_id = ?1 AND content LIKE ?2",
        rusqlite::params![project_id, &like_query],
        |row| row.get(0),
    )
    .unwrap_or(0)
}

/// Full-text search for symbols: FTS5 with LIKE fallback.
pub fn search_text(
    conn: &Connection,
    query: &str,
    project_id: &str,
    limit: usize,
) -> Vec<SearchResult> {
    let mut results = search_symbols_fts(conn, query, project_id, None, limit);
    if results.is_empty() {
        results = search_symbols_by_name(conn, query, project_id, None, limit);
    }
    results.into_iter().map(|s| s.to_brief()).collect()
}

/// Full-text search across file content chunks.
pub fn search_content(
    conn: &Connection,
    query: &str,
    project_id: &str,
    limit: usize,
) -> Vec<ContentSearchHit> {
    if query.trim().is_empty() {
        return Vec::new();
    }

    let safe_query = query.replace('"', "\"\"");

    // Try FTS5 first
    let sql = "SELECT c.file_path, c.line_start, c.line_end, c.language, \
               snippet(code_content_fts, 0, '>>>', '<<<', '...', 40) as snippet \
               FROM code_content_fts fts \
               JOIN code_content_chunks c ON c.rowid = fts.rowid \
               WHERE code_content_fts MATCH ?1 AND c.project_id = ?2 \
               ORDER BY rank LIMIT ?3";

    let fts_result: Result<Vec<ContentSearchHit>, rusqlite::Error> = (|| {
        let mut stmt = conn.prepare(sql)?;
        let rows = stmt.query_map(
            rusqlite::params![format!("\"{safe_query}\""), project_id, limit as i64],
            |row| {
                Ok(ContentSearchHit {
                    file_path: row.get("file_path")?,
                    line_start: row.get::<_, i64>("line_start")? as usize,
                    line_end: row.get::<_, i64>("line_end")? as usize,
                    snippet: row.get("snippet")?,
                    language: row.get("language")?,
                })
            },
        )?;
        Ok(rows.filter_map(|r| r.ok()).collect())
    })();

    match fts_result {
        Ok(hits) if !hits.is_empty() => hits,
        _ => {
            // Fallback to LIKE search
            let like_query = format!("%{query}%");
            let sql = "SELECT file_path, line_start, line_end, language, \
                       substr(content, max(1, instr(content, ?1) - 60), 120) as snippet \
                       FROM code_content_chunks \
                       WHERE project_id = ?2 AND content LIKE ?3 \
                       LIMIT ?4";
            let mut stmt = match conn.prepare(sql) {
                Ok(s) => s,
                Err(_) => return Vec::new(),
            };
            stmt.query_map(
                rusqlite::params![query, project_id, &like_query, limit as i64],
                |row| {
                    Ok(ContentSearchHit {
                        file_path: row.get("file_path")?,
                        line_start: row.get::<_, i64>("line_start")? as usize,
                        line_end: row.get::<_, i64>("line_end")? as usize,
                        snippet: row.get("snippet")?,
                        language: row.get("language")?,
                    })
                },
            )
            .ok()
            .map(|rows| rows.filter_map(|r| r.ok()).collect())
            .unwrap_or_default()
        }
    }
}
