use postgres::Client;
use postgres::Row;

use crate::config::{Context, ProjectIndexScope};
use crate::models::ContentSearchHit;
use crate::visibility;
use crate::visibility::TOMBSTONE_LANGUAGE;

use super::common::{
    PgParam, bm25_score_expr, param_refs, push_id_list_param, push_id_param, push_param,
    push_path_filter, requires_explicit_project_filter, sanitize_pg_search_query, trusted_row_id,
};
use super::errors::{CONTENT_INDEX, bm25_query_error};

/// Build the visible-content query with the BM25 scan pinned to one execution.
///
/// The match set lives in a MATERIALIZED CTE because PostgreSQL evaluates such a
/// CTE exactly once. Left as a plain join, the planner is free to put the
/// ParadeDB scan on the inner side of a nested loop over the visible files, and
/// it has nothing to tell it apart: the RLS quals are opaque to the selectivity
/// estimator, so both sides estimate one row and the join order is effectively
/// arbitrary. On a project where it guesses wrong the scan re-runs once per
/// indexed file -- 173 executions and 19s on a 173-file repository (#22279).
///
/// Scoring changes with the shape. ParadeDB scores each heap-filter group it
/// pushes down and sums them, so today's plan -- which pushes the query once
/// with the RLS qual and again with the project equality the join derives --
/// returns roughly twice the single-evaluation BM25 score. The CTE has no join
/// to derive an equality from, so the score is evaluated once. Ranking is
/// unchanged except where the two legs' differing statistics reordered ties.
fn visible_content_sql(
    match_conditions: &[String],
    visible_files_sql: &str,
    limit_placeholder: &str,
) -> String {
    let score = bm25_score_expr(&trusted_row_id("c.id"));
    format!(
        "WITH matches AS MATERIALIZED (
             SELECT c.id,
                    c.project_id,
                    c.file_path,
                    c.content_hash,
                    c.line_start,
                    c.line_end,
                    c.language,
                    c.content,
                    {score} AS bm25_score
             FROM code_content_chunks c
             WHERE {}
         ),
         visible_files AS ({visible_files_sql})
         SELECT m.file_path,
                m.line_start::BIGINT AS line_start,
                m.line_end::BIGINT AS line_end,
                m.language,
                m.content
         FROM matches m
         JOIN visible_files vf
           ON vf.project_id = m.project_id
          AND vf.file_path = m.file_path
          AND vf.content_hash = m.content_hash
         ORDER BY m.bm25_score DESC, m.project_id ASC, m.id ASC
         LIMIT {limit_placeholder}",
        match_conditions.join(" AND ")
    )
}

pub fn search_content_visible(
    conn: &mut Client,
    query: &str,
    ctx: &Context,
    language: Option<&str>,
    paths: &[String],
    limit: usize,
) -> anyhow::Result<Vec<ContentSearchHit>> {
    if query.trim().is_empty() || limit == 0 {
        return Ok(Vec::new());
    }

    let bm25_query = sanitize_pg_search_query(query);
    if bm25_query.is_empty() {
        eprintln!(
            "gcode: visible content BM25 search skipped because query contains no pg_search terms; use `gcode grep` for exact text"
        );
        return Ok(Vec::new());
    }

    let mut params = Vec::new();
    let visible_files_sql = visible_files_sql(ctx, &mut params);
    let query_placeholder = push_param(&mut params, bm25_query);
    let mut conditions = vec![format!("c.content @@@ {query_placeholder}")];
    if let Some(lang) = language {
        let placeholder = push_param(&mut params, lang.to_string());
        conditions.push(format!("c.language = {placeholder}"));
    }
    push_path_filter(&mut conditions, &mut params, "c", paths);
    // The join to visible_files still scopes the result, but it no longer bounds
    // the materialized match set. On a managed connection RLS already does that;
    // anywhere else the query has to name the projects itself.
    if requires_explicit_project_filter(&ctx.database_url) {
        let project_ids = visibility::visible_project_ids(ctx);
        let placeholder = push_id_list_param(&mut params, &project_ids);
        conditions.push(format!("c.project_id = ANY({placeholder})"));
    }
    let limit_placeholder = push_param(&mut params, limit as i64);
    let refs = param_refs(&params);
    let sql = visible_content_sql(&conditions, &visible_files_sql, &limit_placeholder);

    let rows = conn
        .query(&sql, &refs)
        .map_err(|error| bm25_query_error(CONTENT_INDEX, &error))?;
    Ok(content_hits_from_rows(&rows, query))
}

fn visible_files_sql(ctx: &Context, params: &mut Vec<PgParam>) -> String {
    let Some(machine_id) = visibility::local_machine_uuid_or_invisible() else {
        return "SELECT NULL::uuid AS project_id, NULL::text AS file_path,
                       NULL::text AS content_hash
                WHERE FALSE"
            .to_string();
    };
    let machine_placeholder = push_param(params, machine_id);
    match &ctx.index_scope {
        ProjectIndexScope::Single => {
            let project_placeholder = push_id_param(params, &ctx.project_id);
            let tombstone_placeholder = push_param(params, TOMBSTONE_LANGUAGE.to_string());
            format!(
                "SELECT f.project_id, f.file_path, f.content_hash
                 FROM code_indexed_file_states fs
                 JOIN code_indexed_files f
                   ON f.project_id = fs.project_id
                  AND f.file_path = fs.file_path
                  AND f.content_hash = fs.content_hash
                 WHERE fs.machine_id = {machine_placeholder}
                   AND fs.project_id = {project_placeholder}
                   AND f.language != {tombstone_placeholder}"
            )
        }
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } => {
            let overlay_placeholder = push_id_param(params, overlay_project_id);
            let parent_placeholder = push_id_param(params, parent_project_id);
            let tombstone_placeholder = push_param(params, TOMBSTONE_LANGUAGE.to_string());
            format!(
                "SELECT f.project_id, f.file_path, f.content_hash
                 FROM code_indexed_file_states fs
                 JOIN code_indexed_files f
                   ON f.project_id = fs.project_id
                  AND f.file_path = fs.file_path
                  AND f.content_hash = fs.content_hash
                 WHERE fs.machine_id = {machine_placeholder}
                   AND fs.project_id = {overlay_placeholder}
                   AND f.language != {tombstone_placeholder}
                 UNION ALL
                 SELECT pf.project_id, pf.file_path, pf.content_hash
                 FROM code_indexed_file_states pfs
                 JOIN code_indexed_files pf
                   ON pf.project_id = pfs.project_id
                  AND pf.file_path = pfs.file_path
                  AND pf.content_hash = pfs.content_hash
                 WHERE pfs.machine_id = {machine_placeholder}
                   AND pfs.project_id = {parent_placeholder}
                   AND pf.language != {tombstone_placeholder}
                   AND NOT EXISTS (
                       SELECT 1 FROM code_indexed_file_states shadow
                       WHERE shadow.machine_id = {machine_placeholder}
                         AND shadow.project_id = {overlay_placeholder}
                         AND shadow.file_path = pfs.file_path
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

#[cfg(test)]
mod tests {
    use super::*;

    fn assert_uses_pdb_score(sql: &str) {
        assert!(sql.contains("pdb.score(c.id)"));
        assert!(!sql.contains("pg_search.score"));
    }

    #[test]
    fn visible_content_scores_once_in_a_materialized_match_set() {
        let sql = visible_content_sql(
            &["c.content @@@ $1".to_string()],
            "SELECT f.project_id, f.file_path, f.content_hash FROM code_indexed_files f",
            "$2",
        );

        // MATERIALIZED is what makes the ParadeDB scan run exactly once instead
        // of once per visible file; a plain CTE leaves that to the planner.
        assert!(sql.contains("WITH matches AS MATERIALIZED ("), "{sql}");
        assert!(sql.contains("pdb.score(c.id) AS bm25_score"), "{sql}");
        assert_eq!(sql.matches("c.content @@@ $1").count(), 1, "{sql}");
        assert_uses_pdb_score(&sql);
    }

    #[test]
    fn visible_content_orders_by_the_materialized_score_with_stable_tiebreakers() {
        let sql = visible_content_sql(&["c.content @@@ $1".to_string()], "SELECT 1", "$2");

        assert!(
            sql.contains("ORDER BY m.bm25_score DESC, m.project_id ASC, m.id ASC"),
            "{sql}"
        );
        // Scoring the outer join rows again would re-introduce the per-file rescan.
        assert!(!sql.contains("ORDER BY pdb.score"), "{sql}");
    }
}
