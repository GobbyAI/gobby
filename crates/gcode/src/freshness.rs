use std::collections::HashMap;
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
    /// This checkout cannot refresh the index, and these paths are stale: their
    /// visible rows serve bytes the worktree no longer has, or the worktree holds
    /// indexable files no row serves.
    Stale(Vec<String>),
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

    /// Warning for stale paths a read-only checkout reports, naming up to three.
    pub fn stale_warning(&self) -> Option<String> {
        let Self::Stale(paths) = self else {
            return None;
        };
        let shown = paths
            .iter()
            .take(3)
            .map(String::as_str)
            .collect::<Vec<_>>()
            .join(", ");
        let more = match paths.len().saturating_sub(3) {
            0 => String::new(),
            count => format!(", +{count} more"),
        };
        Some(format!(
            "warning: gcode index is stale for {} path(s) this checkout cannot refresh \
             ({shown}{more}); results for them may be out of date",
            paths.len()
        ))
    }
}

pub fn ensure_fresh(ctx: &Context, scope: FreshnessScope) -> anyhow::Result<FreshnessStatus> {
    if std::env::var_os(INFLIGHT_ENV).is_some() {
        return Ok(FreshnessStatus::Checked);
    }

    // Read-only parent and sibling scopes must never attempt index writes, and
    // must not serve rows the worktree has moved past without saying so.
    if !ctx.can_refresh_index()? {
        let mut conn = db::connect_readonly(&ctx.database_url)?;
        let files = match &scope {
            FreshnessScope::Project => None,
            FreshnessScope::Files(paths) => Some(paths.as_slice()),
        };
        let stale = stale_served_paths(&mut conn, ctx, files)?;
        return Ok(if stale.is_empty() {
            FreshnessStatus::Checked
        } else {
            FreshnessStatus::Stale(stale)
        });
    }

    // Lock-free pre-gate for whole-project reads: if nothing on disk is newer
    // than the recorded index and nothing was deleted, skip the advisory lock
    // and the full re-hash entirely (no "refresh already running" warning).
    // `FreshnessScope::Files` is already cheap (explicit-files path) and is left
    // untouched.
    if matches!(scope, FreshnessScope::Project) && !project_needs_refresh(ctx)? {
        return Ok(FreshnessStatus::Checked);
    }

    // Overlay explicit-file reads skip the refresh when every requested file
    // matches the version the overlay serves for it.
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

/// Paths a checkout must report as not current: visible rows whose
/// served bytes differ from the worktree, served paths that are gone, and
/// indexable files no row serves. `files` narrows the check to the requested
/// paths; `None` checks the whole checkout. Runs for writable checkouts too: a
/// refresh skipped on a busy lock or degraded by an error leaves served rows the
/// worktree has moved past, and only the disk comparison can report that.
pub(crate) fn stale_served_paths(
    conn: &mut postgres::Client,
    ctx: &Context,
    files: Option<&[PathBuf]>,
) -> anyhow::Result<Vec<String>> {
    let served: HashMap<String, String> = visibility::visible_tree(conn, ctx)?
        .into_iter()
        .map(|file| (file.file_path, file.content_hash))
        .collect();
    let excludes = &ctx.indexing.extra_excludes;
    let mut stale = match files {
        None => {
            let machine_id = db::id_param(&ctx.machine_id)?;
            let last_indexed_at = project_last_indexed_at(conn, &machine_id, &ctx.project_id)?;
            let options = crate::index::walker::DiscoveryOptions {
                respect_gitignore: ctx.indexing.respect_gitignore,
            };
            api::stale_paths(
                &ctx.project_root,
                last_indexed_at,
                &served,
                excludes,
                options,
            )
        }
        Some(files) => files
            .iter()
            .map(|path| crate::index::normalize_storage_path(path))
            .filter(|file| {
                api::explicit_file_changed(
                    &ctx.project_root,
                    file,
                    served.get(file).map(String::as_str),
                    excludes,
                )
            })
            .collect(),
    };
    stale.sort();
    stale.dedup();
    Ok(stale)
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
        ProjectIndexScope::Overlay { .. } => {
            // Overlay rows plus the pins they do not shadow. The parent's index
            // time does not change what the overlay serves, so it does not gate
            // freshness. Tombstoned paths drop out: a file deleted in the
            // worktree is known-absent, and one that reappears reads as new.
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

/// Read-only gate for explicit files in an overlay checkout. Each requested file
/// is judged on its own content: the worktree copy must match the version the
/// overlay serves for it, so no other file's refresh can certify it.
fn overlay_files_need_refresh(ctx: &Context, paths: &[PathBuf]) -> anyhow::Result<bool> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    for path in paths {
        let file = crate::index::normalize_storage_path(path);
        let served = overlay_served_content_hash(&mut conn, ctx, &file)?;
        if api::explicit_file_changed(
            &ctx.project_root,
            &file,
            served.as_deref(),
            &ctx.indexing.extra_excludes,
        ) {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Content hash of the version an overlay serves for `file_path`: its own row,
/// else the parent row it does not shadow. `None` when it serves no file there.
fn overlay_served_content_hash(
    conn: &mut postgres::Client,
    ctx: &Context,
    file_path: &str,
) -> anyhow::Result<Option<String>> {
    let ProjectIndexScope::Overlay {
        overlay_project_id, ..
    } = &ctx.index_scope
    else {
        return Ok(None);
    };
    let Some(machine_id) = visibility::machine_uuid_or_invisible(&ctx.machine_id) else {
        return Ok(None);
    };
    let Some(overlay_project_id) = visibility::project_uuid_or_invisible(overlay_project_id) else {
        return Ok(None);
    };
    // The selectors visibility reads: the overlay's own rows, then the pins for
    // paths it does not shadow. Parent rows never serve.
    let selectors = visibility::file_selectors_sql("$1", "$2", true);
    let row = conn.query_opt(
        &format!(
            "SELECT served.content_hash
             FROM ({selectors}) AS served
             WHERE served.file_path = $3
             LIMIT 1"
        ),
        &[&machine_id, &overlay_project_id, &file_path],
    )?;
    Ok(row.map(|row| row.try_get::<_, String>(0)).transpose()?)
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
