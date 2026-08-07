use postgres::Client;

use super::{
    TOMBSTONE_LANGUAGE, VisibleFile, local_machine_uuid, local_machine_uuid_or_invisible,
    project_uuid_or_invisible,
};
use crate::config::{Context, ProjectIndexScope};
use crate::db;

pub fn visible_kinds(conn: &mut Client, ctx: &Context) -> anyhow::Result<Vec<String>> {
    let machine_id = local_machine_uuid()?;
    let rows = match &ctx.index_scope {
        ProjectIndexScope::Single => conn.query(
            "SELECT DISTINCT cs.kind
             FROM code_symbols cs
             JOIN code_indexed_file_states fs
               ON fs.project_id = cs.project_id
              AND fs.file_path = cs.file_path
              AND fs.content_hash = cs.file_content_hash
             JOIN code_indexed_files cf
               ON cf.project_id = fs.project_id
              AND cf.file_path = fs.file_path
              AND cf.content_hash = fs.content_hash
             WHERE fs.machine_id = $1
               AND cs.project_id = $2
               AND cf.language != $3
             ORDER BY cs.kind",
            &[
                &machine_id,
                &db::id_param(&ctx.project_id)?,
                &TOMBSTONE_LANGUAGE,
            ],
        )?,
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } => conn.query(
            "SELECT kind
             FROM (
                 SELECT cs.kind
                 FROM code_symbols cs
                 JOIN code_indexed_file_states fs
                   ON fs.project_id = cs.project_id
                  AND fs.file_path = cs.file_path
                  AND fs.content_hash = cs.file_content_hash
                 JOIN code_indexed_files cf
                   ON cf.project_id = fs.project_id
                  AND cf.file_path = fs.file_path
                  AND cf.content_hash = fs.content_hash
                 WHERE fs.machine_id = $1
                   AND cs.project_id = $2
                   AND cf.language != $4
                 UNION
                 SELECT cs.kind
                 FROM code_symbols cs
                 JOIN code_indexed_file_states fs
                   ON fs.project_id = cs.project_id
                  AND fs.file_path = cs.file_path
                  AND fs.content_hash = cs.file_content_hash
                 JOIN code_indexed_files cf
                   ON cf.project_id = fs.project_id
                  AND cf.file_path = fs.file_path
                  AND cf.content_hash = fs.content_hash
                 WHERE fs.machine_id = $1
                   AND cs.project_id = $3
                   AND cf.language != $4
                   AND NOT EXISTS (
                       SELECT 1 FROM code_indexed_file_states shadow
                       WHERE shadow.machine_id = $1
                         AND shadow.project_id = $2
                         AND shadow.file_path = cs.file_path
                   )
             ) visible
             ORDER BY kind",
            &[
                &machine_id,
                &db::id_param(overlay_project_id)?,
                &db::id_param(parent_project_id)?,
                &TOMBSTONE_LANGUAGE,
            ],
        )?,
    };

    rows.iter()
        .map(|row| Ok(row.try_get::<_, String>("kind")?))
        .collect()
}

pub fn visible_tree(conn: &mut Client, ctx: &Context) -> anyhow::Result<Vec<VisibleFile>> {
    let machine_id = local_machine_uuid()?;
    let rows = match &ctx.index_scope {
        ProjectIndexScope::Single => conn.query(
            "SELECT fs.file_path, f.language, f.symbol_count::BIGINT AS symbol_count
             FROM code_indexed_file_states fs
             JOIN code_indexed_files f
               ON f.project_id = fs.project_id
              AND f.file_path = fs.file_path
              AND f.content_hash = fs.content_hash
             WHERE fs.machine_id = $1
               AND fs.project_id = $2
               AND f.language != $3
             ORDER BY fs.file_path",
            &[
                &machine_id,
                &db::id_param(&ctx.project_id)?,
                &TOMBSTONE_LANGUAGE,
            ],
        )?,
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } => conn.query(
            "SELECT ofs.file_path, of.language, of.symbol_count::BIGINT AS symbol_count
             FROM code_indexed_file_states ofs
             JOIN code_indexed_files of
               ON of.project_id = ofs.project_id
              AND of.file_path = ofs.file_path
              AND of.content_hash = ofs.content_hash
             WHERE ofs.machine_id = $1
               AND ofs.project_id = $2
               AND of.language != $4
             UNION ALL
             SELECT pfs.file_path, pf.language, pf.symbol_count::BIGINT AS symbol_count
             FROM code_indexed_file_states pfs
             JOIN code_indexed_files pf
               ON pf.project_id = pfs.project_id
              AND pf.file_path = pfs.file_path
              AND pf.content_hash = pfs.content_hash
             WHERE pfs.machine_id = $1
               AND pfs.project_id = $3
               AND pf.language != $4
               AND NOT EXISTS (
                   SELECT 1 FROM code_indexed_file_states shadow
                   WHERE shadow.machine_id = $1
                     AND shadow.project_id = $2
                     AND shadow.file_path = pfs.file_path
               )
             ORDER BY file_path",
            &[
                &machine_id,
                &db::id_param(overlay_project_id)?,
                &db::id_param(parent_project_id)?,
                &TOMBSTONE_LANGUAGE,
            ],
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
    let (Some(machine_id), Some(overlay_project_id)) = (
        local_machine_uuid_or_invisible(),
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
