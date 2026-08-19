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

use std::collections::BTreeSet;

use postgres::Client;

use crate::db;
use crate::index::api;
use crate::models::{CallRelation, CallTargetKind, InheritanceRelation};

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

/// Promote pending LocalImport inheritance rows whose owner file or candidate
/// provider just appeared. Misses stay LocalImport with their candidate carrier.
/// Returns distinct owning file paths whose rows changed.
pub(super) fn resolve_local_import_inheritance(
    conn: &mut Client,
    project_id: &str,
    trigger_paths: &[String],
) -> anyhow::Result<Vec<String>> {
    if trigger_paths.is_empty() {
        return Ok(Vec::new());
    }
    let mut pending = db::read_local_import_inheritance(conn, project_id, trigger_paths)?;
    let extra = db::read_project_local_import_inheritance(conn, project_id)?;
    pending.extend(
        extra
            .into_iter()
            .filter(|row| inheritance_names_trigger(row, trigger_paths)),
    );
    dedup_inheritance(&mut pending);
    resolve_pending_local_import_inheritance(conn, project_id, pending)
}

#[allow(dead_code)]
pub(super) fn resolve_project_local_import_inheritance(
    conn: &mut Client,
    project_id: &str,
) -> anyhow::Result<Vec<String>> {
    let pending = db::read_project_local_import_inheritance(conn, project_id)?;
    resolve_pending_local_import_inheritance(conn, project_id, pending)
}

fn inheritance_names_trigger(row: &InheritanceRelation, trigger_paths: &[String]) -> bool {
    if trigger_paths.iter().any(|path| path == &row.file_path) {
        return true;
    }
    row.source_local_import_candidate_files()
        .into_iter()
        .chain(row.target_local_import_candidate_files())
        .any(|candidate| trigger_paths.iter().any(|path| path == &candidate))
}

fn dedup_inheritance(rows: &mut Vec<InheritanceRelation>) {
    let mut seen = BTreeSet::new();
    rows.retain(|row| {
        seen.insert((
            row.file_path.clone(),
            row.content_hash.clone(),
            row.source_name.clone(),
            row.target_name.clone(),
            row.heritage_kind.as_rel_type(),
            row.line,
            row.source_kind.as_str(),
            row.target_kind.as_str(),
            row.source_external_module.clone(),
            row.target_external_module.clone(),
        ))
    });
}

const INHERITANCE_PROMOTION_BATCH: usize = 32;

fn resolve_pending_local_import_inheritance(
    conn: &mut Client,
    project_id: &str,
    pending: Vec<InheritanceRelation>,
) -> anyhow::Result<Vec<String>> {
    let mut owners = BTreeSet::new();
    let mut to_promote = Vec::new();
    for original in pending {
        let mut updated = original.clone();
        let mut changed = false;
        if original.source_kind == CallTargetKind::LocalImport {
            let candidates = original.source_local_import_candidate_files();
            if let Some(id) = db::resolve_local_callee_symbol_id(
                conn,
                project_id,
                &candidates,
                &original.source_name,
            )? {
                updated.source_symbol_id = Some(id);
                updated.source_kind = CallTargetKind::Symbol;
                updated.source_external_module = None;
                changed = true;
            }
        }
        if original.target_kind == CallTargetKind::LocalImport {
            let candidates = original.target_local_import_candidate_files();
            if let Some(id) = db::resolve_local_callee_symbol_id(
                conn,
                project_id,
                &candidates,
                &original.target_name,
            )? {
                updated.target_symbol_id = Some(id);
                updated.target_kind = CallTargetKind::Symbol;
                updated.target_external_module = None;
                changed = true;
            }
        }
        if changed {
            to_promote.push((original, updated));
        }
    }
    for chunk in to_promote.chunks(INHERITANCE_PROMOTION_BATCH) {
        let mut tx = conn.transaction()?;
        for (original, updated) in chunk {
            api::promote_inheritance_row(&mut tx, project_id, original, updated)?;
            db::dirty_graph_sync_for_file(
                &mut tx,
                project_id,
                &original.file_path,
                &original.content_hash,
            )?;
        }
        tx.commit()?;
        for (original, _) in chunk {
            owners.insert(original.file_path.clone());
        }
    }
    Ok(owners.into_iter().collect())
}
