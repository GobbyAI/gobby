use postgres::Client;

use crate::config::Context;

use super::common::{
    PgParam, SymbolFilters, param_refs, push_param, push_path_filter, push_symbol_filters,
    push_visible_project_file_filter, query_count, sanitize_pg_search_query,
};

pub fn count_text(
    conn: &mut Client,
    query: &str,
    project_id: &str,
    language: Option<&str>,
    paths: &[String],
) -> usize {
    if query.trim().is_empty() {
        return 0;
    }

    let bm25_query = sanitize_pg_search_query(query);
    if bm25_query.is_empty() {
        log::warn!("BM25 symbol count skipped because query contains no pg_search terms");
        return 0;
    }

    let mut params = Vec::new();
    let query_placeholder = push_param(&mut params, bm25_query);
    let project_placeholder = push_param(&mut params, project_id.to_string());
    let mut conditions = vec![
        format!(
            "(cs.name @@@ {q} OR cs.qualified_name @@@ {q} OR cs.signature @@@ {q} OR cs.docstring @@@ {q} OR cs.summary @@@ {q})",
            q = query_placeholder
        ),
        format!("cs.project_id = {project_placeholder}"),
    ];
    let path_filter_requires_post_filter = push_symbol_filters(
        &mut conditions,
        &mut params,
        "cs",
        SymbolFilters {
            kind: None,
            language,
            paths,
        },
    );
    if path_filter_requires_post_filter {
        return count_symbol_file_path_rows(conn, conditions, params, paths).unwrap_or(0);
    }
    let refs = param_refs(&params);
    let sql = format!(
        "SELECT COUNT(*)::BIGINT AS count
         FROM code_symbols cs
         JOIN code_indexed_files cf
           ON cf.project_id = cs.project_id AND cf.file_path = cs.file_path
         WHERE {}",
        conditions.join(" AND ")
    );
    match conn.query_one(&sql, &refs) {
        Ok(row) => row.try_get::<_, i64>("count").unwrap_or(0) as usize,
        Err(error) => {
            log::error!("BM25 symbol count failed; pg_search is required: {error}");
            0
        }
    }
}

/// Count matching content chunks using pg_search BM25.
pub fn count_content(
    conn: &mut Client,
    query: &str,
    project_id: &str,
    language: Option<&str>,
    paths: &[String],
) -> usize {
    if query.trim().is_empty() {
        return 0;
    }

    let bm25_query = sanitize_pg_search_query(query);
    if bm25_query.is_empty() {
        log::warn!("BM25 content count skipped because query contains no pg_search terms");
        return 0;
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
    let refs = param_refs(&params);
    let sql = format!(
        "SELECT COUNT(*)::BIGINT AS count
         FROM code_content_chunks c
         JOIN code_indexed_files cf
           ON cf.project_id = c.project_id AND cf.file_path = c.file_path
         WHERE {}",
        conditions.join(" AND ")
    );
    match conn.query_one(&sql, &refs) {
        Ok(row) => row.try_get::<_, i64>("count").unwrap_or(0) as usize,
        Err(error) => {
            log::error!("BM25 content count failed; pg_search is required: {error}");
            0
        }
    }
}

fn count_visible_symbols_by_conditions(
    conn: &mut Client,
    ctx: &Context,
    mut conditions: Vec<String>,
    mut params: Vec<PgParam>,
    language: Option<&str>,
    paths: &[String],
) -> Result<usize, postgres::Error> {
    let path_filter_requires_post_filter = push_symbol_filters(
        &mut conditions,
        &mut params,
        "cs",
        SymbolFilters {
            kind: None,
            language,
            paths,
        },
    );
    push_visible_project_file_filter(&mut conditions, &mut params, "cs", "cf", ctx);
    if path_filter_requires_post_filter {
        return count_symbol_file_path_rows(conn, conditions, params, paths);
    }
    let sql = format!(
        "SELECT COUNT(*)::BIGINT AS count
         FROM code_symbols cs
         JOIN code_indexed_files cf
           ON cf.project_id = cs.project_id AND cf.file_path = cs.file_path
         WHERE {}",
        conditions.join(" AND ")
    );
    query_count(conn, &sql, &params)
}

fn count_symbol_file_path_rows(
    conn: &mut Client,
    mut conditions: Vec<String>,
    mut params: Vec<PgParam>,
    paths: &[String],
) -> Result<usize, postgres::Error> {
    push_pg_regex_path_filter(&mut conditions, &mut params, "cs", paths);
    let sql = format!(
        "SELECT COUNT(*)::BIGINT AS count
         FROM code_symbols cs
         JOIN code_indexed_files cf
           ON cf.project_id = cs.project_id AND cf.file_path = cs.file_path
         WHERE {}",
        conditions.join(" AND ")
    );
    query_count(conn, &sql, &params)
}

fn push_pg_regex_path_filter(
    conditions: &mut Vec<String>,
    params: &mut Vec<PgParam>,
    alias: &str,
    paths: &[String],
) {
    if paths.is_empty() {
        return;
    }
    let regexes = paths
        .iter()
        .filter_map(|path| match glob_to_pg_regex(path) {
            Some(regex) => Some(regex),
            None => {
                log::warn!("omitting invalid post-query count path glob `{path}`");
                None
            }
        })
        .collect::<Vec<_>>();
    if regexes.is_empty() {
        conditions.push("FALSE".to_string());
        return;
    }
    let placeholder = push_param(params, regexes);
    conditions.push(format!("{alias}.file_path ~ ANY({placeholder}::TEXT[])"));
}

fn glob_to_pg_regex(pattern: &str) -> Option<String> {
    // Convert the post-query count path glob subset to PostgreSQL regex.
    //
    // Supported syntax is `*` for one path segment fragment, `**` for any
    // path depth, `?` for one non-slash character, and bracket classes.
    let mut regex = String::from("^");
    let mut chars = pattern.chars().peekable();
    while let Some(ch) = chars.next() {
        match ch {
            '*' => {
                let mut star_count = 1usize;
                while chars.peek() == Some(&'*') {
                    chars.next();
                    star_count += 1;
                }
                if star_count > 1 {
                    regex.push_str(".*");
                } else {
                    regex.push_str("[^/]*");
                }
            }
            '?' => regex.push_str("[^/]"),
            '[' => {
                regex.push('[');
                if chars.peek() == Some(&'!') {
                    chars.next();
                    regex.push('^');
                }
                let mut closed = false;
                for class_ch in chars.by_ref() {
                    regex.push(class_ch);
                    if class_ch == ']' {
                        closed = true;
                        break;
                    }
                }
                if !closed {
                    return None;
                }
            }
            '\\' => regex.push_str("\\\\"),
            '.' | '+' | '(' | ')' | '|' | '^' | '$' | '{' | '}' | ']' => {
                regex.push('\\');
                regex.push(ch);
            }
            ch => regex.push(ch),
        }
    }
    regex.push('$');
    Some(regex)
}

fn count_symbols_fts_visible(
    conn: &mut Client,
    bm25_query: &str,
    ctx: &Context,
    language: Option<&str>,
    paths: &[String],
) -> Result<usize, postgres::Error> {
    let mut params = Vec::new();
    let query_placeholder = push_param(&mut params, bm25_query.to_string());
    let conditions = vec![format!(
        "(cs.name @@@ {q} OR cs.qualified_name @@@ {q} OR cs.signature @@@ {q} OR cs.docstring @@@ {q} OR cs.summary @@@ {q})",
        q = query_placeholder
    )];
    count_visible_symbols_by_conditions(conn, ctx, conditions, params, language, paths)
}

fn push_content_filters(
    conditions: &mut Vec<String>,
    params: &mut Vec<PgParam>,
    alias: &str,
    language: Option<&str>,
    paths: &[String],
) {
    if let Some(lang) = language {
        let placeholder = push_param(params, lang.to_string());
        conditions.push(format!("{alias}.language = {placeholder}"));
    }
    push_path_filter(conditions, params, alias, paths);
}

fn count_visible_content_by_conditions(
    conn: &mut Client,
    ctx: &Context,
    mut conditions: Vec<String>,
    mut params: Vec<PgParam>,
    language: Option<&str>,
    paths: &[String],
) -> Result<usize, postgres::Error> {
    push_content_filters(&mut conditions, &mut params, "c", language, paths);
    push_visible_project_file_filter(&mut conditions, &mut params, "c", "cf", ctx);
    let sql = format!(
        "SELECT COUNT(*)::BIGINT AS count
         FROM code_content_chunks c
         JOIN code_indexed_files cf
           ON cf.project_id = c.project_id AND cf.file_path = c.file_path
         WHERE {}",
        conditions.join(" AND ")
    );
    query_count(conn, &sql, &params)
}

fn count_content_bm25_visible(
    conn: &mut Client,
    bm25_query: &str,
    ctx: &Context,
    language: Option<&str>,
    paths: &[String],
) -> Result<usize, postgres::Error> {
    let mut params = Vec::new();
    let query_placeholder = push_param(&mut params, bm25_query.to_string());
    let conditions = vec![format!("c.content @@@ {query_placeholder}")];
    count_visible_content_by_conditions(conn, ctx, conditions, params, language, paths)
}

pub fn count_text_visible(
    conn: &mut Client,
    query: &str,
    ctx: &Context,
    language: Option<&str>,
    paths: &[String],
) -> usize {
    if query.trim().is_empty() {
        return 0;
    }

    let bm25_query = sanitize_pg_search_query(query);
    if bm25_query.is_empty() {
        log::warn!("visible BM25 symbol count skipped because query contains no pg_search terms");
        return 0;
    }

    match count_symbols_fts_visible(conn, &bm25_query, ctx, language, paths) {
        Ok(count) => count,
        Err(error) => {
            log::error!("visible BM25 symbol count failed; pg_search is required: {error}");
            0
        }
    }
}

pub fn count_content_visible(
    conn: &mut Client,
    query: &str,
    ctx: &Context,
    language: Option<&str>,
    paths: &[String],
) -> usize {
    if query.trim().is_empty() {
        return 0;
    }

    let bm25_query = sanitize_pg_search_query(query);
    if bm25_query.is_empty() {
        log::warn!("visible BM25 content count skipped because query contains no pg_search terms");
        return 0;
    }

    match count_content_bm25_visible(conn, &bm25_query, ctx, language, paths) {
        Ok(count) => count,
        Err(error) => {
            log::error!("visible BM25 content count failed; pg_search is required: {error}");
            0
        }
    }
}

#[cfg(test)]
mod tests {
    use super::glob_to_pg_regex;

    #[test]
    fn glob_to_pg_regex_anchors_and_escapes_patterns() {
        assert_eq!(glob_to_pg_regex("*.rs").as_deref(), Some("^[^/]*\\.rs$"));
        assert_eq!(
            glob_to_pg_regex("src/foo?.[ch]").as_deref(),
            Some("^src/foo[^/]\\.[ch]$")
        );
        assert_eq!(
            glob_to_pg_regex("src/literal].rs").as_deref(),
            Some("^src/literal\\]\\.rs$")
        );
        assert_eq!(
            glob_to_pg_regex("src/**/*.rs").as_deref(),
            Some("^src/.*/[^/]*\\.rs$")
        );
        assert_eq!(
            glob_to_pg_regex("src/***/main.rs").as_deref(),
            Some("^src/.*/main\\.rs$")
        );
        assert_eq!(glob_to_pg_regex("src/["), None);
    }
}
