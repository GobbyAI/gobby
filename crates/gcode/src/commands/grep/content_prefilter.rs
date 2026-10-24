use postgres::Client;

use super::GrepOptions;
use crate::config::Context;
use crate::db;

/// The literal every matching line contains, when grep can prefilter on it.
///
/// Only case-sensitive, non-regex patterns qualify. `-w` stays sound because a
/// word match still contains the literal. Case-insensitive matching is excluded:
/// Postgres `lower()` and Rust's Unicode case folding disagree on some
/// characters, so a substring test could drop a match.
pub(super) fn content_literal<'a>(options: &GrepOptions<'a>) -> Option<&'a str> {
    let pattern = options.pattern;
    if options.ignore_case || pattern.is_empty() || pattern.contains('\n') {
        return None;
    }
    let is_literal = options.fixed_strings || regex::escape(pattern) == pattern;
    is_literal.then_some(pattern)
}

/// Files in the grep scope with a chunk that contains the literal. This is a
/// superset of the matching files: visibility and path checks still run on the
/// loaded rows, and every chunk of a listed file loads, so context lines survive.
pub(super) fn literal_files(
    conn: &mut Client,
    ctx: &Context,
    literal: &str,
) -> anyhow::Result<Vec<String>> {
    let selectors = crate::visibility::visible_selectors_sql(ctx, "$1", "$2");
    let rows = conn.query(
        &format!(
            "SELECT DISTINCT c.file_path FROM code_content_chunks c
                  JOIN ({selectors}) visible ON visible.project_id = c.project_id
                   AND visible.file_path = c.file_path AND visible.content_hash = c.content_hash
                  WHERE strpos(c.content, $3) > 0"
        ),
        &[
            &db::id_param(&ctx.machine_id)?,
            &db::id_param(crate::visibility::scope_project_id(ctx))?,
            &literal,
        ],
    )?;
    let files = rows
        .iter()
        .map(|row| row.try_get::<_, String>(0))
        .collect::<Result<Vec<String>, postgres::Error>>()?;
    Ok(files)
}
