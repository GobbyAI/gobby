//! Graph-result visibility under the caller's ordinary project scope.

use std::collections::HashSet;

use postgres::Client;

use super::{
    TOMBSTONE_LANGUAGE, local_machine_uuid_or_invisible, machine_state_condition,
    project_uuid_or_invisible, visible_symbols_by_ids,
};
use crate::config::{Context, ProjectIndexScope};
use crate::db;
use crate::models::GraphResult;

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
        .filter(|result| {
            !result.file_path.is_empty()
                && (graph_result_node_kind(result) != GraphResultNodeKind::Symbol
                    || db::id_param(&result.id).is_err())
        })
        .map(|result| result.file_path.clone())
        .collect::<HashSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    let visible_paths = visible_graph_paths(conn, ctx, &path_candidates)?;

    Ok(results
        .into_iter()
        .filter(|result| graph_result_is_visible(result, &visible_ids, &visible_paths))
        .collect())
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum GraphResultNodeKind {
    Symbol,
    External,
    Unresolved,
}

pub(crate) fn graph_result_node_kind(result: &GraphResult) -> GraphResultNodeKind {
    match result.node_kind.as_deref() {
        Some(kind) if kind.eq_ignore_ascii_case("external") => GraphResultNodeKind::External,
        Some(kind) if kind.eq_ignore_ascii_case("externalsymbol") => GraphResultNodeKind::External,
        Some(kind) if kind.eq_ignore_ascii_case("unresolved") => GraphResultNodeKind::Unresolved,
        Some(kind) if kind.eq_ignore_ascii_case("unresolvedcallee") => {
            GraphResultNodeKind::Unresolved
        }
        _ => GraphResultNodeKind::Symbol,
    }
}

pub(crate) fn graph_result_is_visible(
    result: &GraphResult,
    visible_symbol_ids: &HashSet<String>,
    visible_caller_paths: &HashSet<String>,
) -> bool {
    match graph_result_node_kind(result) {
        GraphResultNodeKind::External | GraphResultNodeKind::Unresolved => {
            result.file_path.is_empty() || visible_caller_paths.contains(&result.file_path)
        }
        GraphResultNodeKind::Symbol => {
            if db::id_param(&result.id).is_ok() {
                visible_symbol_ids.contains(&result.id)
            } else {
                visible_caller_paths.contains(&result.file_path)
            }
        }
    }
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
