use postgres::Client;

use super::GrepOptions;
use crate::config::{Context, ProjectIndexScope};
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
    let projects = match &ctx.index_scope {
        ProjectIndexScope::Single => vec![db::id_param(&ctx.project_id)?],
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } => vec![
            db::id_param(overlay_project_id)?,
            db::id_param(parent_project_id)?,
        ],
    };
    let rows = conn.query(
        "SELECT DISTINCT file_path
         FROM code_content_chunks
         WHERE project_id = ANY($1) AND strpos(content, $2) > 0",
        &[&projects, &literal],
    )?;
    let files = rows
        .iter()
        .map(|row| row.try_get::<_, String>(0))
        .collect::<Result<Vec<String>, postgres::Error>>()?;
    Ok(files)
}
