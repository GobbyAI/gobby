use std::collections::HashMap;
use std::path::Path;

use postgres::Client;

use crate::config::Context;
use crate::graph::code_graph;
use crate::index::{api, hasher};
use crate::models::IndexedProject;
use crate::projection::sync::{self, ProjectionSyncRequest, ProjectionTarget};
use crate::vector::code_symbols;

use super::types::{IndexDegradation, IndexOutcome, IndexRequest};
use super::util::{epoch_secs_str, relative_path};

pub(super) fn cleanup_deleted_file_projections(
    ctx: &Context,
    file_path: &str,
    outcome: &mut IndexOutcome,
) {
    if let Err(error) = code_graph::delete_file_projection(ctx, file_path) {
        push_projection_cleanup_degradation(
            outcome,
            file_path,
            ProjectionTarget::Graph,
            error.to_string(),
        );
    }

    match ctx.qdrant.as_ref() {
        Some(qdrant) => {
            if let Err(error) =
                code_symbols::delete_file_vectors(qdrant, &ctx.project_id, file_path)
            {
                push_projection_cleanup_degradation(
                    outcome,
                    file_path,
                    ProjectionTarget::Vectors,
                    error.to_string(),
                );
            }
        }
        None => push_projection_cleanup_degradation(
            outcome,
            file_path,
            ProjectionTarget::Vectors,
            "Qdrant config is required for deleted-file vector cleanup".to_string(),
        ),
    }
}

fn push_projection_cleanup_degradation(
    outcome: &mut IndexOutcome,
    file_path: &str,
    target: ProjectionTarget,
    message: String,
) {
    outcome
        .degraded
        .push(IndexDegradation::ProjectionCleanupFailed {
            file_path: file_path.to_string(),
            target,
            message,
        });
}

pub(super) fn attach_projection_sync(outcome: &mut IndexOutcome, request: &IndexRequest) {
    if !request.sync_projections {
        return;
    }

    outcome.projection_sync = Some(sync::pending_after_code_fact_write(ProjectionSyncRequest {
        project_id: outcome.project_id.clone(),
        file_paths: outcome.indexed_file_paths.clone(),
        targets: vec![ProjectionTarget::Graph, ProjectionTarget::Vectors],
    }));
}

/// Invalidate all index data for a project.
pub fn invalidate(
    conn: &mut Client,
    project_id: &str,
    daemon_url: Option<&str>,
) -> anyhow::Result<()> {
    // Notify daemon FIRST — it reads project stats from the same hub
    // to know what to clean from FalkorDB/Qdrant.
    if let Some(url) = daemon_url {
        notify_daemon_invalidate(url, project_id);
    }

    let mut tx = conn.transaction()?;
    tx.execute(
        "DELETE FROM code_symbols WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_indexed_files WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_content_chunks WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_imports WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_calls WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_indexed_projects WHERE id = $1",
        &[&project_id],
    )?;
    tx.commit()?;
    eprintln!("Invalidated code index for project {project_id}");

    Ok(())
}

/// POST to the Gobby daemon requesting FalkorDB/Qdrant cleanup for a project.
/// Fire-and-forget: warns on failure, never errors.
fn notify_daemon_invalidate(base_url: &str, project_id: &str) {
    let client = match reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .build()
    {
        Ok(c) => c,
        Err(error) => {
            eprintln!("Warning: could not build daemon invalidate HTTP client: {error}");
            return;
        }
    };

    let base = base_url.trim_end_matches('/');
    let url = format!("{base}/api/code-index/invalidate");
    match client
        .post(&url)
        .json(&serde_json::json!({"project_id": project_id}))
        .send()
    {
        Ok(resp) if !resp.status().is_success() => {
            eprintln!("Warning: daemon invalidate returned {}", resp.status());
        }
        Err(e) => {
            eprintln!("Warning: could not notify daemon: {e}");
        }
        _ => {}
    }
}

pub(super) fn refresh_project_stats(
    conn: &mut Client,
    root_path: &Path,
    project_id: &str,
    elapsed_ms: u64,
    total_eligible_files: Option<usize>,
) {
    let total_files = count_rows(conn, "code_indexed_files", project_id);
    let total_symbols = count_rows(conn, "code_symbols", project_id);

    if let Err(error) = api::upsert_project_stats(
        conn,
        &IndexedProject {
            id: project_id.to_string(),
            root_path: root_path.to_string_lossy().to_string(),
            total_files,
            total_symbols,
            last_indexed_at: epoch_secs_str(),
            index_duration_ms: elapsed_ms,
            total_eligible_files,
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
    project_id: &str,
    current_hashes: &HashMap<String, String>,
) -> HashMap<String, ()> {
    let mut stale = HashMap::new();
    let mut indexed = HashMap::new();
    if let Ok(rows) = conn.query(
        "SELECT file_path, content_hash FROM code_indexed_files WHERE project_id = $1",
        &[&project_id],
    ) {
        for row in rows {
            if let (Ok(file_path), Ok(content_hash)) = (
                row.try_get::<_, String>("file_path"),
                row.try_get::<_, String>("content_hash"),
            ) {
                indexed.insert(file_path, content_hash);
            }
        }
    }

    for (path, hash) in current_hashes {
        if indexed.get(path) != Some(hash) {
            stale.insert(path.clone(), ());
        }
    }
    stale
}

pub(super) fn current_file_hashes(
    root_path: &Path,
    candidates: &[std::path::PathBuf],
    content_only: &[std::path::PathBuf],
) -> HashMap<String, String> {
    let mut current_hashes = HashMap::new();
    for path in candidates.iter().chain(content_only.iter()) {
        if let Ok(rel) = relative_path(path, root_path) {
            let hash = match hasher::file_content_hash(path) {
                Ok(hash) => hash,
                Err(error) => {
                    eprintln!(
                        "Warning: failed to hash {} for incremental index detection: {error}",
                        path.display()
                    );
                    String::new()
                }
            };
            current_hashes.insert(rel, hash);
        }
    }
    current_hashes
}

pub(super) fn get_orphan_files(
    conn: &mut Client,
    project_id: &str,
    current_hashes: &HashMap<String, String>,
) -> Vec<String> {
    let mut orphans = Vec::new();
    if let Ok(rows) = conn.query(
        "SELECT file_path FROM code_indexed_files WHERE project_id = $1",
        &[&project_id],
    ) {
        for row in rows {
            if let Ok(file_path) = row.try_get::<_, String>("file_path")
                && !current_hashes.contains_key(&file_path)
            {
                orphans.push(file_path);
            }
        }
    }
    orphans
}

fn count_rows(conn: &mut Client, table: &str, project_id: &str) -> usize {
    if !matches!(table, "code_indexed_files" | "code_symbols") {
        return 0;
    }
    let sql = format!("SELECT COUNT(*)::BIGINT AS count FROM {table} WHERE project_id = $1");
    conn.query_one(&sql, &[&project_id])
        .ok()
        .and_then(|row| row.try_get::<_, i64>("count").ok())
        .unwrap_or(0) as usize
}
