use std::collections::{HashMap, HashSet};
use std::path::Path;

use anyhow::Context as _;
use postgres::Client;

use crate::db::id_param;
use crate::index::{api, hasher};
use crate::models::IndexedProject;
use crate::projection::sync::{self, ProjectionSyncRequest, ProjectionTarget};

use super::types::{IndexOutcome, IndexRequest};
use super::util::{epoch_secs_str, relative_path};

pub(super) fn attach_projection_sync(outcome: &mut IndexOutcome, request: &IndexRequest) {
    if !request.sync_projections {
        return;
    }

    outcome.projection_sync = Some(sync::pending_after_code_fact_write(ProjectionSyncRequest {
        project_id: outcome.project_id.clone(),
        graph_file_paths: outcome.graph_file_paths.clone(),
        vector_file_paths: outcome.vector_file_paths.clone(),
        targets: vec![ProjectionTarget::Graph, ProjectionTarget::Vectors],
    }));
}

/// Invalidate all index data for a project.
pub fn invalidate(
    conn: &mut Client,
    project_id: &str,
    _daemon_url: Option<&str>,
) -> anyhow::Result<()> {
    let machine_id = gobby_core::machine::read_local_machine_id()?;
    let machine_uuid = id_param(&machine_id)?;
    let project_uuid = id_param(project_id)?;
    let mut tx = conn.transaction()?;
    tx.execute(
        "DELETE FROM code_indexed_file_states
         WHERE machine_id = $1 AND project_id = $2",
        &[&machine_uuid, &project_uuid],
    )?;
    tx.execute(
        "DELETE FROM code_indexed_project_states
         WHERE machine_id = $1 AND project_id = $2",
        &[&machine_uuid, &project_uuid],
    )?;
    tx.commit()?;
    eprintln!("Invalidated local code index state for project {project_id}");

    Ok(())
}

pub(super) fn refresh_project_stats(
    conn: &mut Client,
    machine_id: &str,
    root_path: &Path,
    project_id: &str,
    elapsed_ms: u64,
    total_eligible_files: Option<usize>,
    indexer_version: Option<&str>,
) {
    let total_files = count_machine_rows(conn, machine_id, project_id, false);
    let total_symbols = count_machine_rows(conn, machine_id, project_id, true);

    if let Err(error) = api::upsert_project_stats(
        conn,
        machine_id,
        &IndexedProject {
            id: project_id.to_string(),
            root_path: root_path.to_string_lossy().to_string(),
            total_files,
            total_symbols,
            last_indexed_at: epoch_secs_str(),
            index_duration_ms: elapsed_ms,
            total_eligible_files,
            indexer_version: indexer_version.map(ToOwned::to_owned),
        },
    ) {
        eprintln!(
            "Warning: refresh_project_stats failed to upsert project stats for project {project_id} at {}: {error}",
            root_path.display()
        );
    }
}

pub(super) fn get_stale_files(
    conn: &mut Client,
    machine_id: &str,
    project_id: &str,
    current_hashes: &HashMap<String, String>,
) -> anyhow::Result<HashSet<String>> {
    let mut stale = HashSet::new();
    let mut indexed = HashMap::new();
    let project_uuid = id_param(project_id)
        .with_context(|| format!("stale detection requires a uuid project id, got {project_id}"))?;
    let machine_uuid = id_param(machine_id)
        .with_context(|| format!("stale detection requires a uuid machine id, got {machine_id}"))?;
    let rows = conn
        .query(
            "SELECT file_path, content_hash
             FROM code_indexed_file_states
             WHERE machine_id = $1 AND project_id = $2",
            &[&machine_uuid, &project_uuid],
        )
        .map_err(|error| {
            log::error!(
                "failed to query indexed files for stale detection for project {project_id}: {error}"
            );
            error
        })?;
    for row in rows {
        let file_path = match row.try_get::<_, String>("file_path") {
            Ok(file_path) => file_path,
            Err(error) => {
                log::warn!(
                    "skipping malformed indexed-file stale-detection row for project {project_id}: file_path: {error}"
                );
                continue;
            }
        };
        let content_hash = match row.try_get::<_, String>("content_hash") {
            Ok(content_hash) => content_hash,
            Err(error) => {
                log::warn!(
                    "skipping malformed indexed-file stale-detection row for project {project_id}, file {file_path}: content_hash: {error}"
                );
                continue;
            }
        };
        indexed.insert(file_path, content_hash);
    }

    for (path, hash) in current_hashes {
        if indexed.get(path) != Some(hash) {
            stale.insert(path.clone());
        }
    }
    Ok(stale)
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub(super) struct CurrentFileState {
    pub(super) hashes: HashMap<String, String>,
    pub(super) present_paths: HashSet<String>,
}

pub(super) fn current_file_state(
    root_path: &Path,
    candidates: &[std::path::PathBuf],
    content_only: &[std::path::PathBuf],
) -> CurrentFileState {
    let mut state = CurrentFileState::default();
    for path in candidates.iter().chain(content_only.iter()) {
        if let Ok(rel) = relative_path(path, root_path) {
            state.present_paths.insert(rel.clone());
            match hasher::file_content_hash(path) {
                Ok(hash) => {
                    state.hashes.insert(rel, hash);
                }
                Err(error) => {
                    eprintln!(
                        "Warning: failed to hash {} for incremental index detection: {error}",
                        path.display()
                    );
                }
            }
        }
    }
    state
}

pub(super) fn get_orphan_files(
    conn: &mut Client,
    machine_id: &str,
    project_id: &str,
    present_paths: &HashSet<String>,
) -> anyhow::Result<Vec<String>> {
    let mut orphans = Vec::new();
    let project_uuid = id_param(project_id).with_context(|| {
        format!("orphan detection requires a uuid project id, got {project_id}")
    })?;
    let machine_uuid = id_param(machine_id).with_context(|| {
        format!("orphan detection requires a uuid machine id, got {machine_id}")
    })?;
    let rows = conn
        .query(
            "SELECT file_path FROM code_indexed_file_states
             WHERE machine_id = $1 AND project_id = $2",
            &[&machine_uuid, &project_uuid],
        )
        .map_err(|error| {
            log::error!(
                "failed to query indexed files for orphan detection for project {project_id}: {error}"
            );
            error
        })?;
    for row in rows {
        let file_path = match row.try_get::<_, String>("file_path") {
            Ok(file_path) => file_path,
            Err(error) => {
                log::warn!(
                    "skipping malformed indexed-file orphan-detection row for project {project_id}: file_path: {error}"
                );
                continue;
            }
        };
        if !present_paths.contains(&file_path) {
            orphans.push(file_path);
        }
    }
    Ok(orphans)
}

fn count_machine_rows(
    conn: &mut Client,
    machine_id: &str,
    project_id: &str,
    symbols: bool,
) -> usize {
    // A non-uuid project id cannot exist in the uuid column; report zero rows,
    // matching this helper's existing swallow-to-zero error handling.
    let Ok(project_id) = id_param(project_id) else {
        return 0;
    };
    let Ok(machine_id) = id_param(machine_id) else {
        return 0;
    };
    let sql = if symbols {
        "SELECT COUNT(*)::BIGINT AS count
         FROM code_symbols cs
         JOIN code_indexed_file_states cifs
           ON cifs.project_id = cs.project_id
          AND cifs.file_path = cs.file_path
          AND cifs.content_hash = cs.file_content_hash
         WHERE cifs.machine_id = $1 AND cifs.project_id = $2"
    } else {
        "SELECT COUNT(*)::BIGINT AS count
         FROM code_indexed_file_states
         WHERE machine_id = $1 AND project_id = $2"
    };
    conn.query_one(sql, &[&machine_id, &project_id])
        .ok()
        .and_then(|row| row.try_get::<_, i64>("count").ok())
        .unwrap_or(0) as usize
}
