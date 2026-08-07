use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::Context as _;
use postgres::Client;

use crate::config::{Context, ServiceConfigSelection};
use crate::db;
use crate::graph::code_graph;
use crate::index::hasher;
use crate::index_lock::{IndexLockPolicy, lock_project_by_id};
use crate::vector::code_symbols;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct ContentGcCandidate {
    pub(super) id: String,
    pub(super) project_id: String,
    pub(super) file_path: String,
    pub(super) content_hash: String,
    pub(super) symbol_ids: Vec<String>,
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub(super) struct ContentGcTotals {
    pub(super) deleted_versions: usize,
    pub(super) deleted_symbols: usize,
    pub(super) busy_projects: usize,
    pub(super) failed_versions: usize,
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
         GROUP BY f.id, f.project_id, f.file_path, f.content_hash, ps.root_path
         ORDER BY f.project_id, f.file_path, f.content_hash",
        &[&machine_id, &retention_days, &project_filter],
    )?;

    // Historical-hash lookups are cached per (root, path): candidates that share
    // a file spawn one git subprocess batch instead of one per content version.
    let mut history_cache: HashMap<(PathBuf, String), Option<HashSet<String>>> = HashMap::new();
    let mut candidates = Vec::new();
    for row in rows {
        let root_path = PathBuf::from(row.try_get::<_, String>("root_path")?);
        let file_path: String = row.try_get("file_path")?;
        let content_hash: String = row.try_get("content_hash")?;
        let key = (root_path, file_path);
        if !history_cache.contains_key(&key) {
            let looked_up = match recent_content_hashes_in_git_history(
                &key.0,
                &key.1,
                retention_days,
            ) {
                Ok(hashes) => Some(hashes),
                Err(error) => {
                    log::warn!(
                        "retaining code-index content for {}:{} because recent git history could not be inspected: {error:#}",
                        row.try_get::<_, String>("project_id")?,
                        key.1,
                    );
                    None
                }
            };
            history_cache.insert(key.clone(), looked_up);
        }
        match history_cache.get(&key) {
            Some(Some(hashes)) if !hashes.contains(&content_hash) => {}
            // Protected by recent git history, or history unavailable: retain.
            _ => continue,
        }
        let (_, file_path) = key;
        candidates.push(ContentGcCandidate {
            id: row.try_get("id")?,
            project_id: row.try_get("project_id")?,
            file_path,
            content_hash,
            symbol_ids: row.try_get("symbol_ids")?,
        });
    }
    Ok(candidates)
}

pub(super) fn prune_content_versions(
    services: &Context,
    candidates: &[ContentGcCandidate],
) -> anyhow::Result<ContentGcTotals> {
    prune_content_versions_with(services, candidates, |project_id| {
        Context::resolve_for_project_id_with_services(
            project_id,
            services.quiet,
            ServiceConfigSelection::projection_cleanup(),
        )
    })
}

fn prune_content_versions_with(
    services: &Context,
    candidates: &[ContentGcCandidate],
    resolve_project_services: impl Fn(&str) -> anyhow::Result<Context>,
) -> anyhow::Result<ContentGcTotals> {
    let mut conn = db::connect_readwrite(&services.database_url)?;
    let mut totals = ContentGcTotals::default();
    let mut project_contexts: HashMap<String, Option<Context>> = HashMap::new();
    for candidate in candidates {
        let Some(_lock) = lock_project_by_id(
            &services.database_url,
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

        if let Err(error) = delete_candidate_projections(ctx, candidate) {
            // The projections are now partially deleted while the SQL row still
            // says synced; reset the flags so a re-sync or retry reconverges.
            log::warn!(
                "resetting sync flags for {}:{}@{} after projection delete failure: {error:#}",
                candidate.project_id,
                candidate.file_path,
                candidate.content_hash,
            );
            reset_candidate_sync_flags(&mut conn, &candidate.id)?;
            totals.failed_versions += 1;
            continue;
        }

        let deleted = conn.execute(
            "DELETE FROM code_indexed_files f
             WHERE f.id = $1
               AND NOT EXISTS (
                   SELECT 1 FROM code_indexed_file_states fs
                   WHERE fs.project_id = f.project_id
                     AND fs.file_path = f.file_path
                     AND fs.content_hash = f.content_hash
               )",
            &[&db::id_param(&candidate.id)?],
        )?;
        if deleted > 0 {
            totals.deleted_versions += 1;
            totals.deleted_symbols += candidate.symbol_ids.len();
        } else {
            // The row became referenced again after the unreferenced check, but
            // its projections were just deleted; flag it for re-sync.
            reset_candidate_sync_flags(&mut conn, &candidate.id)?;
        }
    }
    Ok(totals)
}

fn delete_candidate_projections(
    ctx: &Context,
    candidate: &ContentGcCandidate,
) -> anyhow::Result<()> {
    if ctx.falkordb.is_some() {
        code_graph::delete_symbol_ids(ctx, &candidate.symbol_ids).with_context(|| {
            format!(
                "delete graph symbols for {}:{}@{}",
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

fn content_is_unreferenced(conn: &mut Client, indexed_file_id: &str) -> anyhow::Result<bool> {
    conn.query_one(
        "SELECT NOT EXISTS (
             SELECT 1
             FROM code_indexed_files f
             JOIN code_indexed_file_states fs
               ON fs.project_id = f.project_id
              AND fs.file_path = f.file_path
              AND fs.content_hash = f.content_hash
             WHERE f.id = $1
         )",
        &[&db::id_param(indexed_file_id)?],
    )?
    .try_get(0)
    .map_err(Into::into)
}

fn recent_content_hashes_in_git_history(
    root_path: &Path,
    file_path: &str,
    retention_days: i32,
) -> anyhow::Result<HashSet<String>> {
    let since = format!("{retention_days}.days");
    let objects = Command::new("git")
        .arg("-C")
        .arg(root_path)
        .args([
            "rev-list",
            "--objects",
            "--all",
            "--since",
            &since,
            "--",
            file_path,
        ])
        .output()
        .with_context(|| format!("run git rev-list in {}", root_path.display()))?;
    if !objects.status.success() {
        anyhow::bail!(
            "git rev-list failed: {}",
            String::from_utf8_lossy(&objects.stderr).trim()
        );
    }

    // --objects lists each blob once, so ping-ponging content versions cost one
    // cat-file per unique blob instead of one git show per revision.
    let mut hashes = HashSet::new();
    for line in String::from_utf8(objects.stdout)?.lines() {
        let Some((object_id, path)) = line.split_once(' ') else {
            continue;
        };
        if path != file_path {
            continue;
        }
        let blob = Command::new("git")
            .arg("-C")
            .arg(root_path)
            .args(["cat-file", "blob", object_id])
            .output()
            .with_context(|| format!("read git blob {object_id} for {file_path}"))?;
        if blob.status.success() {
            hashes.insert(hasher::content_hash(&blob.stdout));
        }
    }
    Ok(hashes)
}

#[cfg(test)]
#[path = "content_gc/tests.rs"]
mod tests;
