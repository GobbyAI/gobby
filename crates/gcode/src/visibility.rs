use std::collections::{HashMap, HashSet};

use postgres::Client;
use uuid::Uuid;

use crate::config::{Context, ProjectIndexScope};
use crate::db;
use crate::models::{GraphResult, Symbol};

mod catalog;

pub use catalog::{tombstone_count, visible_kinds, visible_tree};

pub const TOMBSTONE_LANGUAGE: &str = "__gcode_deleted__";
pub const TOMBSTONE_HASH: &str = "__gcode_tombstone__";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VisibleFile {
    pub file_path: String,
    pub language: String,
    pub symbol_count: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct IndexedFileState {
    content_hash: String,
    language: String,
}

pub fn is_tombstone_language(language: &str) -> bool {
    language == TOMBSTONE_LANGUAGE
}

pub fn visible_project_ids(ctx: &Context) -> Vec<String> {
    match &ctx.index_scope {
        ProjectIndexScope::Single => vec![ctx.project_id.clone()],
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

pub(super) fn local_machine_uuid() -> anyhow::Result<Uuid> {
    let machine_id = gobby_core::machine::read_local_machine_id()?;
    db::id_param(&machine_id)
}

pub(super) fn local_machine_uuid_or_invisible() -> Option<Uuid> {
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
    let Some(machine_id) = local_machine_uuid_or_invisible() else {
        return false;
    };
    match &ctx.index_scope {
        ProjectIndexScope::Single => {
            let Some(project_id) = project_uuid_or_invisible(&ctx.project_id) else {
                return false;
            };
            conn.query_one(
                "SELECT EXISTS(
                    SELECT 1
                    FROM code_indexed_file_states fs
                    JOIN code_indexed_files f
                      ON f.project_id = fs.project_id
                     AND f.file_path = fs.file_path
                     AND f.content_hash = fs.content_hash
                    WHERE fs.machine_id = $1
                      AND fs.project_id = $2
                      AND fs.file_path = $3
                      AND f.language != $4
                )",
                &[&machine_id, &project_id, &file_path, &TOMBSTONE_LANGUAGE],
            )
            .and_then(|row| row.try_get::<_, bool>(0))
            .unwrap_or(false)
        }
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } => {
            let (Some(overlay_project_id), Some(parent_project_id)) = (
                project_uuid_or_invisible(overlay_project_id),
                project_uuid_or_invisible(parent_project_id),
            ) else {
                return false;
            };
            conn.query_one(
                "SELECT EXISTS(
                    SELECT 1
                    FROM code_indexed_file_states ofs
                    JOIN code_indexed_files of
                      ON of.project_id = ofs.project_id
                     AND of.file_path = ofs.file_path
                     AND of.content_hash = ofs.content_hash
                    WHERE ofs.machine_id = $1
                      AND ofs.project_id = $2
                      AND ofs.file_path = $4
                      AND of.language != $5
                    UNION ALL
                    SELECT 1
                    FROM code_indexed_file_states pfs
                    JOIN code_indexed_files pf
                      ON pf.project_id = pfs.project_id
                     AND pf.file_path = pfs.file_path
                     AND pf.content_hash = pfs.content_hash
                    WHERE pfs.machine_id = $1
                      AND pfs.project_id = $3
                      AND pfs.file_path = $4
                      AND pf.language != $5
                      AND NOT EXISTS (
                          SELECT 1 FROM code_indexed_file_states shadow
                          WHERE shadow.machine_id = $1
                            AND shadow.project_id = $2
                            AND shadow.file_path = pfs.file_path
                      )
                    LIMIT 1
                )",
                &[
                    &machine_id,
                    &overlay_project_id,
                    &parent_project_id,
                    &file_path,
                    &TOMBSTONE_LANGUAGE,
                ],
            )
            .and_then(|row| row.try_get::<_, bool>(0))
            .unwrap_or(false)
        }
    }
}

pub fn content_chunks_exist(conn: &mut Client, ctx: &Context, file_path: &str) -> bool {
    let Some(machine_id) = local_machine_uuid_or_invisible() else {
        return false;
    };
    match &ctx.index_scope {
        ProjectIndexScope::Single => {
            let Some(project_id) = project_uuid_or_invisible(&ctx.project_id) else {
                return false;
            };
            conn.query_one(
                "SELECT EXISTS(
                    SELECT 1
                    FROM code_indexed_file_states fs
                    JOIN code_content_chunks c
                      ON c.project_id = fs.project_id
                     AND c.file_path = fs.file_path
                     AND c.content_hash = fs.content_hash
                    WHERE fs.machine_id = $1
                      AND fs.project_id = $2
                      AND fs.file_path = $3
                )",
                &[&machine_id, &project_id, &file_path],
            )
            .and_then(|row| row.try_get::<_, bool>(0))
            .unwrap_or(false)
        }
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } => {
            let (Some(overlay_project_id), Some(parent_project_id)) = (
                project_uuid_or_invisible(overlay_project_id),
                project_uuid_or_invisible(parent_project_id),
            ) else {
                return false;
            };
            conn.query_one(
                "SELECT EXISTS(
                    SELECT 1
                    FROM code_indexed_file_states ofs
                    JOIN code_content_chunks c
                      ON c.project_id = ofs.project_id
                     AND c.file_path = ofs.file_path
                     AND c.content_hash = ofs.content_hash
                    JOIN code_indexed_files f
                      ON f.project_id = ofs.project_id
                     AND f.file_path = ofs.file_path
                     AND f.content_hash = ofs.content_hash
                    WHERE ofs.machine_id = $1
                      AND ofs.project_id = $2
                      AND ofs.file_path = $4
                      AND f.language != $5
                    UNION ALL
                    SELECT 1
                    FROM code_indexed_file_states pfs
                    JOIN code_content_chunks c
                      ON c.project_id = pfs.project_id
                     AND c.file_path = pfs.file_path
                     AND c.content_hash = pfs.content_hash
                    JOIN code_indexed_files f
                      ON f.project_id = pfs.project_id
                     AND f.file_path = pfs.file_path
                     AND f.content_hash = pfs.content_hash
                    WHERE pfs.machine_id = $1
                      AND pfs.project_id = $3
                      AND pfs.file_path = $4
                      AND f.language != $5
                      AND NOT EXISTS (
                          SELECT 1 FROM code_indexed_file_states shadow
                          WHERE shadow.machine_id = $1
                            AND shadow.project_id = $2
                            AND shadow.file_path = pfs.file_path
                      )
                    LIMIT 1
                )",
                &[
                    &machine_id,
                    &overlay_project_id,
                    &parent_project_id,
                    &file_path,
                    &TOMBSTONE_LANGUAGE,
                ],
            )
            .and_then(|row| row.try_get::<_, bool>(0))
            .unwrap_or(false)
        }
    }
}

pub fn symbol_is_visible(conn: &mut Client, ctx: &Context, symbol: &Symbol) -> bool {
    let Some(machine_id) = local_machine_uuid_or_invisible() else {
        return false;
    };
    let Some(project_id) = project_uuid_or_invisible(&symbol.project_id) else {
        return false;
    };
    let is_active = conn
        .query_one(
            "SELECT EXISTS(
                SELECT 1 FROM code_indexed_file_states
                WHERE machine_id = $1
                  AND project_id = $2
                  AND file_path = $3
                  AND content_hash = $4
            )",
            &[
                &machine_id,
                &project_id,
                &symbol.file_path,
                &symbol.file_content_hash,
            ],
        )
        .and_then(|row| row.try_get::<_, bool>(0))
        .unwrap_or(false);
    is_active && project_path_is_visible(conn, ctx, &symbol.project_id, &symbol.file_path)
}

pub fn project_path_is_visible(
    conn: &mut Client,
    ctx: &Context,
    project_id: &str,
    file_path: &str,
) -> bool {
    match &ctx.index_scope {
        ProjectIndexScope::Single => {
            project_id == ctx.project_id
                && project_file_is_visible(conn, &ctx.project_id, file_path)
        }
        ProjectIndexScope::Overlay {
            overlay_project_id, ..
        } if project_id == overlay_project_id => {
            project_file_is_visible(conn, overlay_project_id, file_path)
        }
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } if project_id == parent_project_id => {
            !overlay_has_row(conn, overlay_project_id, file_path)
                && project_file_is_visible(conn, parent_project_id, file_path)
        }
        ProjectIndexScope::Overlay { .. } => false,
    }
}

pub fn project_file_is_visible(conn: &mut Client, project_id: &str, file_path: &str) -> bool {
    let (Some(machine_id), Some(project_id)) = (
        local_machine_uuid_or_invisible(),
        project_uuid_or_invisible(project_id),
    ) else {
        return false;
    };
    conn.query_one(
        "SELECT EXISTS(
            SELECT 1
            FROM code_indexed_file_states fs
            JOIN code_indexed_files f
              ON f.project_id = fs.project_id
             AND f.file_path = fs.file_path
             AND f.content_hash = fs.content_hash
            WHERE fs.machine_id = $1
              AND fs.project_id = $2
              AND fs.file_path = $3
              AND f.language != $4
        )",
        &[&machine_id, &project_id, &file_path, &TOMBSTONE_LANGUAGE],
    )
    .and_then(|row| row.try_get::<_, bool>(0))
    .unwrap_or(false)
}

pub fn overlay_has_row(conn: &mut Client, overlay_project_id: &str, file_path: &str) -> bool {
    let (Some(machine_id), Some(overlay_project_id)) = (
        local_machine_uuid_or_invisible(),
        project_uuid_or_invisible(overlay_project_id),
    ) else {
        return false;
    };
    conn.query_one(
        "SELECT EXISTS(
            SELECT 1 FROM code_indexed_file_states
            WHERE machine_id = $1 AND project_id = $2 AND file_path = $3
        )",
        &[&machine_id, &overlay_project_id, &file_path],
    )
    .and_then(|row| row.try_get::<_, bool>(0))
    .unwrap_or(false)
}

pub fn visible_symbol_by_id(
    conn: &mut Client,
    ctx: &Context,
    id: &str,
) -> anyhow::Result<Option<Symbol>> {
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

pub fn filter_visible_graph_results(
    conn: &mut Client,
    ctx: &Context,
    results: Vec<GraphResult>,
) -> anyhow::Result<Vec<GraphResult>> {
    let ids = results
        .iter()
        .map(|result| result.id.clone())
        .collect::<Vec<_>>();
    let visible_ids = visible_symbols_by_ids(conn, ctx, &ids)?
        .into_iter()
        .map(|symbol| symbol.id)
        .collect::<HashSet<_>>();
    let path_candidates = results
        .iter()
        .filter(|result| db::id_param(&result.id).is_err())
        .map(|result| result.file_path.clone())
        .filter(|path| !path.is_empty())
        .collect::<HashSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    let visible_paths = visible_graph_paths(conn, ctx, &path_candidates)?;

    Ok(results
        .into_iter()
        .filter(|result| {
            if db::id_param(&result.id).is_ok() {
                visible_ids.contains(&result.id)
            } else {
                visible_paths.contains(&result.file_path)
            }
        })
        .collect())
}

pub(crate) fn visible_graph_paths(
    conn: &mut Client,
    ctx: &Context,
    file_paths: &[String],
) -> anyhow::Result<HashSet<String>> {
    if file_paths.is_empty() {
        return Ok(HashSet::new());
    }
    let Some(machine_id) = local_machine_uuid_or_invisible() else {
        return Ok(HashSet::new());
    };
    let rows = match &ctx.index_scope {
        ProjectIndexScope::Single => {
            let Some(project_id) = project_uuid_or_invisible(&ctx.project_id) else {
                return Ok(HashSet::new());
            };
            let active = machine_state_condition("f", "content_hash", "$1");
            conn.query(
                &format!(
                    "SELECT DISTINCT f.file_path
                     FROM code_indexed_files f
                     WHERE f.project_id = $2
                       AND f.file_path = ANY($3)
                       AND f.language != $4
                       AND {active}"
                ),
                &[&machine_id, &project_id, &file_paths, &TOMBSTONE_LANGUAGE],
            )?
        }
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } => {
            let (Some(overlay_project_id), Some(parent_project_id)) = (
                project_uuid_or_invisible(overlay_project_id),
                project_uuid_or_invisible(parent_project_id),
            ) else {
                return Ok(HashSet::new());
            };
            let overlay_active = machine_state_condition("of", "content_hash", "$1");
            let parent_active = machine_state_condition("pf", "content_hash", "$1");
            conn.query(
                &format!(
                    "SELECT of.file_path
                     FROM code_indexed_files of
                     WHERE of.project_id = $2
                       AND of.file_path = ANY($4)
                       AND of.language != $5
                       AND {overlay_active}
                     UNION
                     SELECT pf.file_path
                     FROM code_indexed_files pf
                     WHERE pf.project_id = $3
                       AND pf.file_path = ANY($4)
                       AND pf.language != $5
                       AND {parent_active}
                       AND NOT EXISTS (
                           SELECT 1 FROM code_indexed_file_states shadow
                           WHERE shadow.machine_id = $1
                             AND shadow.project_id = $2
                             AND shadow.file_path = pf.file_path
                       )"
                ),
                &[
                    &machine_id,
                    &overlay_project_id,
                    &parent_project_id,
                    &file_paths,
                    &TOMBSTONE_LANGUAGE,
                ],
            )?
        }
    };
    rows.into_iter()
        .map(|row| {
            row.try_get::<_, String>("file_path")
                .map_err(anyhow::Error::from)
        })
        .collect()
}

pub(crate) fn filter_visible_symbols(
    conn: &mut Client,
    ctx: &Context,
    symbols: Vec<Symbol>,
) -> anyhow::Result<Vec<Symbol>> {
    if symbols.is_empty() {
        return Ok(symbols);
    }

    let mut project_ids = symbols
        .iter()
        .map(|symbol| symbol.project_id.clone())
        .collect::<HashSet<_>>();
    if let ProjectIndexScope::Overlay {
        overlay_project_id,
        parent_project_id,
        ..
    } = &ctx.index_scope
    {
        project_ids.insert(overlay_project_id.clone());
        project_ids.insert(parent_project_id.clone());
    }
    let project_ids = project_ids.into_iter().collect::<Vec<_>>();
    let file_paths = symbols
        .iter()
        .map(|symbol| symbol.file_path.clone())
        .collect::<HashSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    let file_states = indexed_file_states(conn, &project_ids, &file_paths)?;

    Ok(symbols
        .into_iter()
        .filter(|symbol| symbol_visible_from_file_states(ctx, symbol, &file_states))
        .collect())
}

fn indexed_file_states(
    conn: &mut Client,
    project_ids: &[String],
    file_paths: &[String],
) -> anyhow::Result<HashMap<(String, String), IndexedFileState>> {
    if project_ids.is_empty() || file_paths.is_empty() {
        return Ok(HashMap::new());
    }

    // Non-uuid project ids (unit-test contexts) cannot exist in the hub; skip
    // them instead of failing the whole visibility lookup.
    let project_ids: Vec<Uuid> = project_ids
        .iter()
        .filter_map(|id| db::id_param(id).ok())
        .collect();
    if project_ids.is_empty() {
        return Ok(HashMap::new());
    }

    // Unresolvable local identity makes every candidate invisible. Returning
    // an empty map keeps that contract explicit for all in-memory filtering.
    let Some(machine_id) = local_machine_uuid_or_invisible() else {
        return Ok(HashMap::new());
    };
    let rows = conn.query(
        "SELECT fs.project_id, fs.file_path, fs.content_hash, f.language
         FROM code_indexed_file_states fs
         JOIN code_indexed_files f
           ON f.project_id = fs.project_id
          AND f.file_path = fs.file_path
          AND f.content_hash = fs.content_hash
         WHERE fs.machine_id = $1
           AND fs.project_id = ANY($2)
           AND fs.file_path = ANY($3)",
        &[&machine_id, &project_ids, &file_paths],
    )?;
    rows.into_iter()
        .map(|row| {
            Ok((
                (
                    db::id_string(&row, "project_id")?,
                    row.try_get::<_, String>("file_path")?,
                ),
                IndexedFileState {
                    content_hash: row.try_get("content_hash")?,
                    language: row.try_get("language")?,
                },
            ))
        })
        .collect()
}

fn symbol_visible_from_file_states(
    ctx: &Context,
    symbol: &Symbol,
    file_states: &HashMap<(String, String), IndexedFileState>,
) -> bool {
    match &ctx.index_scope {
        ProjectIndexScope::Single => {
            symbol.project_id == ctx.project_id
                && indexed_state_matches_symbol(
                    file_states.get(&(ctx.project_id.clone(), symbol.file_path.clone())),
                    symbol,
                )
        }
        ProjectIndexScope::Overlay {
            overlay_project_id, ..
        } if symbol.project_id == *overlay_project_id => indexed_state_matches_symbol(
            file_states.get(&(overlay_project_id.clone(), symbol.file_path.clone())),
            symbol,
        ),
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } if symbol.project_id == *parent_project_id => {
            let overlay_key = (overlay_project_id.clone(), symbol.file_path.clone());
            let parent_key = (parent_project_id.clone(), symbol.file_path.clone());
            !file_states.contains_key(&overlay_key)
                && indexed_state_matches_symbol(file_states.get(&parent_key), symbol)
        }
        ProjectIndexScope::Overlay { .. } => false,
    }
}

fn indexed_state_matches_symbol(state: Option<&IndexedFileState>, symbol: &Symbol) -> bool {
    state.is_some_and(|state| {
        state.content_hash == symbol.file_content_hash && !is_tombstone_language(&state.language)
    })
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

    match &ctx.index_scope {
        ProjectIndexScope::Single => query_symbols_for_files(conn, &ctx.project_id, file_paths),
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } => {
            query_overlay_symbols_for_files(conn, overlay_project_id, parent_project_id, file_paths)
        }
    }
}

fn query_symbols_for_files(
    conn: &mut Client,
    project_id: &str,
    file_paths: &[String],
) -> anyhow::Result<Vec<Symbol>> {
    let machine_id = local_machine_uuid()?;
    let project_id = db::id_param(project_id)?;
    let rows = conn.query(
        &symbols_for_files_sql(),
        &[&machine_id, &project_id, &file_paths, &TOMBSTONE_LANGUAGE],
    )?;
    rows.iter().map(Symbol::from_row).collect()
}

fn query_overlay_symbols_for_files(
    conn: &mut Client,
    overlay_project_id: &str,
    parent_project_id: &str,
    file_paths: &[String],
) -> anyhow::Result<Vec<Symbol>> {
    let machine_id = local_machine_uuid()?;
    let overlay_project_id = db::id_param(overlay_project_id)?;
    let parent_project_id = db::id_param(parent_project_id)?;
    let rows = conn.query(
        &overlay_symbols_for_files_sql(),
        &[
            &machine_id,
            &overlay_project_id,
            &parent_project_id,
            &file_paths,
            &TOMBSTONE_LANGUAGE,
        ],
    )?;
    rows.iter().map(Symbol::from_row).collect()
}

fn symbols_for_files_sql() -> String {
    let columns = db::symbol_select_columns("cs");
    let machine_state = machine_state_condition("cs", "file_content_hash", "$1");
    format!(
        "SELECT {columns}
         FROM code_symbols cs
         JOIN code_indexed_files cf
           ON cf.project_id = cs.project_id
          AND cf.file_path = cs.file_path
          AND cf.content_hash = cs.file_content_hash
         WHERE cs.project_id = $2
           AND cs.file_path = ANY($3)
           AND cf.language != $4
           AND {machine_state}
         ORDER BY cs.file_path, cs.line_start, cs.byte_start"
    )
}

fn overlay_symbols_for_files_sql() -> String {
    let columns = db::symbol_select_columns("cs");
    let machine_state = machine_state_condition("cs", "file_content_hash", "$1");
    format!(
        "SELECT {columns}
         FROM code_symbols cs
         JOIN code_indexed_files cf
           ON cf.project_id = cs.project_id
          AND cf.file_path = cs.file_path
          AND cf.content_hash = cs.file_content_hash
         WHERE cs.file_path = ANY($4)
           AND cf.language != $5
           AND {machine_state}
           AND (
               cs.project_id = $2
               OR (
                   cs.project_id = $3
                   AND NOT EXISTS (
                       SELECT 1 FROM code_indexed_file_states shadow
                       WHERE shadow.machine_id = $1
                         AND shadow.project_id = $2
                         AND shadow.file_path = cs.file_path
                   )
               )
           )
         ORDER BY cs.file_path, cs.line_start, cs.byte_start"
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn visible_project_ids_include_overlay_before_parent() {
        let ctx = Context {
            database_url: String::new(),
            project_root: PathBuf::from("/worktree"),
            project_id: "overlay".to_string(),
            quiet: true,
            falkordb: None,
            qdrant: None,
            embedding: None,
            code_vectors: crate::config::CodeVectorSettings::default(),
            runtime_config_capture_degraded: false,
            indexing: gobby_core::config::IndexingConfig::default(),
            daemon_url: None,
            grant_ai: None,
            index_scope: ProjectIndexScope::Overlay {
                overlay_project_id: "overlay".to_string(),
                overlay_root: PathBuf::from("/worktree"),
                parent_project_id: "parent".to_string(),
                parent_root: PathBuf::from("/parent"),
            },
        };

        assert_eq!(visible_project_ids(&ctx), vec!["overlay", "parent"]);
    }

    #[test]
    fn symbols_for_file_sql_qualifies_joined_symbol_columns() {
        let sql = symbols_for_files_sql();

        assert!(sql.contains("SELECT cs.id, cs.project_id, cs.file_path"));
        assert!(sql.contains("FROM code_symbols cs"));
        assert!(sql.contains("JOIN code_indexed_files cf"));
        assert!(sql.contains("cs.file_path = ANY($3)"));
        assert!(!sql.contains("SELECT id, project_id, file_path"));
    }

    #[test]
    fn overlay_symbols_for_files_sql_batches_paths_and_preserves_overlay_shadowing() {
        let sql = overlay_symbols_for_files_sql();

        assert!(sql.contains("SELECT cs.id, cs.project_id, cs.file_path"));
        assert!(sql.contains("cs.file_path = ANY($4)"));
        assert!(sql.contains("cs.project_id = $2"));
        assert!(sql.contains("cs.project_id = $3"));
        assert!(sql.contains("NOT EXISTS"));
        assert!(sql.contains("shadow.project_id = $2"));
        assert!(sql.contains("shadow.file_path = cs.file_path"));
        assert!(!sql.contains("cs.file_path = $4"));
    }
}
