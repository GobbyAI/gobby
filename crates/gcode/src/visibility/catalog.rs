use postgres::Client;

use super::{
    TOMBSTONE_LANGUAGE, VisibleFile, machine_uuid_or_invisible, project_uuid_or_invisible,
};
use crate::config::{Context, ProjectIndexScope};
use crate::db;

pub fn visible_kinds(conn: &mut Client, ctx: &Context) -> anyhow::Result<Vec<String>> {
    let selectors = super::visible_selectors_sql(ctx, "$1", "$2");
    let rows = conn.query(
        &format!("SELECT DISTINCT cs.kind FROM code_symbols cs
                  JOIN ({selectors}) visible ON visible.project_id = cs.project_id
                   AND visible.file_path = cs.file_path AND visible.content_hash = cs.file_content_hash
                  ORDER BY cs.kind"),
        &[&db::id_param(&ctx.machine_id)?, &db::id_param(super::scope_project_id(ctx))?],
    )?;
    rows.iter().map(|row| Ok(row.try_get("kind")?)).collect()
}

pub fn visible_tree(conn: &mut Client, ctx: &Context) -> anyhow::Result<Vec<VisibleFile>> {
    let selectors = super::visible_selectors_sql(ctx, "$1", "$2");
    let rows = conn.query(
        &format!(
            "SELECT file_path, language, symbol_count::BIGINT AS symbol_count, content_hash
                  FROM ({selectors}) visible ORDER BY file_path"
        ),
        &[
            &db::id_param(&ctx.machine_id)?,
            &db::id_param(super::scope_project_id(ctx))?,
        ],
    )?;
    rows.iter()
        .map(|row| {
            Ok(VisibleFile {
                file_path: row.try_get("file_path")?,
                language: row.try_get("language")?,
                symbol_count: row.try_get("symbol_count")?,
                content_hash: row.try_get("content_hash")?,
            })
        })
        .collect()
}

pub(crate) fn visible_imports(
    conn: &mut Client,
    ctx: &Context,
) -> anyhow::Result<Vec<(String, String)>> {
    let selectors = super::visible_selectors_sql(ctx, "$1", "$2");
    let rows = conn.query(
        &format!(
            "SELECT ci.source_file, ci.target_module FROM code_imports ci
                  JOIN ({selectors}) visible ON visible.project_id = ci.project_id
                   AND visible.file_path = ci.source_file AND visible.content_hash = ci.content_hash
                  ORDER BY ci.source_file, ci.target_module"
        ),
        &[
            &db::id_param(&ctx.machine_id)?,
            &db::id_param(super::scope_project_id(ctx))?,
        ],
    )?;
    rows.iter()
        .map(|row| Ok((row.try_get("source_file")?, row.try_get("target_module")?)))
        .collect()
}

pub fn tombstone_count(conn: &mut Client, ctx: &Context) -> usize {
    let ProjectIndexScope::Overlay {
        overlay_project_id, ..
    } = &ctx.index_scope
    else {
        return 0;
    };
    let (Some(machine_id), Some(overlay_project_id)) = (
        machine_uuid_or_invisible(&ctx.machine_id),
        project_uuid_or_invisible(overlay_project_id),
    ) else {
        return 0;
    };
    conn.query_one(
        "SELECT COUNT(*)::BIGINT AS count
         FROM code_indexed_file_states fs
         JOIN code_indexed_files f
           ON f.project_id = fs.project_id
          AND f.file_path = fs.file_path
          AND f.content_hash = fs.content_hash
         WHERE fs.machine_id = $1
           AND fs.project_id = $2
           AND f.language = $3",
        &[&machine_id, &overlay_project_id, &TOMBSTONE_LANGUAGE],
    )
    .ok()
    .and_then(|row| row.try_get::<_, i64>("count").ok())
    .and_then(|count| count.try_into().ok())
    .unwrap_or(0)
}

/// One selector a project serves: its own owned state, or a pin row it inherits.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct EffectiveSelector {
    pub(crate) file_path: String,
    pub(crate) source_project_id: String,
    pub(crate) content_hash: String,
    pub(crate) git_blob_id: Option<String>,
}

/// Non-tombstone selectors `project_id` serves on `machine_id`, in path order. Its own
/// owned states come first; a pin row only contributes when no owned state shadows it.
pub(crate) fn effective_selectors(
    conn: &mut Client,
    machine_id: &str,
    project_id: &str,
) -> anyhow::Result<Vec<EffectiveSelector>> {
    let machine_id = db::id_param(machine_id)?;
    let project_id = db::id_param(project_id)?;
    let selectors = super::file_selectors_sql("$1", "$2", true);
    let rows = conn.query(
        &format!(
            "SELECT file_path, project_id::text AS source_project_id, content_hash, git_blob_id
                  FROM ({selectors}) visible ORDER BY file_path"
        ),
        &[&machine_id, &project_id],
    )?;
    rows.iter()
        .map(|row| {
            Ok(EffectiveSelector {
                file_path: row.try_get("file_path")?,
                source_project_id: row.try_get("source_project_id")?,
                content_hash: row.try_get("content_hash")?,
                git_blob_id: row.try_get("git_blob_id")?,
            })
        })
        .collect()
}
