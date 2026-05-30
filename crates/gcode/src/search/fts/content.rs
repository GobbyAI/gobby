use std::collections::HashSet;

use postgres::Client;

use crate::config::Context;
use crate::models::ContentSearchHit;
use crate::visibility;

use super::common::{
    escape_like, param_refs, push_param, push_path_filter, sanitize_pg_search_query,
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

pub fn search_content_visible(
    conn: &mut Client,
    query: &str,
    ctx: &Context,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> Vec<ContentSearchHit> {
    let mut results = Vec::new();
    let mut seen = HashSet::new();
    for project_id in visibility::visible_project_ids(ctx) {
        let hits = search_content(conn, query, &project_id, language, paths, limit);
        for hit in hits {
            let key = format!("{}:{}:{}", project_id, hit.file_path, hit.line_start);
            if seen.insert(key)
                && visibility::project_path_is_visible(conn, ctx, &project_id, &hit.file_path)
            {
                results.push(hit);
                if results.len() >= limit {
                    return results;
                }
            }
        }
    }
    results
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

pub(super) fn make_snippet(content: &str, query: &str) -> String {
    let tokens: Vec<String> = query
        .split_whitespace()
        .map(str::to_lowercase)
        .filter(|token| !token.is_empty())
        .collect();
    let (lower_content, lower_byte_to_original_char) = lowercase_with_original_char_map(content);
    let mut match_at = None;
    for token in tokens {
        if let Some(byte_index) = lower_content.find(&token) {
            match_at = lower_byte_to_original_char
                .get(byte_index)
                .copied()
                .or(Some(0));
            break;
        }
    }
    let match_at = match_at.unwrap_or(0);
    let start = match_at.saturating_sub(60);
    let end = (match_at + 120).min(content.chars().count());
    content.chars().skip(start).take(end - start).collect()
}

fn lowercase_with_original_char_map(content: &str) -> (String, Vec<usize>) {
    let mut lower = String::with_capacity(content.len());
    let mut lower_byte_to_original_char = Vec::with_capacity(content.len());
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
