use std::collections::BTreeSet;

use postgres::Client;

use crate::commands::scope;
use crate::config::{Context, ProjectIndexScope};
use crate::visibility;

#[cfg(test)]
thread_local! {
    static LOOKUPS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

const SINGLE_SCOPE_SQL: &str = "SELECT DISTINCT fs.file_path
    FROM code_indexed_file_states fs
    JOIN code_indexed_files f
      ON f.project_id = fs.project_id
     AND f.file_path = fs.file_path
     AND f.content_hash = fs.content_hash
    WHERE fs.machine_id = $1
      AND fs.project_id = $2
      AND fs.file_path = ANY($3)
      AND f.language != $4";

const OVERLAY_SCOPE_SQL: &str = "SELECT ofs.file_path
    FROM code_indexed_file_states ofs
    JOIN code_indexed_files of
      ON of.project_id = ofs.project_id
     AND of.file_path = ofs.file_path
     AND of.content_hash = ofs.content_hash
    WHERE ofs.machine_id = $1
      AND ofs.project_id = $2
      AND ofs.file_path = ANY($4)
      AND of.language != $5
    UNION
    SELECT pfs.file_path
    FROM code_indexed_file_states pfs
    JOIN code_indexed_files pf
      ON pf.project_id = pfs.project_id
     AND pf.file_path = pfs.file_path
     AND pf.content_hash = pfs.content_hash
    WHERE pfs.machine_id = $1
      AND pfs.project_id = $3
      AND pfs.file_path = ANY($4)
      AND pf.language != $5
      AND NOT EXISTS (
          SELECT 1 FROM code_indexed_file_states shadow
          WHERE shadow.machine_id = $1
            AND shadow.project_id = $2
            AND shadow.file_path = pfs.file_path
      )";

/// Candidate paths grep may return: indexed on the context machine and still
/// present in the project checkout. Issues one set-based query per call.
pub(super) fn valid_indexed_paths(
    conn: &mut Client,
    ctx: &Context,
    file_paths: &BTreeSet<String>,
) -> BTreeSet<String> {
    let candidates: Vec<String> = file_paths.iter().cloned().collect();
    indexed_file_paths(conn, ctx, &candidates)
        .into_iter()
        .filter(|file_path| scope::path_exists_in_current_project(ctx, file_path))
        .collect()
}

/// The subset of `file_paths` that `visibility::indexed_file_exists` accepts for
/// each path, answered by one statement for the whole set.
fn indexed_file_paths(conn: &mut Client, ctx: &Context, file_paths: &[String]) -> BTreeSet<String> {
    if file_paths.is_empty() {
        return BTreeSet::new();
    }
    let Some(machine_id) = visibility::machine_uuid_or_invisible(&ctx.machine_id) else {
        return BTreeSet::new();
    };
    record_lookup();
    let rows = match &ctx.index_scope {
        ProjectIndexScope::Single => {
            let Some(project_id) = visibility::project_uuid_or_invisible(&ctx.project_id) else {
                return BTreeSet::new();
            };
            conn.query(
                SINGLE_SCOPE_SQL,
                &[
                    &machine_id,
                    &project_id,
                    &file_paths,
                    &visibility::TOMBSTONE_LANGUAGE,
                ],
            )
        }
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } => {
            let (Some(overlay_project_id), Some(parent_project_id)) = (
                visibility::project_uuid_or_invisible(overlay_project_id),
                visibility::project_uuid_or_invisible(parent_project_id),
            ) else {
                return BTreeSet::new();
            };
            conn.query(
                OVERLAY_SCOPE_SQL,
                &[
                    &machine_id,
                    &overlay_project_id,
                    &parent_project_id,
                    &file_paths,
                    &visibility::TOMBSTONE_LANGUAGE,
                ],
            )
        }
    };
    rows.unwrap_or_default()
        .iter()
        .filter_map(|row| row.try_get::<_, String>(0).ok())
        .collect()
}

#[cfg(test)]
fn record_lookup() {
    LOOKUPS.with(|count| count.set(count.get() + 1));
}

#[cfg(not(test))]
fn record_lookup() {}

#[cfg(test)]
pub(super) fn reset_lookup_count() {
    LOOKUPS.with(|count| count.set(0));
}

#[cfg(test)]
pub(super) fn lookup_count() -> usize {
    LOOKUPS.with(std::cell::Cell::get)
}
