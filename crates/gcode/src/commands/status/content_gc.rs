use std::collections::{BTreeSet, HashMap, HashSet};
use std::path::PathBuf;
use std::time::Instant;

use anyhow::Context as _;
use postgres::Client;

use crate::config::{Context, ServiceConfigSelection};
use crate::db;
use crate::graph::code_graph;
use crate::index_lock::{IndexLockPolicy, lease_project_lock};
use crate::vector::code_symbols;

mod history;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct ContentGcCandidate {
    pub(super) id: String,
    pub(super) project_id: String,
    pub(super) file_path: String,
    pub(super) content_hash: String,
    pub(super) symbol_ids: Vec<String>,
    pub(super) has_graph_facts: bool,
    pub(super) graph_synced: bool,
    pub(super) vectors_synced: bool,
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub(super) struct ContentGcTotals {
    pub(super) deleted_versions: usize,
    pub(super) deleted_symbols: usize,
    pub(super) busy_projects: usize,
    pub(super) failed_versions: usize,
    pub(super) skipped_versions: usize,
    /// Versions left for a later run because the time budget expired.
    pub(super) deferred_versions: usize,
    /// Projects whose post-deletion graph orphan sweep failed.
    pub(super) orphan_sweep_failures: usize,
}

pub(super) fn discover_content_gc(
    database_url: &str,
    retention_days: u32,
    project_filter: Option<&str>,
) -> anyhow::Result<Vec<ContentGcCandidate>> {
    let mut conn = db::connect_readonly(database_url)?;
    let machine_id = db::id_param(&gobby_core::machine::read_local_machine_id()?)?;
    let project_filter = project_filter.map(db::id_param).transpose()?;
    let retention_days = i32::try_from(retention_days).context("GC retention days exceed i32")?;
    let rows = conn.query(
        "SELECT f.id::text AS id,
                f.project_id::text AS project_id,
                f.file_path,
                f.content_hash,
                f.graph_synced,
                f.vectors_synced,
                (
                    EXISTS (
                        SELECT 1 FROM code_symbols graph_symbol
                        WHERE graph_symbol.project_id = f.project_id
                          AND graph_symbol.file_path = f.file_path
                          AND graph_symbol.file_content_hash = f.content_hash
                    )
                    OR EXISTS (
                        SELECT 1 FROM code_imports graph_import
                        WHERE graph_import.project_id = f.project_id
                          AND graph_import.source_file = f.file_path
                          AND graph_import.content_hash = f.content_hash
                    )
                    OR EXISTS (
                        SELECT 1 FROM code_calls graph_call
                        WHERE graph_call.project_id = f.project_id
                          AND graph_call.file_path = f.file_path
                          AND graph_call.content_hash = f.content_hash
                    )
                    OR EXISTS (
                        SELECT 1 FROM code_inheritance graph_inherit
                        WHERE graph_inherit.project_id = f.project_id
                          AND graph_inherit.file_path = f.file_path
                          AND graph_inherit.content_hash = f.content_hash
                    )
                ) AS has_graph_facts,
                ps.root_path,
                COALESCE(
                    array_agg(s.id::text ORDER BY s.id)
                        FILTER (WHERE s.id IS NOT NULL),
                    ARRAY[]::text[]
                ) AS symbol_ids
         FROM code_indexed_files f
         JOIN code_indexed_project_states ps
           ON ps.machine_id = $1 AND ps.project_id = f.project_id
         LEFT JOIN code_symbols s
           ON s.project_id = f.project_id
          AND s.file_path = f.file_path
          AND s.file_content_hash = f.content_hash
         WHERE f.last_referenced_at < NOW() - make_interval(days => $2)
           AND ($3::uuid IS NULL OR f.project_id = $3)
           AND NOT EXISTS (
               SELECT 1 FROM code_indexed_file_states fs
               WHERE fs.project_id = f.project_id
                 AND fs.file_path = f.file_path
                 AND fs.content_hash = f.content_hash
           )
           AND NOT EXISTS (
               SELECT 1 FROM code_overlay_pins p
               WHERE p.source_project_id = f.project_id
                 AND p.file_path = f.file_path
                 AND p.content_hash = f.content_hash
           )
         GROUP BY f.id, f.project_id, f.file_path, f.content_hash,
                  f.graph_synced, f.vectors_synced, ps.root_path
         ORDER BY f.project_id, f.file_path, f.content_hash",
        &[&machine_id, &retention_days, &project_filter],
    )?;

    // Recent-history lookups are one git batch per repository root: every
    // candidate path of a root is checked against that root's history in a
    // single call, so the cost is bounded by that history rather than by the
    // candidate count (two git subprocesses per candidate path timed out the
    // daemon's prune on a 4,800-path backlog, #21085).
    let mut paths_by_root: HashMap<PathBuf, BTreeSet<String>> = HashMap::new();
    for row in &rows {
        let root_path = PathBuf::from(row.try_get::<_, String>("root_path")?);
        paths_by_root
            .entry(root_path)
            .or_default()
            .insert(row.try_get("file_path")?);
    }
    let mut protected_by_root: HashMap<PathBuf, Option<HashSet<(String, String)>>> = HashMap::new();
    for (root_path, paths) in &paths_by_root {
        let looked_up = match history::recent_path_hashes(root_path, retention_days, paths) {
            Ok(protected) => Some(protected),
            Err(error) => {
                log::warn!(
                    "retaining {} code-index paths because recent git history in {} could not be inspected: {error:#}",
                    paths.len(),
                    root_path.display(),
                );
                None
            }
        };
        protected_by_root.insert(root_path.clone(), looked_up);
    }
    let mut candidates = Vec::new();
    for row in rows {
        let root_path = PathBuf::from(row.try_get::<_, String>("root_path")?);
        let version = (
            row.try_get::<_, String>("file_path")?,
            row.try_get::<_, String>("content_hash")?,
        );
        match protected_by_root.get(&root_path) {
            Some(Some(protected)) if !protected.contains(&version) => {}
            // Protected by recent git history at this path, or history
            // unavailable: retain.
            _ => continue,
        }
        let (file_path, content_hash) = version;
        candidates.push(ContentGcCandidate {
            id: row.try_get("id")?,
            project_id: row.try_get("project_id")?,
            file_path,
            content_hash,
            symbol_ids: row.try_get("symbol_ids")?,
            has_graph_facts: row.try_get("has_graph_facts")?,
            graph_synced: row.try_get("graph_synced")?,
            vectors_synced: row.try_get("vectors_synced")?,
        });
    }
    Ok(candidates)
}

/// Delete every candidate's projections and SQL row, stopping once `deadline`
/// passes; the remainder is reported as deferred for a later run.
pub(super) fn prune_content_versions(
    services: &Context,
    candidates: &[ContentGcCandidate],
    deadline: Option<Instant>,
) -> anyhow::Result<ContentGcTotals> {
    prune_content_versions_with(
        services,
        candidates,
        || deadline.is_some_and(|deadline| Instant::now() >= deadline),
        |project_id| {
            Context::resolve_for_project_id_with_services(
                project_id,
                services.quiet,
                ServiceConfigSelection::projection_cleanup(),
            )
        },
        delete_candidate_projections,
        code_graph::cleanup_orphans,
    )
}

fn prune_content_versions_with(
    services: &Context,
    candidates: &[ContentGcCandidate],
    mut deadline_reached: impl FnMut() -> bool,
    resolve_project_services: impl Fn(&str) -> anyhow::Result<Context>,
    mut delete_projections: impl FnMut(&Context, &ContentGcCandidate) -> anyhow::Result<()>,
    mut sweep_graph_orphans: impl FnMut(&Context) -> anyhow::Result<()>,
) -> anyhow::Result<ContentGcTotals> {
    let mut conn = db::connect_readwrite(&services.database_url)?;
    // One lock session for the whole run: the project lock is leased per
    // candidate on it, so a backlog costs two hub connections rather than one
    // TLS session per version (#21085).
    let mut lock_conn = db::connect_readwrite(&services.database_url)?;
    let mut totals = ContentGcTotals::default();
    let mut project_contexts: HashMap<String, Option<Context>> = HashMap::new();
    // Projects whose graph facts were deleted get one orphan sweep after the
    // loop: `cleanup_orphans` is O(project graph size), and sweeping after
    // every version made a large backlog take hours instead of minutes (#21085).
    let mut graph_dirty_projects = BTreeSet::new();
    for (index, candidate) in candidates.iter().enumerate() {
        if deadline_reached() {
            totals.deferred_versions = candidates.len() - index;
            break;
        }
        let Some(_lock) = lease_project_lock(
            &mut lock_conn,
            &candidate.project_id,
            IndexLockPolicy::maintenance_try(),
        )?
        else {
            totals.busy_projects += 1;
            continue;
        };
        if !content_is_unreferenced(&mut conn, &candidate.id)? {
            continue;
        }

        // Projection endpoints are project-scoped configuration; the caller's
        // context may carry another project's (or the global) backends.
        let ctx = project_contexts
            .entry(candidate.project_id.clone())
            .or_insert_with(|| match resolve_project_services(&candidate.project_id) {
                Ok(ctx) => Some(ctx),
                Err(error) => {
                    log::warn!(
                        "skipping content GC for project {}: service context resolution failed: {error:#}",
                        candidate.project_id,
                    );
                    None
                }
            });
        let Some(ctx) = ctx.as_ref() else {
            totals.failed_versions += 1;
            continue;
        };

        // A store that is not configured on this machine cannot be cleaned
        // here. Deleting the SQL row anyway would strand this version's
        // content-scoped projections in the shared store forever, so retain the
        // row for a machine that has the store configured.
        let graph_unreachable =
            candidate.has_graph_facts && candidate.graph_synced && ctx.falkordb.is_none();
        let vectors_unreachable =
            !candidate.symbol_ids.is_empty() && candidate.vectors_synced && ctx.qdrant.is_none();
        if graph_unreachable || vectors_unreachable {
            log::warn!(
                "retaining content version {}:{}@{}: its projection store(s) are not configured on this machine",
                candidate.project_id,
                candidate.file_path,
                candidate.content_hash,
            );
            totals.skipped_versions += 1;
            continue;
        }

        if let Err(error) = delete_projections(ctx, candidate) {
            // Keep the authoritative pending-cleanup flags until every store
            // succeeds. Deletes are idempotent, so a later retry can safely
            // repeat a deletion that already completed in another store.
            log::warn!(
                "retaining sync flags for {}:{}@{} after projection delete failure: {error:#}",
                candidate.project_id,
                candidate.file_path,
                candidate.content_hash,
            );
            totals.failed_versions += 1;
            continue;
        }
        if candidate.has_graph_facts && ctx.falkordb.is_some() {
            graph_dirty_projects.insert(candidate.project_id.clone());
        }

        if delete_unreferenced_content_row(&mut conn, &candidate.id)? {
            totals.deleted_versions += 1;
            totals.deleted_symbols += candidate.symbol_ids.len();
        }
    }
    for project_id in &graph_dirty_projects {
        let Some(Some(ctx)) = project_contexts.get(project_id) else {
            continue;
        };
        if let Err(error) = sweep_graph_orphans(ctx) {
            log::warn!(
                "graph orphan sweep failed for project {project_id} after content GC: {error:#}"
            );
            totals.orphan_sweep_failures += 1;
        }
    }
    Ok(totals)
}

pub(super) fn delete_candidate_projections(
    ctx: &Context,
    candidate: &ContentGcCandidate,
) -> anyhow::Result<()> {
    if candidate.has_graph_facts && ctx.falkordb.is_some() {
        code_graph::delete_content_version(ctx, &candidate.file_path, &candidate.content_hash)
            .with_context(|| {
                format!(
                    "delete graph content version for {}:{}@{}",
                    candidate.project_id, candidate.file_path, candidate.content_hash
                )
            })?;
    }
    if let Some(qdrant) = ctx.qdrant.as_ref() {
        code_symbols::delete_symbol_vectors(qdrant, &candidate.project_id, &candidate.symbol_ids)
            .with_context(|| {
            format!(
                "delete vector symbols for {}:{}@{}",
                candidate.project_id, candidate.file_path, candidate.content_hash
            )
        })?;
    }
    Ok(())
}

fn reset_candidate_sync_flags(conn: &mut Client, indexed_file_id: &str) -> anyhow::Result<()> {
    conn.execute(
        "UPDATE code_indexed_files
            SET graph_synced = false, vectors_synced = false
          WHERE id = $1",
        &[&db::id_param(indexed_file_id)?],
    )?;
    Ok(())
}

pub(super) fn content_is_unreferenced(
    conn: &mut Client,
    indexed_file_id: &str,
) -> anyhow::Result<bool> {
    conn.query_one(
        "SELECT NOT EXISTS (
             SELECT 1
             FROM code_indexed_files f
             WHERE f.id = $1
               AND (
                   EXISTS (
                       SELECT 1 FROM code_indexed_file_states fs
                       WHERE fs.project_id = f.project_id
                         AND fs.file_path = f.file_path
                         AND fs.content_hash = f.content_hash
                   )
                   OR EXISTS (
                       SELECT 1 FROM code_overlay_pins p
                       WHERE p.source_project_id = f.project_id
                         AND p.file_path = f.file_path
                         AND p.content_hash = f.content_hash
                   )
               )
         )",
        &[&db::id_param(indexed_file_id)?],
    )?
    .try_get(0)
    .map_err(Into::into)
}

/// Called only while the caller owns the existing project index lock.
pub(super) fn delete_unreferenced_content_row(
    conn: &mut Client,
    indexed_file_id: &str,
) -> anyhow::Result<bool> {
    let deleted = conn.execute(
        "DELETE FROM code_indexed_files f
         WHERE f.id = $1
           AND NOT EXISTS (
               SELECT 1 FROM code_indexed_file_states fs
               WHERE fs.project_id = f.project_id
                 AND fs.file_path = f.file_path
                 AND fs.content_hash = f.content_hash
           )
           AND NOT EXISTS (
               SELECT 1 FROM code_overlay_pins p
               WHERE p.source_project_id = f.project_id
                 AND p.file_path = f.file_path
                 AND p.content_hash = f.content_hash
           )",
        &[&db::id_param(indexed_file_id)?],
    )?;
    if deleted == 0 {
        // A new reference after projection deletion needs reconstruction.
        reset_candidate_sync_flags(conn, indexed_file_id)?;
    }
    Ok(deleted > 0)
}

#[cfg(test)]
#[path = "content_gc/tests.rs"]
mod tests;
