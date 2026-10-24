use std::collections::HashSet;

use postgres::Client;
use uuid::Uuid;

use crate::config::{Context, ProjectIndexScope};
use crate::db;
use crate::models::Symbol;

pub(crate) mod catalog;
pub mod graph;
mod selectors;

pub(crate) use selectors::{
    file_selectors_sql, scope_project_id, selector_condition, visible_selectors_sql,
};

pub use catalog::{tombstone_count, visible_kinds, visible_tree};

pub const TOMBSTONE_LANGUAGE: &str = "__gcode_deleted__";
pub const TOMBSTONE_HASH: &str = "__gcode_tombstone__";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VisibleFile {
    pub file_path: String,
    pub language: String,
    pub symbol_count: i64,
    pub content_hash: String,
}

pub fn is_tombstone_language(language: &str) -> bool {
    language == TOMBSTONE_LANGUAGE
}

pub fn visible_project_ids(ctx: &Context) -> Vec<String> {
    match &ctx.index_scope {
        ProjectIndexScope::Single => {
            vec![ctx.project_id.clone()]
        }
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } => vec![overlay_project_id.clone(), parent_project_id.clone()],
    }
}

pub fn context_for_source_project(ctx: &Context, source_project_id: &str) -> Context {
    let mut scoped = ctx.clone();
    scoped.project_id = source_project_id.to_string();
    scoped.project_root = match &ctx.index_scope {
        ProjectIndexScope::Overlay {
            overlay_project_id,
            overlay_root,
            parent_project_id: _parent_project_id,
            parent_root: _parent_root,
        } if source_project_id == overlay_project_id => overlay_root.clone(),
        ProjectIndexScope::Overlay {
            parent_project_id,
            parent_root,
            ..
        } if source_project_id == parent_project_id => parent_root.clone(),
        _ => ctx.project_root.clone(),
    };
    scoped.index_scope = ProjectIndexScope::Single;
    scoped
}

/// Parse a project id for binding against a uuid column in a bool-returning
/// visibility check. A non-uuid id cannot exist in the hub, so callers treat a
/// parse failure exactly like a no-row query result.
pub(super) fn project_uuid_or_invisible(project_id: &str) -> Option<Uuid> {
    db::id_param(project_id).ok()
}

/// Parse a machine id for binding against a uuid column, as `project_uuid_or_invisible`.
pub(crate) fn machine_uuid_or_invisible(machine_id: &str) -> Option<Uuid> {
    db::id_param(machine_id).ok()
}

pub(super) fn local_machine_uuid() -> anyhow::Result<Uuid> {
    let machine_id = gobby_core::machine::read_local_machine_id()?;
    db::id_param(&machine_id)
}

pub(crate) fn local_machine_uuid_or_invisible() -> Option<Uuid> {
    match local_machine_uuid() {
        Ok(machine_id) => Some(machine_id),
        Err(error) => {
            log::warn!("local machine identity is unavailable; results are invisible: {error:#}");
            None
        }
    }
}

/// SQL condition restricting `row_alias` rows to the content versions selected
/// by the local machine's file-state. `hash_column` names the row's
/// content-hash column and `machine_placeholder` the caller's bound
/// machine-uuid parameter.
pub(super) fn machine_state_condition(
    row_alias: &str,
    hash_column: &str,
    machine_placeholder: &str,
) -> String {
    format!(
        "EXISTS (
            SELECT 1 FROM code_indexed_file_states vfs
            WHERE vfs.machine_id = {machine_placeholder}
              AND vfs.project_id = {row_alias}.project_id
              AND vfs.file_path = {row_alias}.file_path
              AND vfs.content_hash = {row_alias}.{hash_column}
        )"
    )
}

/// Content hash the local machine's file-state selects for `file_path`, or
/// `None` when the machine is unresolvable, the file is untracked here, or the
/// active version is a tombstone.
pub(crate) fn local_active_content_hash(
    conn: &mut Client,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<Option<String>> {
    let Some(machine_id) = local_machine_uuid_or_invisible() else {
        return Ok(None);
    };
    let Some(project_id) = project_uuid_or_invisible(project_id) else {
        return Ok(None);
    };
    let row = conn.query_opt(
        "SELECT fs.content_hash
         FROM code_indexed_file_states fs
         JOIN code_indexed_files f
           ON f.project_id = fs.project_id
          AND f.file_path = fs.file_path
          AND f.content_hash = fs.content_hash
         WHERE fs.machine_id = $1
           AND fs.project_id = $2
           AND fs.file_path = $3
           AND f.language != $4",
        &[&machine_id, &project_id, &file_path, &TOMBSTONE_LANGUAGE],
    )?;
    Ok(row.map(|row| row.try_get("content_hash")).transpose()?)
}

pub fn indexed_file_exists(conn: &mut Client, ctx: &Context, file_path: &str) -> bool {
    let (Some(machine), Some(project)) = (
        machine_uuid_or_invisible(&ctx.machine_id),
        project_uuid_or_invisible(scope_project_id(ctx)),
    ) else {
        return false;
    };
    let selectors = visible_selectors_sql(ctx, "$1", "$2");
    conn.query_one(
        &format!("SELECT EXISTS(SELECT 1 FROM ({selectors}) visible WHERE file_path = $3)"),
        &[&machine, &project, &file_path],
    )
    .and_then(|row| row.try_get(0))
    .unwrap_or(false)
}

pub fn content_chunks_exist(conn: &mut Client, ctx: &Context, file_path: &str) -> bool {
    let (Some(machine), Some(project)) = (
        machine_uuid_or_invisible(&ctx.machine_id),
        project_uuid_or_invisible(scope_project_id(ctx)),
    ) else {
        return false;
    };
    let selectors = visible_selectors_sql(ctx, "$1", "$2");
    let visible = selector_condition(&selectors, "c", "content_hash");
    conn.query_one(
        &format!("SELECT EXISTS(SELECT 1 FROM code_content_chunks c WHERE c.file_path = $3 AND {visible})"),
        &[&machine, &project, &file_path],
    ).and_then(|row| row.try_get(0)).unwrap_or(false)
}

pub fn symbol_is_visible(conn: &mut Client, ctx: &Context, symbol: &Symbol) -> bool {
    let (Some(machine), Some(project), Some(source)) = (
        machine_uuid_or_invisible(&ctx.machine_id),
        project_uuid_or_invisible(scope_project_id(ctx)),
        project_uuid_or_invisible(&symbol.project_id),
    ) else {
        return false;
    };
    let selectors = visible_selectors_sql(ctx, "$1", "$2");
    conn.query_one(
        &format!(
            "SELECT EXISTS(SELECT 1 FROM ({selectors}) visible
                  WHERE project_id = $3 AND file_path = $4 AND content_hash = $5)"
        ),
        &[
            &machine,
            &project,
            &source,
            &symbol.file_path,
            &symbol.file_content_hash,
        ],
    )
    .and_then(|row| row.try_get(0))
    .unwrap_or(false)
}

#[cfg(test)]
thread_local! {
    static PER_ID_SYMBOL_LOOKUPS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
    static BATCH_SYMBOL_LOOKUPS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

#[cfg(test)]
pub(crate) fn reset_symbol_lookup_counts() {
    PER_ID_SYMBOL_LOOKUPS.with(|count| count.set(0));
    BATCH_SYMBOL_LOOKUPS.with(|count| count.set(0));
}

#[cfg(test)]
pub(crate) fn per_id_symbol_lookup_count() -> usize {
    PER_ID_SYMBOL_LOOKUPS.with(std::cell::Cell::get)
}

#[cfg(test)]
pub(crate) fn batch_symbol_lookup_count() -> usize {
    BATCH_SYMBOL_LOOKUPS.with(std::cell::Cell::get)
}

#[cfg(test)]
fn record_per_id_symbol_lookup() {
    PER_ID_SYMBOL_LOOKUPS.with(|count| count.set(count.get() + 1));
}

#[cfg(not(test))]
fn record_per_id_symbol_lookup() {}

#[cfg(test)]
fn record_batch_symbol_lookup() {
    BATCH_SYMBOL_LOOKUPS.with(|count| count.set(count.get() + 1));
}

#[cfg(not(test))]
fn record_batch_symbol_lookup() {}

pub fn visible_symbol_by_id(
    conn: &mut Client,
    ctx: &Context,
    id: &str,
) -> anyhow::Result<Option<Symbol>> {
    record_per_id_symbol_lookup();
    // A non-uuid id cannot exist in the uuid column: no such symbol.
    let Ok(id) = db::id_param(id) else {
        return Ok(None);
    };
    let columns = db::symbol_select_columns("cs");
    let Some(row) = conn.query_opt(
        &format!("SELECT {columns} FROM code_symbols cs WHERE cs.id = $1"),
        &[&id],
    )?
    else {
        return Ok(None);
    };
    let symbol = Symbol::from_row(&row)?;
    Ok(symbol_is_visible(conn, ctx, &symbol).then_some(symbol))
}

pub fn visible_symbols_by_ids(
    conn: &mut Client,
    ctx: &Context,
    ids: &[String],
) -> anyhow::Result<Vec<Symbol>> {
    if ids.is_empty() {
        return Ok(Vec::new());
    }

    // Non-uuid ids cannot exist in the uuid column; drop them the same way a
    // non-matching id used to fall out of the IN list.
    let ids: Vec<Uuid> = ids.iter().filter_map(|id| db::id_param(id).ok()).collect();
    if ids.is_empty() {
        return Ok(Vec::new());
    }
    record_batch_symbol_lookup();
    let columns = db::symbol_select_columns("");
    let sql = format!(
        "SELECT {columns} FROM code_symbols
         WHERE id = ANY($1)
         ORDER BY file_path, line_start"
    );
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    for row in conn.query(&sql, &[&ids])? {
        let symbol = Symbol::from_row(&row)?;
        if seen.insert(symbol.id.clone()) {
            out.push(symbol);
        }
    }
    filter_visible_symbols(conn, ctx, out)
}

pub(crate) fn filter_visible_symbols(
    conn: &mut Client,
    ctx: &Context,
    symbols: Vec<Symbol>,
) -> anyhow::Result<Vec<Symbol>> {
    if symbols.is_empty() {
        return Ok(symbols);
    }
    let (Some(machine), Some(project)) = (
        machine_uuid_or_invisible(&ctx.machine_id),
        project_uuid_or_invisible(scope_project_id(ctx)),
    ) else {
        return Ok(Vec::new());
    };
    let paths = symbols
        .iter()
        .map(|s| s.file_path.clone())
        .collect::<Vec<_>>();
    let selectors = visible_selectors_sql(ctx, "$1", "$2");
    let rows = conn.query(
        &format!("SELECT project_id, file_path, content_hash FROM ({selectors}) visible WHERE file_path = ANY($3)"),
        &[&machine, &project, &paths],
    )?;
    let versions = rows
        .iter()
        .map(|row| {
            Ok((
                db::id_string(row, "project_id")?,
                row.try_get::<_, String>("file_path")?,
                row.try_get::<_, String>("content_hash")?,
            ))
        })
        .collect::<anyhow::Result<HashSet<_>>>()?;
    Ok(symbols
        .into_iter()
        .filter(|s| {
            versions.contains(&(
                s.project_id.clone(),
                s.file_path.clone(),
                s.file_content_hash.clone(),
            ))
        })
        .collect())
}

pub fn visible_symbols_for_file(
    conn: &mut Client,
    ctx: &Context,
    file_path: &str,
) -> anyhow::Result<Vec<Symbol>> {
    visible_symbols_for_files(conn, ctx, &[file_path.to_string()])
}

pub fn visible_symbols_for_files(
    conn: &mut Client,
    ctx: &Context,
    file_paths: &[String],
) -> anyhow::Result<Vec<Symbol>> {
    if file_paths.is_empty() {
        return Ok(Vec::new());
    }
    let machine = db::id_param(&ctx.machine_id)?;
    let project = db::id_param(scope_project_id(ctx))?;
    let sql = symbols_for_files_sql(matches!(ctx.index_scope, ProjectIndexScope::Overlay { .. }));
    conn.query(&sql, &[&machine, &project, &file_paths])?
        .iter()
        .map(Symbol::from_row)
        .collect()
}

fn symbols_for_files_sql(include_pins: bool) -> String {
    let columns = db::symbol_select_columns("cs");
    let selectors = file_selectors_sql("$1", "$2", include_pins);
    let visible = selector_condition(&selectors, "cs", "file_content_hash");
    format!(
        "SELECT {columns} FROM code_symbols cs
             JOIN code_indexed_files cf ON cf.project_id = cs.project_id
              AND cf.file_path = cs.file_path AND cf.content_hash = cs.file_content_hash
             WHERE cs.file_path = ANY($3) AND {visible}
             ORDER BY cs.file_path, cs.line_start, cs.byte_start"
    )
}

#[cfg(test)]
#[path = "visibility/tests.rs"]
mod tests;
