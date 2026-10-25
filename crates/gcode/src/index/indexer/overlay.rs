use std::collections::{BTreeSet, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use anyhow::Context as _;
use postgres::Client;
use wait_timeout::ChildExt;

use crate::cli_error::CliError;
use crate::config::{Context, ProjectIndexScope, parent_code_index_exists};
use crate::db;
use crate::index::api;
use crate::index::{parser, walker};
use crate::models::IndexedFile;
use crate::project::read_isolation_marker;
use crate::projection::sync::{self, ProjectionTarget, SilentProjectionProgress};
use crate::visibility;

use super::file::{
    ExplicitFileRoute, create_semantic_resolver_if_needed, index_content_only, index_file,
};
use super::lifecycle::{attach_projection_sync, refresh_communities, refresh_project_stats};
use super::local_imports::{resolve_local_import_calls, resolve_local_import_inheritance};
use super::pipeline::{explicit_route_with_discovery_options, index_discovered_files};
use super::selector_blob::{self, HeadBlobs};
use super::sink::{CodeFactSink, PostgresCodeFactSink};
use super::types::{
    IndexDegradation, IndexOptions, IndexOutcome, IndexRequest, IndexTarget, OverlayIndexMetadata,
};
use super::util::{
    effective_excludes, epoch_secs_str, relative_path, requested_relative_path,
    unsupported_file_types,
};

const GIT_STATUS_TIMEOUT_ENV: &str = "GCODE_GIT_STATUS_TIMEOUT_SECS";
const DEFAULT_GIT_STATUS_TIMEOUT_SECS: u64 = 5;

#[derive(Debug, Clone)]
pub(super) struct IndexedFileState {
    pub(super) content_hash: String,
    pub(super) language: String,
    pub(super) git_blob_id: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum OverlayReconcileAction {
    Index,
    Inherit,
    Tombstone,
    DeleteOverlay,
    Skip,
}

pub(super) fn overlay_reconcile_action(
    file_exists: bool,
    current_hash: Option<&str>,
    parent: Option<&IndexedFileState>,
    overlay: Option<&IndexedFileState>,
    indexable: bool,
) -> OverlayReconcileAction {
    if !file_exists {
        return if parent.is_some() {
            if overlay.is_some_and(|state| visibility::is_tombstone_language(&state.language)) {
                OverlayReconcileAction::Skip
            } else {
                OverlayReconcileAction::Tombstone
            }
        } else if overlay.is_some() {
            OverlayReconcileAction::DeleteOverlay
        } else {
            OverlayReconcileAction::Skip
        };
    }

    if parent.is_some_and(|state| Some(state.content_hash.as_str()) == current_hash) {
        return if overlay.is_some() {
            OverlayReconcileAction::Inherit
        } else {
            OverlayReconcileAction::Skip
        };
    }

    if indexable {
        OverlayReconcileAction::Index
    } else if parent.is_some() {
        OverlayReconcileAction::Tombstone
    } else {
        OverlayReconcileAction::Skip
    }
}

pub(super) fn index_overlay_files(
    conn: &mut Client,
    request: &IndexRequest,
    ctx: &Context,
    options: &mut IndexOptions<'_>,
) -> anyhow::Result<IndexOutcome> {
    let mut timings = super::IndexTimings::new("overlay.parent_index");
    let ProjectIndexScope::Overlay {
        overlay_project_id,
        overlay_root,
        parent_project_id,
        parent_root,
    } = &ctx.index_scope
    else {
        anyhow::bail!("overlay indexer requires an overlay project context");
    };

    // Degradations from the parent-first pass belong to this overlay's outcome.
    let mut parent_degraded = Vec::new();
    if !parent_code_index_exists(conn, parent_project_id, &ctx.machine_id)? {
        // An overlay-bound managed grant cannot write the parent's rows, so refuse
        // before any write rather than attempting the parent-first pass.
        if !ctx.can_write_project_index(conn, parent_project_id)? {
            return Err(CliError::parent_index_required(
                parent_root,
                parent_project_id,
                overlay_root,
            )
            .into());
        }
        // Index the parent first so the overlay pass has its rows to read. Fully
        // projected unchanged parent files are adopted rather than re-parsed.
        let parent_ctx = ctx.rebased(
            parent_root.clone(),
            parent_project_id.clone(),
            ProjectIndexScope::Single,
        );
        let parent_request = IndexRequest {
            project_root: parent_root.clone(),
            path_filter: None,
            explicit_files: Vec::new(),
            full: false,
            require_cpp_semantics: request.require_cpp_semantics,
            sync_projections: request.sync_projections,
        };
        let parent_outcome = index_discovered_files(conn, &parent_request, &parent_ctx, options)?;
        if request.sync_projections {
            parent_degraded.extend(sync_parent_projections(&parent_ctx, &parent_outcome));
        }
        parent_degraded.extend(parent_outcome.degraded);
    }

    timings.phase("overlay.seed");

    let start = Instant::now();
    let discovery_start = Instant::now();
    let root_path = &request.project_root;
    let head_blobs = HeadBlobs::read(root_path);
    let target = IndexTarget {
        project_id: overlay_project_id,
        root_path,
        mode: api::IndexWriteMode::Overlay,
        head_blobs: &head_blobs,
        machine_id: &ctx.machine_id,
    };
    let machine_id = ctx.machine_id.as_str();
    api::upsert_project_seed(
        conn,
        machine_id,
        overlay_project_id,
        root_path,
        api::IndexWriteMode::Overlay,
    )?;
    let mut outcome = IndexOutcome::new(overlay_project_id);
    outcome.degraded = parent_degraded;
    outcome.overlay = Some(OverlayIndexMetadata {
        overlay_project_id: overlay_project_id.clone(),
        overlay_root: overlay_root.to_string_lossy().to_string(),
        parent_project_id: parent_project_id.clone(),
        parent_root: parent_root.to_string_lossy().to_string(),
    });

    timings.phase("overlay.discovery");
    let excludes = effective_excludes(&ctx.indexing.extra_excludes);
    let explicit_request = !request.explicit_files.is_empty();
    let explicit_rels = request
        .explicit_files
        .iter()
        .map(|path| requested_relative_path(root_path, path))
        .collect::<Vec<_>>();
    let state_filter = explicit_request.then_some(explicit_rels.as_slice());
    // The base each overlay path reconciles against is its pin, never the
    // parent's moving rows.
    let parent_files = pinned_base_states(conn, machine_id, overlay_project_id, state_filter)?;
    let overlay_files = indexed_file_states(conn, machine_id, overlay_project_id, state_filter)?;

    // `None` means complete discovery is required: `--full`, no pin provenance,
    // or a Git failure.
    let divergence = overlay_divergence(request, root_path);
    let candidate_only = !explicit_request && divergence.is_some();
    let (mut ast_by_rel, mut content_by_rel, mut import_candidates) = if explicit_request {
        let (ast_by_rel, content_by_rel) = explicit_overlay_file_maps(
            request,
            root_path,
            &excludes,
            ctx.indexing.respect_gitignore,
        );
        let mut seen = HashSet::new();
        let mut import_candidates =
            db::list_indexed_file_paths(conn, parent_project_id, machine_id)?
                .into_iter()
                .chain(db::list_indexed_file_paths(
                    conn,
                    overlay_project_id,
                    machine_id,
                )?)
                .map(|path| root_path.join(path))
                .filter(|path| seen.insert(path.clone()))
                .collect::<Vec<_>>();
        import_candidates.extend(
            ast_by_rel
                .values()
                .filter(|path| seen.insert((*path).clone()))
                .cloned(),
        );
        (ast_by_rel, content_by_rel, import_candidates)
    } else if candidate_only {
        // Discovery never runs. Unchanged providers are the pinned and owned
        // paths already loaded here; the routed candidates join them below.
        let providers: Vec<PathBuf> = parent_files
            .keys()
            .chain(overlay_files.keys())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .map(|rel| root_path.join(rel))
            .collect();
        (HashMap::new(), HashMap::new(), providers)
    } else {
        let (candidates, content_only) = walker::discover_files_with_options(
            root_path,
            &excludes,
            walker::DiscoveryOptions {
                respect_gitignore: ctx.indexing.respect_gitignore,
            },
        );
        let ast_by_rel = paths_by_relative(root_path, &candidates);
        let content_by_rel = paths_by_relative(root_path, &content_only);
        (ast_by_rel, content_by_rel, candidates)
    };
    timings.phase("overlay.reconcile");
    let mut rels = overlay_reconcile_paths(
        request,
        root_path,
        divergence.as_ref(),
        &ast_by_rel,
        &content_by_rel,
        &parent_files,
        &overlay_files,
    );
    if let Some(filter) = request.path_filter.as_deref() {
        rels.retain(|rel| rel_matches_filter(root_path, filter, rel));
    }
    if candidate_only {
        (ast_by_rel, content_by_rel) =
            route_candidates(root_path, &rels, &excludes, ctx.indexing.respect_gitignore);
        let known: HashSet<PathBuf> = import_candidates.iter().cloned().collect();
        import_candidates.extend(
            ast_by_rel
                .values()
                .filter(|path| !known.contains(*path))
                .cloned(),
        );
    }
    // Import resolution is only read when an AST candidate is parsed.
    timings.phase("overlay.import_context");
    let import_context = if ast_by_rel.is_empty() {
        Default::default()
    } else {
        parser::build_import_resolution_context(root_path, &import_candidates)
    };
    let unsupported_paths = rels
        .iter()
        .filter_map(|rel| content_by_rel.get(rel).cloned())
        .collect::<Vec<_>>();
    outcome.set_unsupported_file_types(unsupported_file_types(root_path, &unsupported_paths));

    outcome.scanned_files = rels.len();
    outcome.durations.discovery_ms = discovery_start.elapsed().as_millis() as u64;

    let mut hash_by_rel = HashMap::new();
    let mut clean_blob_by_rel = HashMap::new();
    for rel in &rels {
        let abs = root_path.join(rel);
        if abs.exists()
            && let Ok(snapshot) = selector_blob::read_snapshot(&abs, rel, &head_blobs)
        {
            if let Some(clean_blob) = snapshot.clean_blob {
                clean_blob_by_rel.insert(rel.clone(), clean_blob);
            }
            hash_by_rel.insert(rel.clone(), snapshot.content_hash);
        }
    }

    let mut ast_reindex = Vec::new();
    for rel in &rels {
        let Some(path) = ast_by_rel.get(rel) else {
            continue;
        };
        let differs_from_parent = match (hash_by_rel.get(rel), parent_files.get(rel)) {
            (Some(hash), Some(parent)) => hash != &parent.content_hash,
            _ => true,
        };
        if differs_from_parent {
            ast_reindex.push(path.clone());
        }
    }
    let mut semantic_resolver =
        create_semantic_resolver_if_needed(root_path, &ast_reindex, request.require_cpp_semantics)?;

    timings.phase("overlay.files");
    let indexing_start = Instant::now();
    let mut adopted_paths = Vec::new();
    for rel in rels {
        let abs = root_path.join(&rel);
        let parent = parent_files.get(&rel);
        let overlay = overlay_files.get(&rel);
        let current_hash = hash_by_rel.get(&rel).map(String::as_str);
        let indexable = ast_by_rel.contains_key(&rel) || content_by_rel.contains_key(&rel);
        // An overlay row that already carries the file's current content is a
        // finished shadow: re-parsing it changes nothing. Read-path refreshes
        // run with sync_projections=false, so adoption below (which requires
        // synced projections) would fail and re-index every diverged file on
        // every read. `--full` keeps the re-parse escape hatch.
        let overlay_is_current = !request.full
            && overlay.is_some_and(|state| Some(state.content_hash.as_str()) == current_hash);
        let action = if overlay_is_current {
            OverlayReconcileAction::Skip
        } else {
            overlay_reconcile_action(abs.exists(), current_hash, parent, overlay, indexable)
        };
        // Reuse an existing overlay content version instead of re-parsing;
        // `--full` forces the re-parse escape hatch just like the primary
        // pipeline.
        if !request.full
            && indexable
            && matches!(action, OverlayReconcileAction::Index)
            && let Some(hash) = current_hash
            && api::file_state::adopt_file_state(
                conn,
                machine_id,
                overlay_project_id,
                &rel,
                hash,
                root_path,
                api::IndexWriteMode::Overlay,
            )?
        {
            selector_blob::record_selector_blob(
                conn,
                machine_id,
                overlay_project_id,
                &rel,
                hash,
                clean_blob_by_rel.get(&rel).map(String::as_str),
            )?;
            adopted_paths.push(rel.clone());
            outcome.skipped_files += 1;
            continue;
        }
        match action {
            OverlayReconcileAction::Index if ast_by_rel.contains_key(&rel) => {
                match index_file(
                    conn,
                    &abs,
                    target,
                    &excludes,
                    &import_context,
                    semantic_resolver.as_deref_mut(),
                )? {
                    Some(counts) => outcome.add_counts(counts),
                    None => outcome.skipped_files += 1,
                }
            }
            OverlayReconcileAction::Index if content_by_rel.contains_key(&rel) => {
                match index_content_only(conn, &abs, target, &excludes)? {
                    Some(counts) => outcome.add_counts(counts),
                    None => outcome.skipped_files += 1,
                }
            }
            OverlayReconcileAction::Inherit => {
                api::file_state::delete_file_state(
                    conn,
                    machine_id,
                    overlay_project_id,
                    &rel,
                    root_path,
                    api::IndexWriteMode::Overlay,
                )?;
                outcome.skipped_files += 1;
            }
            OverlayReconcileAction::Tombstone => {
                write_tombstone(conn, machine_id, overlay_project_id, root_path, &rel)?;
                outcome.tombstones_indexed += 1;
            }
            OverlayReconcileAction::DeleteOverlay => {
                api::file_state::delete_file_state(
                    conn,
                    machine_id,
                    overlay_project_id,
                    &rel,
                    root_path,
                    api::IndexWriteMode::Overlay,
                )?;
            }
            OverlayReconcileAction::Index => {
                anyhow::bail!("overlay index action selected for non-indexable path `{rel}`")
            }
            OverlayReconcileAction::Skip => {
                let clean_blob = clean_blob_by_rel.get(&rel).map(String::as_str);
                if let (Some(hash), Some(state)) = (current_hash, overlay)
                    && (state.content_hash != hash || state.git_blob_id.as_deref() != clean_blob)
                {
                    selector_blob::record_selector_blob(
                        conn,
                        machine_id,
                        overlay_project_id,
                        &rel,
                        hash,
                        clean_blob,
                    )?;
                }
                outcome.skipped_files += 1;
            }
        }
    }
    // Resolve cross-file local-import calls against the overlay's own symbols.
    // Calls into inherited (not re-indexed) files miss and degrade to unresolved,
    // matching pre-resolution behavior; no project-wide file scan is performed.
    timings.phase("overlay.local_imports");
    resolve_local_import_calls(
        conn,
        overlay_project_id,
        machine_id,
        &outcome.indexed_file_paths,
    )?;
    let mut trigger_paths = outcome.indexed_file_paths.clone();
    trigger_paths.extend(adopted_paths);
    let promoted_owners =
        resolve_local_import_inheritance(conn, overlay_project_id, machine_id, &trigger_paths)?;
    outcome.record_promotion_owners(promoted_owners);
    outcome.durations.indexing_ms = indexing_start.elapsed().as_millis() as u64;

    timings.phase("overlay.stats");
    let stats_start = Instant::now();
    refresh_project_stats(
        conn,
        machine_id,
        target,
        start.elapsed().as_millis() as u64,
        // Candidate-only runs never walk the tree, so they know no eligible total.
        (!candidate_only).then_some(ast_by_rel.len() + content_by_rel.len()),
        None,
        // A path-scoped run reconciles only its filter. Advancing the watermark
        // would hide unreconciled changes from the freshness pre-gate.
        request.explicit_files.is_empty() && request.path_filter.is_none(),
    )?;
    outcome.durations.stats_ms = stats_start.elapsed().as_millis() as u64;
    outcome.durations.total_ms = start.elapsed().as_millis() as u64;

    timings.phase("overlay.communities");
    refresh_communities(conn, ctx, &mut outcome);
    timings.phase("overlay.projection_attachment");
    attach_projection_sync(&mut outcome, request);
    Ok(outcome)
}

/// Projects the parent's freshly parsed files under the parent context, with the
/// same bounded sync the overlay's own files get. Each degraded target becomes
/// a degradation on the overlay outcome, so a failed parent projection is visible.
fn sync_parent_projections(
    parent_ctx: &Context,
    parent_outcome: &IndexOutcome,
) -> Vec<IndexDegradation> {
    let reports = sync::sync_after_index_bounded(
        parent_ctx,
        &parent_outcome.graph_file_paths,
        &parent_outcome.vector_file_paths,
        sync::DEFAULT_PROJECTION_SYNC_STALL_TIMEOUT,
        &mut SilentProjectionProgress,
    );
    [
        (ProjectionTarget::Graph, reports.graph),
        (ProjectionTarget::Vectors, reports.vector),
    ]
    .into_iter()
    .filter(|(_, report)| report.degraded)
    .map(
        |(target, report)| IndexDegradation::ParentProjectionSyncFailed {
            parent_project_id: parent_ctx.project_id.clone(),
            target,
            message: report.error.map_or_else(
                || "projection sync degraded".to_string(),
                |error| format!("{}: {}", error.kind, error.message),
            ),
        },
    )
    .collect()
}

/// The overlay's divergence from the base commit its pins were taken at, or
/// `None` when complete discovery is required: explicit files are judged
/// directly, `--full` walks everything, and a divergence that cannot be named
/// (no pin provenance, or a Git failure) falls back to the same walk.
fn overlay_divergence(request: &IndexRequest, root_path: &Path) -> Option<HashSet<String>> {
    if !request.explicit_files.is_empty() || request.full {
        return None;
    }
    let base_commit = read_isolation_marker(root_path)?.pin?.base_commit;
    overlay_divergent_relative_paths(root_path, &base_commit).ok()
}

/// Paths to reconcile. Divergence plus owned rows when Git can name the
/// divergence; otherwise every source the discovery walk and both trees hold.
fn overlay_reconcile_paths(
    request: &IndexRequest,
    root_path: &Path,
    divergence: Option<&HashSet<String>>,
    ast_by_rel: &HashMap<String, PathBuf>,
    content_by_rel: &HashMap<String, PathBuf>,
    parent_files: &HashMap<String, IndexedFileState>,
    overlay_files: &HashMap<String, IndexedFileState>,
) -> Vec<String> {
    let mut rels: HashSet<String> = HashSet::new();
    if !request.explicit_files.is_empty() {
        for file in &request.explicit_files {
            rels.insert(requested_relative_path(root_path, file));
        }
    } else if let Some(divergence) = divergence {
        rels.extend(divergence.iter().cloned());
        rels.extend(overlay_files.keys().cloned());
    } else {
        rels.extend(ast_by_rel.keys().cloned());
        rels.extend(content_by_rel.keys().cloned());
        rels.extend(parent_files.keys().cloned());
        rels.extend(overlay_files.keys().cloned());
    }

    let mut rels: Vec<_> = rels.into_iter().collect();
    rels.sort();
    rels
}

/// Routes each reconcile path through the same explicit-file rules the
/// explicit branch uses, so a candidate is classified the way discovery would.
fn route_candidates(
    root_path: &Path,
    rels: &[String],
    excludes: &[&str],
    respect_gitignore: bool,
) -> (HashMap<String, PathBuf>, HashMap<String, PathBuf>) {
    let mut ast_by_rel = HashMap::new();
    let mut content_by_rel = HashMap::new();
    for rel in rels {
        let abs = root_path.join(rel);
        let route = explicit_route_with_discovery_options(
            root_path,
            &abs,
            excludes,
            walker::DiscoveryOptions { respect_gitignore },
        );
        match route {
            ExplicitFileRoute::Ast => {
                ast_by_rel.insert(rel.clone(), abs);
            }
            ExplicitFileRoute::ContentOnly => {
                content_by_rel.insert(rel.clone(), abs);
            }
            ExplicitFileRoute::Skip => {}
        }
    }
    (ast_by_rel, content_by_rel)
}

#[cfg(test)]
pub(super) fn overlay_reconcile_candidates(
    request: &IndexRequest,
    root_path: &Path,
    ast_by_rel: &HashMap<String, PathBuf>,
    content_by_rel: &HashMap<String, PathBuf>,
    parent_files: &HashMap<String, IndexedFileState>,
    overlay_files: &HashMap<String, IndexedFileState>,
) -> Vec<String> {
    let divergence = overlay_divergence(request, root_path);
    overlay_reconcile_paths(
        request,
        root_path,
        divergence.as_ref(),
        ast_by_rel,
        content_by_rel,
        parent_files,
        overlay_files,
    )
}

fn paths_by_relative(root_path: &Path, paths: &[PathBuf]) -> HashMap<String, PathBuf> {
    paths
        .iter()
        .filter_map(|path| {
            relative_path(path, root_path)
                .ok()
                .map(|rel| (rel, path.clone()))
        })
        .collect()
}

pub(super) fn explicit_overlay_file_maps(
    request: &IndexRequest,
    root_path: &Path,
    excludes: &[&str],
    respect_gitignore: bool,
) -> (HashMap<String, PathBuf>, HashMap<String, PathBuf>) {
    let mut ast_by_rel = HashMap::new();
    let mut content_by_rel = HashMap::new();
    for file in &request.explicit_files {
        let abs = if file.is_absolute() {
            file.clone()
        } else {
            root_path.join(file)
        };
        let rel = requested_relative_path(root_path, file);
        let route = explicit_route_with_discovery_options(
            root_path,
            &abs,
            excludes,
            walker::DiscoveryOptions { respect_gitignore },
        );
        match route {
            ExplicitFileRoute::Ast => {
                ast_by_rel.insert(rel, abs);
            }
            ExplicitFileRoute::ContentOnly => {
                content_by_rel.insert(rel, abs);
            }
            ExplicitFileRoute::Skip => {}
        }
    }
    (ast_by_rel, content_by_rel)
}

fn indexed_file_states(
    conn: &mut Client,
    machine_id: &str,
    project_id: &str,
    file_paths: Option<&[String]>,
) -> anyhow::Result<HashMap<String, IndexedFileState>> {
    let machine_id = crate::db::id_param(machine_id)?;
    let project_id = crate::db::id_param(project_id)?;
    let rows = if let Some(file_paths) = file_paths {
        conn.query(
            "SELECT s.file_path, s.content_hash, s.git_blob_id, f.language
             FROM code_indexed_file_states s
             JOIN code_indexed_files f
               ON f.project_id = s.project_id
              AND f.file_path = s.file_path
              AND f.content_hash = s.content_hash
             WHERE s.machine_id = $1
               AND s.project_id = $2
               AND s.file_path = ANY($3)",
            &[&machine_id, &project_id, &file_paths],
        )?
    } else {
        conn.query(
            "SELECT s.file_path, s.content_hash, s.git_blob_id, f.language
             FROM code_indexed_file_states s
             JOIN code_indexed_files f
               ON f.project_id = s.project_id
              AND f.file_path = s.file_path
              AND f.content_hash = s.content_hash
             WHERE s.machine_id = $1 AND s.project_id = $2",
            &[&machine_id, &project_id],
        )?
    };
    file_states_from_rows(&rows)
}

/// The base states the overlay's pins serve: each pinned path at the parent
/// content the pin names. Reconciliation compares against these, never against
/// the parent's moving rows.
fn pinned_base_states(
    conn: &mut Client,
    machine_id: &str,
    overlay_project_id: &str,
    file_paths: Option<&[String]>,
) -> anyhow::Result<HashMap<String, IndexedFileState>> {
    let machine_id = crate::db::id_param(machine_id)?;
    let overlay_project_id = crate::db::id_param(overlay_project_id)?;
    let rows = if let Some(file_paths) = file_paths {
        conn.query(
            "SELECT p.file_path, p.content_hash, p.git_blob_id, f.language
             FROM code_overlay_pins p
             JOIN code_indexed_files f
               ON f.project_id = p.source_project_id
              AND f.file_path = p.file_path
              AND f.content_hash = p.content_hash
             WHERE p.machine_id = $1
               AND p.overlay_project_id = $2
               AND p.file_path = ANY($3)",
            &[&machine_id, &overlay_project_id, &file_paths],
        )?
    } else {
        conn.query(
            "SELECT p.file_path, p.content_hash, p.git_blob_id, f.language
             FROM code_overlay_pins p
             JOIN code_indexed_files f
               ON f.project_id = p.source_project_id
              AND f.file_path = p.file_path
              AND f.content_hash = p.content_hash
             WHERE p.machine_id = $1 AND p.overlay_project_id = $2",
            &[&machine_id, &overlay_project_id],
        )?
    };
    file_states_from_rows(&rows)
}

fn file_states_from_rows(
    rows: &[postgres::Row],
) -> anyhow::Result<HashMap<String, IndexedFileState>> {
    let mut files = HashMap::new();
    for row in rows {
        files.insert(
            row.try_get("file_path")?,
            IndexedFileState {
                content_hash: row.try_get("content_hash")?,
                language: row.try_get("language")?,
                git_blob_id: row.try_get("git_blob_id")?,
            },
        );
    }
    Ok(files)
}

/// Paths the overlay changed since the base commit its pins were taken at: its
/// working-tree changes plus every path its HEAD touches since that base. A
/// path the parent changed and the overlay merged still counts, because the
/// overlay's HEAD changed it relative to the pinned content.
fn overlay_divergent_relative_paths(
    root_path: &Path,
    base_commit: &str,
) -> anyhow::Result<HashSet<String>> {
    let mut paths = git_status_relative_paths(root_path)?;
    paths.extend(git_diff_relative_paths(root_path, base_commit)?);
    Ok(paths)
}

fn git_diff_relative_paths(root_path: &Path, base: &str) -> anyhow::Result<HashSet<String>> {
    let stdout = run_git_with_timeout(
        root_path,
        &["diff", "--name-only", "-z", "--no-renames", base, "HEAD"],
    )?;
    Ok(stdout
        .split(|b| *b == 0)
        .filter(|entry| !entry.is_empty())
        .map(|entry| String::from_utf8_lossy(entry).into_owned())
        .collect())
}

fn git_status_relative_paths(root_path: &Path) -> anyhow::Result<HashSet<String>> {
    let stdout = run_git_with_timeout(
        root_path,
        &[
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--no-renames",
        ],
    )?;

    let mut paths = HashSet::new();
    // Porcelain v1 `-z` entries are NUL-delimited records with two status
    // bytes, a separating space, then the changed path.
    for entry in stdout.split(|b| *b == 0) {
        if !is_porcelain_status_entry(entry) {
            continue;
        }
        let path = String::from_utf8_lossy(&entry[3..]).into_owned();
        if !path.is_empty() {
            paths.insert(path);
        }
    }
    Ok(paths)
}

/// Run `git -C root_path <args>` under the overlay git timeout, returning its
/// stdout. Kills the child on timeout and reports git's stderr on failure.
fn run_git_with_timeout(root_path: &Path, args: &[&str]) -> anyhow::Result<Vec<u8>> {
    let verb = args.first().copied().unwrap_or("git");
    let mut child = Command::new("git")
        .arg("-C")
        .arg(root_path)
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .with_context(|| format!("spawn git {verb}"))?;
    let timeout = git_status_timeout();
    let output = match child.wait_timeout(timeout)? {
        Some(_) => child.wait_with_output()?,
        None => {
            let _ = child.kill();
            let output = child.wait_with_output()?;
            let stderr = compact_stderr(&output.stderr);
            if stderr.is_empty() {
                anyhow::bail!("git {verb} timed out after {}ms", timeout.as_millis());
            }
            anyhow::bail!(
                "git {verb} timed out after {}ms: {stderr}",
                timeout.as_millis()
            );
        }
    };
    if !output.status.success() {
        let stderr = compact_stderr(&output.stderr);
        if stderr.is_empty() {
            anyhow::bail!("git {verb} failed");
        }
        anyhow::bail!("git {verb} failed: {stderr}");
    }
    Ok(output.stdout)
}

fn git_status_timeout() -> Duration {
    // Large repos can make `git status --porcelain -z` slow; set
    // GCODE_GIT_STATUS_TIMEOUT_SECS to raise this timeout for overlay indexing.
    let Some(raw) = std::env::var_os(GIT_STATUS_TIMEOUT_ENV) else {
        return Duration::from_secs(DEFAULT_GIT_STATUS_TIMEOUT_SECS);
    };
    let raw = raw.to_string_lossy();
    match raw.trim().parse::<u64>() {
        Ok(seconds) if seconds > 0 => Duration::from_secs(seconds),
        _ => {
            eprintln!(
                "Warning: invalid {GIT_STATUS_TIMEOUT_ENV}={raw:?}; using default {DEFAULT_GIT_STATUS_TIMEOUT_SECS}s"
            );
            Duration::from_secs(DEFAULT_GIT_STATUS_TIMEOUT_SECS)
        }
    }
}

fn compact_stderr(stderr: &[u8]) -> String {
    String::from_utf8_lossy(stderr)
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn is_porcelain_status_entry(entry: &[u8]) -> bool {
    entry.len() >= 4
        && valid_porcelain_status_byte(entry[0])
        && valid_porcelain_status_byte(entry[1])
        && entry[2] == b' '
}

fn valid_porcelain_status_byte(byte: u8) -> bool {
    matches!(
        byte,
        b' ' | b'M' | b'A' | b'D' | b'R' | b'C' | b'U' | b'?' | b'!'
    )
}

fn rel_matches_filter(root_path: &Path, path_filter: &Path, rel: &str) -> bool {
    let filter_abs = if path_filter.is_absolute() {
        path_filter.to_path_buf()
    } else {
        root_path.join(path_filter)
    };
    // Deleted and newly-created overlay files may not canonicalize. The lexical
    // absolute fallback keeps filters usable during that drift, with the known
    // tradeoff that symlink resolution is unavailable until paths exist again.
    let filter_abs = filter_abs.canonicalize().unwrap_or(filter_abs);
    let abs = root_path.join(rel);
    let abs = abs.canonicalize().unwrap_or(abs);
    abs.starts_with(filter_abs)
}

pub(super) fn write_tombstone(
    conn: &mut Client,
    machine_id: &str,
    project_id: &str,
    root_path: &Path,
    rel: &str,
) -> anyhow::Result<()> {
    let mut tx = conn.transaction().context("start tombstone transaction")?;
    let mut sink = PostgresCodeFactSink::new(
        &mut tx,
        project_id,
        root_path,
        api::IndexWriteMode::Overlay,
        machine_id,
    )?;
    sink.upsert_file(&IndexedFile {
        id: IndexedFile::make_id(project_id, rel, visibility::TOMBSTONE_HASH),
        project_id: project_id.to_string(),
        file_path: rel.to_string(),
        language: visibility::TOMBSTONE_LANGUAGE.to_string(),
        content_hash: visibility::TOMBSTONE_HASH.to_string(),
        symbol_count: 0,
        byte_size: 0,
        indexed_at: epoch_secs_str(),
    })?;
    tx.commit().context("commit tombstone transaction")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{DEFAULT_GIT_STATUS_TIMEOUT_SECS, git_status_timeout, valid_porcelain_status_byte};
    use std::time::Duration;

    #[test]
    fn porcelain_status_byte_validation_matches_git_v1_codes() {
        for byte in *b" MADRCU?!" {
            assert!(valid_porcelain_status_byte(byte));
        }
        for byte in [0, b'X', b'\n'] {
            assert!(!valid_porcelain_status_byte(byte));
        }
    }

    #[test]
    #[serial_test::serial]
    fn git_status_timeout_reads_positive_env_seconds() {
        temp_env::with_var("GCODE_GIT_STATUS_TIMEOUT_SECS", Some("2"), || {
            assert_eq!(git_status_timeout(), Duration::from_secs(2));
        });
    }

    #[test]
    #[serial_test::serial]
    fn git_status_timeout_rejects_invalid_env_values() {
        for value in ["0", "-1", "abc"] {
            temp_env::with_var("GCODE_GIT_STATUS_TIMEOUT_SECS", Some(value), || {
                assert_eq!(
                    git_status_timeout(),
                    Duration::from_secs(DEFAULT_GIT_STATUS_TIMEOUT_SECS)
                );
            });
        }
    }
}
