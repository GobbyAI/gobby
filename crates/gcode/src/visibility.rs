use std::collections::HashSet;

use postgres::Client;

use crate::config::{Context, ProjectIndexScope};
use crate::db;
use crate::models::Symbol;

pub const TOMBSTONE_LANGUAGE: &str = "__gcode_deleted__";
pub const TOMBSTONE_HASH: &str = "__gcode_tombstone__";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VisibleFile {
    pub file_path: String,
    pub language: String,
    pub symbol_count: i64,
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
            parent_project_id,
            parent_root,
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

pub fn indexed_file_exists(conn: &mut Client, ctx: &Context, file_path: &str) -> bool {
    match &ctx.index_scope {
        ProjectIndexScope::Single => conn
            .query_one(
                "SELECT EXISTS(
                    SELECT 1 FROM code_indexed_files
                    WHERE project_id = $1 AND file_path = $2 AND language != $3
                )",
                &[&ctx.project_id, &file_path, &TOMBSTONE_LANGUAGE],
            )
            .and_then(|row| row.try_get::<_, bool>(0))
            .unwrap_or(false),
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } => conn
            .query_one(
                "SELECT EXISTS(
                    SELECT 1 FROM code_indexed_files of
                    WHERE of.project_id = $1
                      AND of.file_path = $3
                      AND of.language != $4
                    UNION ALL
                    SELECT 1 FROM code_indexed_files pf
                    WHERE pf.project_id = $2
                      AND pf.file_path = $3
                      AND pf.language != $4
                      AND NOT EXISTS (
                          SELECT 1 FROM code_indexed_files shadow
                          WHERE shadow.project_id = $1 AND shadow.file_path = pf.file_path
                      )
                    LIMIT 1
                )",
                &[
                    overlay_project_id,
                    parent_project_id,
                    &file_path,
                    &TOMBSTONE_LANGUAGE,
                ],
            )
            .and_then(|row| row.try_get::<_, bool>(0))
            .unwrap_or(false),
    }
}

pub fn content_chunks_exist(conn: &mut Client, ctx: &Context, file_path: &str) -> bool {
    match &ctx.index_scope {
        ProjectIndexScope::Single => conn
            .query_one(
                "SELECT EXISTS(
                    SELECT 1 FROM code_content_chunks
                    WHERE project_id = $1 AND file_path = $2
                )",
                &[&ctx.project_id, &file_path],
            )
            .and_then(|row| row.try_get::<_, bool>(0))
            .unwrap_or(false),
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } => conn
            .query_one(
                "SELECT EXISTS(
                    SELECT 1 FROM code_content_chunks c
                    JOIN code_indexed_files f
                      ON f.project_id = c.project_id AND f.file_path = c.file_path
                    WHERE c.project_id = $1
                      AND c.file_path = $3
                      AND f.language != $4
                    UNION ALL
                    SELECT 1 FROM code_content_chunks c
                    JOIN code_indexed_files f
                      ON f.project_id = c.project_id AND f.file_path = c.file_path
                    WHERE c.project_id = $2
                      AND c.file_path = $3
                      AND f.language != $4
                      AND NOT EXISTS (
                          SELECT 1 FROM code_indexed_files shadow
                          WHERE shadow.project_id = $1 AND shadow.file_path = c.file_path
                      )
                    LIMIT 1
                )",
                &[
                    overlay_project_id,
                    parent_project_id,
                    &file_path,
                    &TOMBSTONE_LANGUAGE,
                ],
            )
            .and_then(|row| row.try_get::<_, bool>(0))
            .unwrap_or(false),
    }
}

pub fn symbol_is_visible(conn: &mut Client, ctx: &Context, symbol: &Symbol) -> bool {
    project_path_is_visible(conn, ctx, &symbol.project_id, &symbol.file_path)
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
            overlay_project_id,
            parent_project_id,
            ..
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
    conn.query_one(
        "SELECT EXISTS(
            SELECT 1 FROM code_indexed_files
            WHERE project_id = $1 AND file_path = $2 AND language != $3
        )",
        &[&project_id, &file_path, &TOMBSTONE_LANGUAGE],
    )
    .and_then(|row| row.try_get::<_, bool>(0))
    .unwrap_or(false)
}

pub fn overlay_has_row(conn: &mut Client, overlay_project_id: &str, file_path: &str) -> bool {
    conn.query_one(
        "SELECT EXISTS(
            SELECT 1 FROM code_indexed_files
            WHERE project_id = $1 AND file_path = $2
        )",
        &[&overlay_project_id, &file_path],
    )
    .and_then(|row| row.try_get::<_, bool>(0))
    .unwrap_or(false)
}

pub fn visible_symbol_by_id(
    conn: &mut Client,
    ctx: &Context,
    id: &str,
) -> anyhow::Result<Option<Symbol>> {
    let columns = db::symbol_select_columns("");
    let Some(row) = conn.query_opt(
        &format!("SELECT {columns} FROM code_symbols WHERE id = $1"),
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

    let placeholders: Vec<String> = (1..=ids.len()).map(|i| format!("${i}")).collect();
    let columns = db::symbol_select_columns("");
    let sql = format!(
        "SELECT {columns} FROM code_symbols
         WHERE id IN ({})
         ORDER BY file_path, line_start",
        placeholders.join(",")
    );
    let params: Vec<&(dyn postgres::types::ToSql + Sync)> = ids
        .iter()
        .map(|s| s as &(dyn postgres::types::ToSql + Sync))
        .collect();
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    for row in conn.query(&sql, &params)? {
        let symbol = Symbol::from_row(&row)?;
        if seen.insert(symbol.id.clone()) && symbol_is_visible(conn, ctx, &symbol) {
            out.push(symbol);
        }
    }
    Ok(out)
}

pub fn visible_symbols_for_file(
    conn: &mut Client,
    ctx: &Context,
    file_path: &str,
) -> anyhow::Result<Vec<Symbol>> {
    let columns = db::symbol_select_columns("");
    match &ctx.index_scope {
        ProjectIndexScope::Single => query_symbols_for_file(conn, &ctx.project_id, file_path),
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } => {
            if overlay_has_row(conn, overlay_project_id, file_path) {
                return query_symbols_for_file(conn, overlay_project_id, file_path);
            }
            let sql = format!(
                "SELECT {columns} FROM code_symbols
                 WHERE project_id = $1 AND file_path = $2
                 ORDER BY line_start, byte_start"
            );
            let rows = conn.query(&sql, &[parent_project_id, &file_path])?;
            rows.iter().map(Symbol::from_row).collect()
        }
    }
}

fn query_symbols_for_file(
    conn: &mut Client,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<Vec<Symbol>> {
    let columns = db::symbol_select_columns("");
    let rows = conn.query(
        &format!(
            "SELECT {columns} FROM code_symbols
             WHERE project_id = $1 AND file_path = $2
             ORDER BY line_start, byte_start"
        ),
        &[&project_id, &file_path],
    )?;
    rows.iter().map(Symbol::from_row).collect()
}

pub fn visible_kinds(conn: &mut Client, ctx: &Context) -> anyhow::Result<Vec<String>> {
    let mut kinds = HashSet::new();
    for project_id in visible_project_ids(ctx) {
        let columns = db::symbol_select_columns("");
        for row in conn.query(
            &format!("SELECT {columns} FROM code_symbols WHERE project_id = $1"),
            &[&project_id],
        )? {
            let symbol = Symbol::from_row(&row)?;
            if symbol_is_visible(conn, ctx, &symbol) {
                kinds.insert(symbol.kind);
            }
        }
    }
    let mut kinds: Vec<_> = kinds.into_iter().collect();
    kinds.sort();
    Ok(kinds)
}

pub fn visible_tree(conn: &mut Client, ctx: &Context) -> anyhow::Result<Vec<VisibleFile>> {
    let rows = match &ctx.index_scope {
        ProjectIndexScope::Single => conn.query(
            "SELECT file_path, language, symbol_count::BIGINT AS symbol_count
             FROM code_indexed_files
             WHERE project_id = $1 AND language != $2
             ORDER BY file_path",
            &[&ctx.project_id, &TOMBSTONE_LANGUAGE],
        )?,
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } => conn.query(
            "SELECT file_path, language, symbol_count::BIGINT AS symbol_count
             FROM code_indexed_files
             WHERE project_id = $1 AND language != $3
             UNION ALL
             SELECT pf.file_path, pf.language, pf.symbol_count::BIGINT AS symbol_count
             FROM code_indexed_files pf
             WHERE pf.project_id = $2
               AND pf.language != $3
               AND NOT EXISTS (
                   SELECT 1 FROM code_indexed_files of
                   WHERE of.project_id = $1 AND of.file_path = pf.file_path
               )
             ORDER BY file_path",
            &[overlay_project_id, parent_project_id, &TOMBSTONE_LANGUAGE],
        )?,
    };

    rows.iter()
        .map(|row| {
            Ok(VisibleFile {
                file_path: row.try_get("file_path")?,
                language: row.try_get("language")?,
                symbol_count: row.try_get("symbol_count")?,
            })
        })
        .collect()
}

pub fn tombstone_count(conn: &mut Client, ctx: &Context) -> usize {
    let ProjectIndexScope::Overlay {
        overlay_project_id, ..
    } = &ctx.index_scope
    else {
        return 0;
    };
    conn.query_one(
        "SELECT COUNT(*)::BIGINT AS count
         FROM code_indexed_files
         WHERE project_id = $1 AND language = $2",
        &[overlay_project_id, &TOMBSTONE_LANGUAGE],
    )
    .ok()
    .and_then(|row| row.try_get::<_, i64>("count").ok())
    .unwrap_or(0) as usize
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
            daemon_url: None,
            index_scope: ProjectIndexScope::Overlay {
                overlay_project_id: "overlay".to_string(),
                overlay_root: PathBuf::from("/worktree"),
                parent_project_id: "parent".to_string(),
                parent_root: PathBuf::from("/parent"),
            },
        };

        assert_eq!(visible_project_ids(&ctx), vec!["overlay", "parent"]);
    }
}
