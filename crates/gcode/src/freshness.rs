use std::path::PathBuf;
use std::time::SystemTime;

use crate::config::{Context, ProjectIndexScope};
use crate::db;
use crate::index::{api, hasher};
use crate::index_lock::{self, IndexLockPolicy, IndexLockResult};
use crate::models::Symbol;
use crate::visibility;

const INFLIGHT_ENV: &str = "GCODE_FRESHNESS_INFLIGHT";

pub enum FreshnessScope {
    Project,
    Files(Vec<PathBuf>),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FreshnessStatus {
    Checked,
    /// The refresh was skipped because another indexer holds the project lock,
    /// carrying that holder's identity when it could be read.
    SkippedBusy(Option<String>),
    Degraded(String),
}

impl FreshnessStatus {
    /// Warning text for a refresh skipped by a busy index lock, naming the
    /// holder when one was identified.
    ///
    /// Both CLIs that surface this warning render it here so the two cannot
    /// drift apart.
    pub fn busy_warning(&self) -> Option<String> {
        let Self::SkippedBusy(holder) = self else {
            return None;
        };
        Some(match holder {
            Some(holder) => format!(
                "warning: gcode index refresh already running; reading existing index ({holder})"
            ),
            None => {
                "warning: gcode index refresh already running; reading existing index".to_string()
            }
        })
    }
}

pub fn ensure_fresh(ctx: &Context, scope: FreshnessScope) -> anyhow::Result<FreshnessStatus> {
    if std::env::var_os(INFLIGHT_ENV).is_some() {
        return Ok(FreshnessStatus::Checked);
    }

    // Read-only parent and sibling scopes must never attempt index writes.
    if !ctx.can_refresh_index()? {
        return Ok(FreshnessStatus::Checked);
    }

    // Lock-free pre-gate for whole-project reads: if nothing on disk is newer
    // than the recorded index and nothing was deleted, skip the advisory lock
    // and the full re-hash entirely (no "refresh already running" warning).
    // `FreshnessScope::Files` is already cheap (explicit-files path) and is left
    // untouched.
    if matches!(scope, FreshnessScope::Project) && !project_needs_refresh(ctx)? {
        return Ok(FreshnessStatus::Checked);
    }

    // Overlay explicit-file reads are served from the index unless the overlay's
    // last stamp no longer covers the parent or the requested files.
    if let FreshnessScope::Files(paths) = &scope
        && matches!(ctx.index_scope, ProjectIndexScope::Overlay { .. })
        && !overlay_files_need_refresh(ctx, paths)?
    {
        return Ok(FreshnessStatus::Checked);
    }

    let _guard = FreshnessGuard::enter();
    match scope {
        FreshnessScope::Project => {
            let result =
                index_lock::with_project_lock(ctx, IndexLockPolicy::brief_freshness_try(), || {
                    Ok(refresh_files(ctx, Vec::new()).map_err(|error| degraded_detail(&error)))
                })?;
            Ok(freshness_from_lock(result))
        }
        FreshnessScope::Files(paths) => {
            if paths.is_empty() {
                return Ok(FreshnessStatus::Checked);
            }
            let locks = index_lock::lock_project_files(
                ctx,
                &paths,
                IndexLockPolicy::brief_freshness_try(),
            )?;
            if locks.acquired_files.is_empty() {
                return Ok(FreshnessStatus::SkippedBusy(None));
            }
            match refresh_files(ctx, locks.acquired_files.clone()) {
                Ok(()) if locks.busy_files.is_empty() => Ok(FreshnessStatus::Checked),
                Ok(()) => Ok(FreshnessStatus::SkippedBusy(None)),
                Err(error) => Ok(FreshnessStatus::Degraded(degraded_detail(&error))),
            }
        }
    }
}

/// Degraded detail for a failed refresh. A hub failure's `postgres::Error`
/// displays only "db error"; the SQL cause (an RLS denial, say) is its source.
fn degraded_detail(error: &anyhow::Error) -> String {
    format!("{error:#}")
}

fn refresh_files(ctx: &Context, explicit_files: Vec<PathBuf>) -> anyhow::Result<()> {
    api::index_files_for_freshness(
        api::IndexRequest {
            project_root: ctx.project_root.clone(),
            path_filter: None,
            explicit_files,
            full: false,
            require_cpp_semantics: false,
            sync_projections: false,
        },
        ctx,
        api::IndexOptions::default(),
    )?;
    Ok(())
}

fn freshness_from_lock(result: IndexLockResult<Result<(), String>>) -> FreshnessStatus {
    match result {
        IndexLockResult::Acquired(Ok(())) => FreshnessStatus::Checked,
        IndexLockResult::Acquired(Err(message)) => FreshnessStatus::Degraded(message),
        IndexLockResult::Busy(holder) => FreshnessStatus::SkippedBusy(holder),
    }
}

/// Read-only pre-gate for whole-project freshness.
///
/// Returns `true` when the project must be reconciled under the advisory lock —
/// because it has never been indexed or because the on-disk tree changed since
/// `last_indexed_at` — and `false` when the recorded index is already current
/// and the lock (plus the full re-hash) can be skipped. Reads only; needs the
/// hub exactly like the existing refresh path, and propagates a hub error the
/// same way (`--allow-stale` still bypasses it upstream).
fn project_needs_refresh(ctx: &Context) -> anyhow::Result<bool> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let machine_id = db::id_param(&ctx.machine_id)?;

    // Never indexed (or no recorded timestamp): do not gate; let the existing
    // refresh path build the first index.
    let Some(last_indexed_at) = project_last_indexed_at(&mut conn, &machine_id, &ctx.project_id)?
    else {
        return Ok(true);
    };

    let indexed_paths = match &ctx.index_scope {
        ProjectIndexScope::Single => {
            db::list_indexed_file_paths(&mut conn, &ctx.project_id, &ctx.machine_id)?
        }
        ProjectIndexScope::Overlay {
            parent_project_id, ..
        } => {
            // The overlay serves parent rows for every path it does not shadow,
            // so a parent index newer than the overlay's own changes what the
            // overlay reads without touching a worktree mtime.
            let parent_indexed_at =
                project_last_indexed_at(&mut conn, &machine_id, parent_project_id)?;
            if parent_indexed_at.is_some_and(|parent| parent > last_indexed_at) {
                return Ok(true);
            }
            // Overlay rows plus the parent rows they do not shadow. Tombstoned
            // paths drop out: a file deleted in the worktree is known-absent,
            // and one that reappears reads as new.
            visibility::visible_tree(&mut conn, ctx)?
                .into_iter()
                .map(|file| file.file_path)
                .collect()
        }
    };
    drop(conn);

    Ok(api::project_changed_since(
        &ctx.project_root,
        last_indexed_at,
        &indexed_paths,
        &ctx.indexing.extra_excludes,
        crate::index::walker::DiscoveryOptions {
            respect_gitignore: ctx.indexing.respect_gitignore,
        },
    ))
}

fn project_last_indexed_at(
    conn: &mut postgres::Client,
    machine_id: &uuid::Uuid,
    project_id: &str,
) -> anyhow::Result<Option<SystemTime>> {
    let row = conn.query_opt(
        "SELECT last_indexed_at
         FROM code_indexed_project_states
         WHERE machine_id = $1 AND project_id = $2",
        &[machine_id, &db::id_param(project_id)?],
    )?;
    match row {
        Some(row) => Ok(row.try_get::<_, Option<SystemTime>>(0)?),
        None => Ok(None),
    }
}

/// Read-only gate for explicit files in an overlay checkout. Returns `false`
/// when the overlay has completed an index, no parent write landed after its
/// stamp, and every requested file still matches what the overlay recorded.
fn overlay_files_need_refresh(ctx: &Context, paths: &[PathBuf]) -> anyhow::Result<bool> {
    let ProjectIndexScope::Overlay {
        parent_project_id, ..
    } = &ctx.index_scope
    else {
        return Ok(true);
    };
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let machine_id = db::id_param(&ctx.machine_id)?;

    // Explicit-file refreshes do not advance `last_indexed_at`, so the overlay
    // watermark is its stamp, which only a successful stats write moves.
    let has_full_index =
        project_last_indexed_at(&mut conn, &machine_id, &ctx.project_id)?.is_some();
    let Some(watermark) = project_stamped_at(&mut conn, &machine_id, &ctx.project_id)? else {
        return Ok(true);
    };
    if !has_full_index {
        return Ok(true);
    }
    let parent_stamp = project_stamped_at(&mut conn, &machine_id, parent_project_id)?;
    if parent_stamp.is_some_and(|parent| parent > watermark) {
        return Ok(true);
    }

    for path in paths {
        let file = crate::index::normalize_storage_path(path);
        let indexed = visibility::indexed_file_exists(&mut conn, ctx, &file);
        if api::explicit_file_changed_since(
            &ctx.project_root,
            watermark,
            &file,
            indexed,
            &ctx.indexing.extra_excludes,
        ) {
            return Ok(true);
        }
    }
    Ok(false)
}

fn project_stamped_at(
    conn: &mut postgres::Client,
    machine_id: &uuid::Uuid,
    project_id: &str,
) -> anyhow::Result<Option<SystemTime>> {
    let row = conn.query_opt(
        "SELECT updated_at
         FROM code_indexed_project_states
         WHERE machine_id = $1 AND project_id = $2",
        &[machine_id, &db::id_param(project_id)?],
    )?;
    match row {
        Some(row) => Ok(Some(row.try_get::<_, SystemTime>(0)?)),
        None => Ok(None),
    }
}

pub fn ensure_symbol_fresh(ctx: &Context, id: &str) -> anyhow::Result<FreshnessStatus> {
    if std::env::var_os(INFLIGHT_ENV).is_some() {
        return Ok(FreshnessStatus::Checked);
    }

    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let sym = visibility::visible_symbol_by_id(&mut conn, ctx, id)?;
    drop(conn);

    let Some(sym) = sym else {
        return Ok(FreshnessStatus::Checked);
    };

    if symbol_slice_is_current(ctx, &sym) {
        return Ok(FreshnessStatus::Checked);
    }

    ensure_fresh(
        ctx,
        FreshnessScope::Files(vec![PathBuf::from(&sym.file_path)]),
    )
}

fn symbol_slice_is_current(ctx: &Context, sym: &Symbol) -> bool {
    if sym.content_hash.is_empty() {
        return false;
    }

    let file_path = ctx.project_root.join(&sym.file_path);
    let source = match std::fs::read(file_path) {
        Ok(source) => source,
        Err(_) => return false,
    };

    hasher::symbol_content_hash(&source, sym.byte_start, sym.byte_end)
        .map(|hash| hash == sym.content_hash)
        .unwrap_or(false)
}

struct FreshnessGuard;

impl FreshnessGuard {
    fn enter() -> Self {
        // SAFETY: gcode runs freshness indexing synchronously in this CLI process
        // and restores the variable before returning to command dispatch.
        unsafe { std::env::set_var(INFLIGHT_ENV, "1") };
        Self
    }
}

impl Drop for FreshnessGuard {
    fn drop(&mut self) {
        // SAFETY: see FreshnessGuard::enter.
        unsafe { std::env::remove_var(INFLIGHT_ENV) };
    }
}

#[cfg(test)]
#[path = "freshness/bounded_refresh_tests.rs"]
mod bounded_refresh_tests;

#[cfg(test)]
#[path = "freshness/tests.rs"]
mod tests;
