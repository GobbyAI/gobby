use std::collections::{BTreeSet, HashMap, HashSet};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Instant;

use anyhow::Context as _;
use postgres::Client;

use crate::config::{Context, ServiceConfigSelection};
use crate::db;
use crate::graph::code_graph;
use crate::index::hasher;
use crate::index_lock::{IndexLockPolicy, lease_project_lock};
use crate::vector::code_symbols;

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
         GROUP BY f.id, f.project_id, f.file_path, f.content_hash,
                  f.graph_synced, f.vectors_synced, ps.root_path
         ORDER BY f.project_id, f.file_path, f.content_hash",
        &[&machine_id, &retention_days, &project_filter],
    )?;

    // Recent-history lookups are one git batch per repository root: every blob
    // reachable from a commit inside the retention window is hashed once, so
    // the cost is bounded by that history rather than by the candidate count
    // (two git subprocesses per candidate path timed out the daemon's prune on
    // a 4,800-path backlog, #21085).
    let mut history_cache: HashMap<PathBuf, Option<HashSet<String>>> = HashMap::new();
    let mut candidates = Vec::new();
    for row in rows {
        let root_path = PathBuf::from(row.try_get::<_, String>("root_path")?);
        let content_hash: String = row.try_get("content_hash")?;
        if !history_cache.contains_key(&root_path) {
            let looked_up = match recent_content_hashes_in_git_history(&root_path, retention_days) {
                Ok(hashes) => Some(hashes),
                Err(error) => {
                    log::warn!(
                        "retaining code-index content for project {} because recent git history in {} could not be inspected: {error:#}",
                        row.try_get::<_, String>("project_id")?,
                        root_path.display(),
                    );
                    None
                }
            };
            history_cache.insert(root_path.clone(), looked_up);
        }
        match history_cache.get(&root_path) {
            Some(Some(hashes)) if !hashes.contains(&content_hash) => {}
            // Protected by recent git history, or history unavailable: retain.
            _ => continue,
        }
        candidates.push(ContentGcCandidate {
            id: row.try_get("id")?,
            project_id: row.try_get("project_id")?,
            file_path: row.try_get("file_path")?,
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

fn delete_candidate_projections(
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

/// Content hashes of every blob reachable from a commit inside the retention
/// window, across all refs of the repository at `root_path`.
///
/// One `rev-list` plus one filtered `cat-file` batch per root, so the cost is
/// bounded by the window's history rather than by the number of candidate
/// paths. `--objects` lists a blob once, under the first path it was reached
/// through, so protection is by content: recent content anywhere in the
/// repository is retained, a superset of a per-path rule.
fn recent_content_hashes_in_git_history(
    root_path: &Path,
    retention_days: i32,
) -> anyhow::Result<HashSet<String>> {
    let since = format!("{retention_days}.days");
    let objects = Command::new("git")
        .arg("-C")
        .arg(root_path)
        .args([
            "rev-list",
            "--objects",
            "--filter=object:type=blob",
            "--all",
            "--since",
            &since,
        ])
        .output()
        .with_context(|| format!("run git rev-list in {}", root_path.display()))?;
    if !objects.status.success() {
        anyhow::bail!(
            "git rev-list failed: {}",
            String::from_utf8_lossy(&objects.stderr).trim()
        );
    }

    // Commits are listed bare and root trees with an empty path; both are
    // skipped. The type filter still lists annotated tags under their tag
    // name, which the response parser below discards by type. Each request
    // carries the path whose filters apply when the content is read back.
    let mut requests = Vec::new();
    for line in String::from_utf8(objects.stdout)?.lines() {
        if let Some((object_id, path)) = line.split_once(' ')
            && !path.is_empty()
        {
            requests.push((object_id.to_string(), path.to_string()));
        }
    }
    if requests.is_empty() {
        return Ok(HashSet::new());
    }

    let mut child = Command::new("git")
        .arg("-C")
        .arg(root_path)
        .args(["cat-file", "--batch", "--filters"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .with_context(|| format!("start filtered git blob batch in {}", root_path.display()))?;
    let mut stdin = child.stdin.take().context("open git cat-file stdin")?;
    let request_input = requests
        .iter()
        .map(|(object_id, path)| format!("{object_id} {path}\n"))
        .collect::<String>();
    let writer = std::thread::spawn(move || stdin.write_all(request_input.as_bytes()));
    let stdout = child.stdout.take().context("open git cat-file stdout")?;
    let mut reader = BufReader::new(stdout);
    // One response per request line, in order, until the batch ends: a
    // `<request> missing` line has no body; every other response is
    // `<oid> <type> <size>` followed by the body and a newline. Only blobs are
    // hashed; the body of any other type is discarded.
    let parsed = (|| -> anyhow::Result<HashSet<String>> {
        let mut hashes = HashSet::new();
        let mut header = String::new();
        loop {
            header.clear();
            if reader.read_line(&mut header)? == 0 {
                break;
            }
            let fields = header.split_whitespace().collect::<Vec<_>>();
            if fields.last() == Some(&"missing") {
                continue;
            }
            let [object_id, object_type, size] = fields[..] else {
                anyhow::bail!("unexpected git cat-file response: {header:?}");
            };
            let size = size
                .parse::<usize>()
                .with_context(|| format!("parse git object size for {object_id}"))?;
            let mut body = vec![0; size];
            reader.read_exact(&mut body)?;
            let mut delimiter = [0_u8; 1];
            reader.read_exact(&mut delimiter)?;
            if delimiter[0] != b'\n' {
                anyhow::bail!(
                    "git cat-file object {object_id} lacked a trailing newline delimiter"
                );
            }
            if object_type == "blob" {
                hashes.insert(hasher::content_hash(&body));
            }
        }
        Ok(hashes)
    })();
    let hashes = match parsed {
        Ok(hashes) => hashes,
        Err(error) => {
            drop(reader);
            let _ = child.kill();
            let _ = child.wait();
            let _ = writer.join();
            return Err(error);
        }
    };
    writer
        .join()
        .map_err(|_| anyhow::anyhow!("git cat-file input writer panicked"))?
        .context("write git cat-file batch input")?;
    let output = child
        .wait_with_output()
        .context("wait for git cat-file batch")?;
    if !output.status.success() {
        anyhow::bail!(
            "git cat-file failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(hashes)
}

#[cfg(test)]
#[path = "content_gc/tests.rs"]
mod tests;
