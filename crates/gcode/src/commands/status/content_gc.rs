use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::Context as _;
use postgres::Client;

use crate::config::Context;
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
         WHERE f.indexed_at < NOW() - make_interval(days => $2)
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

    let mut candidates = Vec::new();
    for row in rows {
        let root_path = PathBuf::from(row.try_get::<_, String>("root_path")?);
        let file_path: String = row.try_get("file_path")?;
        let content_hash: String = row.try_get("content_hash")?;
        match content_is_in_recent_git_history(
            &root_path,
            &file_path,
            &content_hash,
            retention_days,
        ) {
            Ok(true) => continue,
            Ok(false) => {}
            Err(error) => {
                log::warn!(
                    "retaining code-index content for {}:{} because recent git history could not be inspected: {error:#}",
                    row.try_get::<_, String>("project_id")?,
                    file_path,
                );
                continue;
            }
        }
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
    let mut conn = db::connect_readwrite(&services.database_url)?;
    let mut totals = ContentGcTotals::default();
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

        let mut ctx = services.clone();
        ctx.project_id.clone_from(&candidate.project_id);
        if ctx.falkordb.is_some() {
            code_graph::delete_symbol_ids(&ctx, &candidate.symbol_ids).with_context(|| {
                format!(
                    "delete graph symbols for {}:{}@{}",
                    candidate.project_id, candidate.file_path, candidate.content_hash
                )
            })?;
        }
        if let Some(qdrant) = ctx.qdrant.as_ref() {
            code_symbols::delete_symbol_vectors(
                qdrant,
                &candidate.project_id,
                &candidate.symbol_ids,
            )
            .with_context(|| {
                format!(
                    "delete vector symbols for {}:{}@{}",
                    candidate.project_id, candidate.file_path, candidate.content_hash
                )
            })?;
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
        }
    }
    Ok(totals)
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

fn content_is_in_recent_git_history(
    root_path: &Path,
    file_path: &str,
    content_hash: &str,
    retention_days: i32,
) -> anyhow::Result<bool> {
    let since = format!("{retention_days}.days");
    let revisions = Command::new("git")
        .arg("-C")
        .arg(root_path)
        .args(["rev-list", "--all", "--since", &since, "--", file_path])
        .output()
        .with_context(|| format!("run git rev-list in {}", root_path.display()))?;
    if !revisions.status.success() {
        anyhow::bail!(
            "git rev-list failed: {}",
            String::from_utf8_lossy(&revisions.stderr).trim()
        );
    }

    for revision in String::from_utf8(revisions.stdout)?.lines() {
        let blob = Command::new("git")
            .arg("-C")
            .arg(root_path)
            .args(["show", &format!("{revision}:{file_path}")])
            .output()
            .with_context(|| format!("read {file_path} from git revision {revision}"))?;
        if blob.status.success() && hasher::content_hash(&blob.stdout) == content_hash {
            return Ok(true);
        }
    }
    Ok(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recent_git_blob_protects_matching_content() -> anyhow::Result<()> {
        let repo = tempfile::tempdir()?;
        std::fs::write(repo.path().join("tracked.txt"), "retained\n")?;
        for args in [
            vec!["init"],
            vec!["add", "tracked.txt"],
            vec![
                "-c",
                "user.name=Gcode Test",
                "-c",
                "user.email=gcode@example.invalid",
                "commit",
                "-m",
                "seed",
            ],
        ] {
            let status = Command::new("git")
                .arg("-C")
                .arg(repo.path())
                .args(args)
                .status()?;
            assert!(status.success());
        }

        assert!(content_is_in_recent_git_history(
            repo.path(),
            "tracked.txt",
            &hasher::content_hash(b"retained\n"),
            30,
        )?);
        assert!(!content_is_in_recent_git_history(
            repo.path(),
            "tracked.txt",
            &hasher::content_hash(b"different\n"),
            30,
        )?);
        Ok(())
    }
}
