//! Post-write resolution of cross-file local-import calls.
//!
//! Parsing a file records each cross-file local import as a `local_import`
//! `CallRelation` carrying the original imported name plus the candidate target
//! files (derived by pure path logic — no file reads, no UUID compute). This
//! pass runs once per index run *after* every file's symbols and calls are
//! written, so `code_symbols` is fully current. For each pending call it looks
//! the target up by `(candidate files, original name)` and rewrites the row to a
//! `Symbol` target on a hit or `Unresolved` on a miss. JavaScript default
//! imports use a conservative fallback: exactly one top-level callable/type
//! symbol in the candidate files.
//!
//! Because the rewrite uses the real indexed symbol id (never a recomputed
//! UUID), a phantom `CALLS` edge to a non-existent symbol is structurally
//! impossible. The pass reads `code_symbols`, which is fully populated by the
//! time it runs, so resolution is order-independent: it does not matter whether
//! the caller or the callee file was indexed first.

use postgres::Client;

use crate::db;
use crate::index::api;
use crate::models::CallRelation;

/// Resolve every pending `local_import` call written for `file_paths` during this
/// run. Returns the number of calls promoted to a `Symbol` target.
///
/// Work is bounded by the calls in the changed file set (`O(changed-calls)`),
/// not the repository size — the candidate target files come from the call row
/// itself, so no project-wide file scan is performed.
pub(super) fn resolve_local_import_calls(
    conn: &mut Client,
    project_id: &str,
    file_paths: &[String],
) -> anyhow::Result<usize> {
    let pending = db::read_local_import_calls(conn, project_id, file_paths)?;
    resolve_pending_local_import_calls(conn, project_id, pending)
}

/// Resolve any pending `local_import` rows left from an earlier interrupted
/// promotion pass. Full, unfiltered indexes call this after the normal
/// changed-file pass so stale project rows are not stranded forever.
pub(super) fn resolve_project_local_import_calls(
    conn: &mut Client,
    project_id: &str,
) -> anyhow::Result<usize> {
    let pending = db::read_project_local_import_calls(conn, project_id)?;
    resolve_pending_local_import_calls(conn, project_id, pending)
}

fn resolve_pending_local_import_calls(
    conn: &mut Client,
    project_id: &str,
    pending: Vec<CallRelation>,
) -> anyhow::Result<usize> {
    let mut resolved_count = 0usize;
    for call in &pending {
        let candidate_files = call.local_import_candidate_files();
        let resolved_id = if call.local_import_uses_default_export_fallback() {
            db::resolve_default_import_symbol_id(conn, project_id, &candidate_files)?
        } else {
            db::resolve_local_callee_symbol_id(
                conn,
                project_id,
                &candidate_files,
                &call.callee_name,
            )?
        };
        let resolved = match resolved_id {
            Some(id) => {
                resolved_count += 1;
                resolved_symbol_call(call, id)
            }
            None => unresolved_call(call),
        };
        api::promote_local_import_call(conn, project_id, call, &resolved)?;
    }
    Ok(resolved_count)
}

/// A `local_import` call that matched a canonical symbol, rewritten to a `Symbol`
/// target with the candidate-file carrier cleared.
fn resolved_symbol_call(original: &CallRelation, callee_symbol_id: String) -> CallRelation {
    CallRelation::new(
        original.caller_symbol_id.clone(),
        original.callee_name.clone(),
        original.file_path.clone(),
        original.line,
    )
    .with_symbol_target(callee_symbol_id)
}

/// A `local_import` call that matched no canonical symbol, degraded to
/// `Unresolved` (the same outcome as before this resolution mechanism existed).
fn unresolved_call(original: &CallRelation) -> CallRelation {
    CallRelation::new(
        original.caller_symbol_id.clone(),
        original.callee_name.clone(),
        original.file_path.clone(),
        original.line,
    )
}
