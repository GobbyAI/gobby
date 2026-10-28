//! Content versions a graph read may serve. Graph edges carry the content hash
//! they were projected from, so reads filter on `path|hash` before any LIMIT or
//! aggregation. A pin serves its graph only after its projection is recorded.

use std::collections::{BTreeSet, HashMap};

use crate::config::Context;
use crate::db;
use crate::graph::typed_query;

use super::{
    TOMBSTONE_LANGUAGE, file_selectors_sql, machine_uuid_or_invisible, project_uuid_or_invisible,
    scope_project_id,
};

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub(crate) struct AdmittedGraphVersions {
    /// `path|content_hash` for every admitted file version.
    pub(crate) keys: Vec<String>,
    /// Every path with at least one admitted version.
    pub(crate) paths: Vec<String>,
}

/// Versions the scope's graph namespace may serve: the owned file states, plus
/// pins whose graph projection has landed and that no owned state shadows.
pub(crate) fn admitted_graph_versions(ctx: &Context) -> anyhow::Result<AdmittedGraphVersions> {
    let (Some(machine), Some(project)) = (
        machine_uuid_or_invisible(&ctx.machine_id),
        project_uuid_or_invisible(scope_project_id(ctx)),
    ) else {
        return Ok(AdmittedGraphVersions::default());
    };
    let owned = file_selectors_sql("$1", "$2", false);
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let rows = conn.query(
        &format!(
            "SELECT file_path, content_hash FROM ({owned}) owned
             UNION
             SELECT p.file_path, p.content_hash
             FROM code_overlay_pins p
             JOIN code_indexed_files f
               ON f.project_id = p.source_project_id AND f.file_path = p.file_path
              AND f.content_hash = p.content_hash
             WHERE p.machine_id = $1 AND p.overlay_project_id = $2
               AND p.graph_projected
               AND f.language != '{TOMBSTONE_LANGUAGE}'
               AND NOT EXISTS (
                   SELECT 1 FROM code_indexed_file_states shadow
                   WHERE shadow.machine_id = $1 AND shadow.project_id = $2
                     AND shadow.file_path = p.file_path
               )"
        ),
        &[&machine, &project],
    )?;
    let mut admitted = AdmittedGraphVersions::default();
    let mut paths = BTreeSet::new();
    for row in &rows {
        let path: String = row.try_get("file_path")?;
        let hash: String = row.try_get("content_hash")?;
        admitted.keys.push(format!("{path}|{hash}"));
        paths.insert(path);
    }
    admitted.paths = paths.into_iter().collect();
    Ok(admitted)
}

/// Binds `$admitted_versions` and `$admitted_paths` into a query built by a
/// payload or report builder.
pub(crate) fn with_admitted_versions(
    (query, mut params): (String, HashMap<String, String>),
    admitted: &AdmittedGraphVersions,
) -> (String, HashMap<String, String>) {
    params.insert(
        "admitted_versions".to_string(),
        format!("[{}]", typed_query::id_list_literal(&admitted.keys)),
    );
    params.insert(
        "admitted_paths".to_string(),
        format!("[{}]", typed_query::id_list_literal(&admitted.paths)),
    );
    (query, params)
}
