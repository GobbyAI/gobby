use postgres::Client;
use postgres::Row;

use crate::config::{Context, ProjectIndexScope};
use crate::models::ContentSearchHit;
use crate::visibility::TOMBSTONE_LANGUAGE;

use super::common::{
    PgParam, escape_like, param_refs, push_param, push_path_filter, sanitize_pg_search_query,
};

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
         ORDER BY pg_search.score(c.id) DESC, c.id ASC
         LIMIT {limit_placeholder}",
        conditions.join(" AND ")
    );

    let hits = match conn.query(&sql, &refs) {
        Ok(rows) => content_hits_from_rows(&rows, query),
        Err(error) => {
            eprintln!("gcode: content BM25 search failed; falling back to ILIKE: {error}");
            Vec::new()
        }
    };

    if !hits.is_empty() {
        return hits;
    }

    search_content_like(conn, query, project_id, language, paths, limit)
}

pub fn search_content_visible(
    conn: &mut Client,
    query: &str,
    ctx: &Context,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> Vec<ContentSearchHit> {
    if query.trim().is_empty() || limit == 0 {
        return Vec::new();
    }

    let bm25_query = sanitize_pg_search_query(query);
    let mut params = Vec::new();
    let visible_files_sql = visible_files_sql(ctx, &mut params);
    let query_placeholder = push_param(&mut params, bm25_query);
    let mut conditions = vec![format!("c.content @@@ {query_placeholder}")];
    if let Some(lang) = language {
        let placeholder = push_param(&mut params, lang.to_string());
        conditions.push(format!("c.language = {placeholder}"));
    }
    push_path_filter(&mut conditions, &mut params, "c", paths);
    let limit_placeholder = push_param(&mut params, limit as i64);
    let refs = param_refs(&params);
    let sql = format!(
        "WITH visible_files AS ({visible_files_sql})
         SELECT c.file_path,
                c.line_start::BIGINT AS line_start,
                c.line_end::BIGINT AS line_end,
                c.language,
                c.content
         FROM code_content_chunks c
         JOIN visible_files vf
           ON vf.project_id = c.project_id AND vf.file_path = c.file_path
         WHERE {}
         ORDER BY pg_search.score(c.id) DESC, c.project_id ASC, c.id ASC
         LIMIT {limit_placeholder}",
        conditions.join(" AND ")
    );

    let hits = match conn.query(&sql, &refs) {
        Ok(rows) => content_hits_from_rows(&rows, query),
        Err(error) => {
            eprintln!("gcode: visible content BM25 search failed; falling back to ILIKE: {error}");
            Vec::new()
        }
    };

    if !hits.is_empty() {
        return hits;
    }

    search_content_visible_like(conn, query, ctx, language, paths, limit)
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
        format!("c.content ILIKE {like_placeholder} ESCAPE '\\'"),
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

    match conn.query(&sql, &refs) {
        Ok(rows) => content_hits_from_rows(&rows, query),
        Err(error) => {
            eprintln!("gcode: content ILIKE search failed: {error}");
            Vec::new()
        }
    }
}

fn search_content_visible_like(
    conn: &mut Client,
    query: &str,
    ctx: &Context,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> Vec<ContentSearchHit> {
    let escaped_query = escape_like(query);
    let like_query = format!("%{escaped_query}%");
    let mut params = Vec::new();
    let visible_files_sql = visible_files_sql(ctx, &mut params);
    let like_placeholder = push_param(&mut params, like_query);
    let mut conditions = vec![format!("c.content ILIKE {like_placeholder} ESCAPE '\\'")];
    if let Some(lang) = language {
        let placeholder = push_param(&mut params, lang.to_string());
        conditions.push(format!("c.language = {placeholder}"));
    }
    push_path_filter(&mut conditions, &mut params, "c", paths);
    let limit_placeholder = push_param(&mut params, limit as i64);
    let refs = param_refs(&params);
    let sql = format!(
        "WITH visible_files AS ({visible_files_sql})
         SELECT c.file_path,
                c.line_start::BIGINT AS line_start,
                c.line_end::BIGINT AS line_end,
                c.language,
                c.content
         FROM code_content_chunks c
         JOIN visible_files vf
           ON vf.project_id = c.project_id AND vf.file_path = c.file_path
         WHERE {}
         ORDER BY c.file_path ASC, c.line_start ASC
         LIMIT {limit_placeholder}",
        conditions.join(" AND ")
    );

    match conn.query(&sql, &refs) {
        Ok(rows) => content_hits_from_rows(&rows, query),
        Err(error) => {
            eprintln!("gcode: visible content ILIKE search failed: {error}");
            Vec::new()
        }
    }
}

fn visible_files_sql(ctx: &Context, params: &mut Vec<PgParam>) -> String {
    match &ctx.index_scope {
        ProjectIndexScope::Single => {
            let project_placeholder = push_param(params, ctx.project_id.clone());
            let tombstone_placeholder = push_param(params, TOMBSTONE_LANGUAGE.to_string());
            format!(
                "SELECT file_path, project_id
                 FROM code_indexed_files
                 WHERE project_id = {project_placeholder}
                   AND language != {tombstone_placeholder}"
            )
        }
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } => {
            let overlay_placeholder = push_param(params, overlay_project_id.clone());
            let parent_placeholder = push_param(params, parent_project_id.clone());
            let tombstone_placeholder = push_param(params, TOMBSTONE_LANGUAGE.to_string());
            format!(
                "SELECT file_path, project_id
                 FROM code_indexed_files
                 WHERE project_id = {overlay_placeholder}
                   AND language != {tombstone_placeholder}
                 UNION ALL
                 SELECT pf.file_path, pf.project_id
                 FROM code_indexed_files pf
                 WHERE pf.project_id = {parent_placeholder}
                   AND pf.language != {tombstone_placeholder}
                   AND NOT EXISTS (
                       SELECT 1 FROM code_indexed_files of
                       WHERE of.project_id = {overlay_placeholder}
                         AND of.file_path = pf.file_path
                   )"
            )
        }
    }
}

fn content_hits_from_rows(rows: &[Row], query: &str) -> Vec<ContentSearchHit> {
    let tokens = snippet_tokens(query);
    rows.iter()
        .filter_map(|row| {
            let content: String = row.try_get("content").ok()?;
            let line_start = usize::try_from(row.try_get::<_, i64>("line_start").ok()?).ok()?;
            let line_end = usize::try_from(row.try_get::<_, i64>("line_end").ok()?).ok()?;
            Some(ContentSearchHit {
                file_path: row.try_get("file_path").ok()?,
                line_start,
                line_end,
                snippet: make_snippet_with_tokens(&content, &tokens),
                language: row.try_get("language").ok()?,
            })
        })
        .collect()
}

#[cfg(test)]
pub(super) fn make_snippet(content: &str, query: &str) -> String {
    let tokens = snippet_tokens(query);
    make_snippet_with_tokens(content, &tokens)
}

fn snippet_tokens(query: &str) -> Vec<String> {
    query
        .split_whitespace()
        .map(str::to_lowercase)
        .filter(|token| !token.is_empty())
        .collect()
}

fn make_snippet_with_tokens(content: &str, tokens: &[String]) -> String {
    let (lower_content, lower_byte_to_original_char) = lowercase_with_original_char_map(content);
    let match_at = tokens
        .iter()
        .filter_map(|token| {
            lower_content
                .find(token)
                .and_then(|byte_index| lower_byte_to_original_char.get(byte_index).copied())
        })
        .min();
    let match_at = match_at.unwrap_or(0);
    let start = match_at.saturating_sub(60);
    let content_len = content.chars().count();
    let end = match_at.saturating_add(120).min(content_len);
    content.chars().skip(start).take(end - start).collect()
}

fn lowercase_with_original_char_map(content: &str) -> (String, Vec<usize>) {
    // Unicode lowercase expansion can produce more bytes than the source.
    let reserve = content.len().saturating_mul(2);
    let mut lower = String::with_capacity(reserve);
    let mut lower_byte_to_original_char = Vec::with_capacity(reserve);
    for (original_char_index, ch) in content.chars().enumerate() {
        for lower_ch in ch.to_lowercase() {
            let mut buf = [0; 4];
            let encoded = lower_ch.encode_utf8(&mut buf);
            lower_byte_to_original_char
                .extend(std::iter::repeat_n(original_char_index, encoded.len()));
            lower.push(lower_ch);
        }
    }
    (lower, lower_byte_to_original_char)
}
