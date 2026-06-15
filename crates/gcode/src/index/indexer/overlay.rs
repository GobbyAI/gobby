use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use anyhow::Context as _;
use postgres::Client;
use wait_timeout::ChildExt;

use crate::config::{Context, ProjectIndexScope};
use crate::db;
use crate::index::api;
use crate::index::{hasher, parser, walker};
use crate::models::IndexedFile;
use crate::visibility;

use super::file::{create_semantic_resolver_if_needed, index_content_only, index_file};
use super::lifecycle::{
    attach_projection_sync, cleanup_deleted_file_projections, refresh_project_stats,
};
use super::local_imports::resolve_local_import_calls;
use super::sink::{CodeFactSink, PostgresCodeFactSink};
use super::types::{IndexOutcome, IndexRequest, OverlayIndexMetadata};
use super::util::{
    DEFAULT_EXCLUDES, epoch_secs_str, relative_path, requested_relative_path,
    unsupported_file_types,
};

const GIT_STATUS_TIMEOUT_ENV: &str = "GCODE_GIT_STATUS_TIMEOUT_SECS";
const DEFAULT_GIT_STATUS_TIMEOUT_SECS: u64 = 5;

#[derive(Debug, Clone)]
pub(super) struct IndexedFileState {
    pub(super) content_hash: String,
    pub(super) language: String,
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
) -> anyhow::Result<IndexOutcome> {
    let ProjectIndexScope::Overlay {
        overlay_project_id,
        overlay_root,
        parent_project_id,
        parent_root,
    } = &ctx.index_scope
    else {
        anyhow::bail!("overlay indexer requires an overlay project context");
    };

    crate::config::validate_parent_code_index(conn, &ctx.index_scope)?;

    let start = Instant::now();
    let discovery_start = Instant::now();
    let root_path = &request.project_root;
    let mut outcome = IndexOutcome::new(overlay_project_id);
    outcome.overlay = Some(OverlayIndexMetadata {
        overlay_project_id: overlay_project_id.clone(),
        overlay_root: overlay_root.to_string_lossy().to_string(),
        parent_project_id: parent_project_id.clone(),
        parent_root: parent_root.to_string_lossy().to_string(),
    });

    let (candidates, content_only) = walker::discover_files_with_options(
        root_path,
        DEFAULT_EXCLUDES,
        walker::DiscoveryOptions {
            respect_gitignore: ctx.indexing.respect_gitignore,
        },
    );
    let ast_by_rel = paths_by_relative(root_path, &candidates);
    let content_by_rel = paths_by_relative(root_path, &content_only);
    let import_context = parser::build_import_resolution_context(root_path, &candidates);

    let parent_files = indexed_file_states(conn, parent_project_id)?;
    let overlay_files = indexed_file_states(conn, overlay_project_id)?;
    let mut rels = overlay_reconcile_candidates(
        request,
        root_path,
        &ast_by_rel,
        &content_by_rel,
        &parent_files,
        &overlay_files,
    );
    if let Some(filter) = request.path_filter.as_deref() {
        rels.retain(|rel| rel_matches_filter(root_path, filter, rel));
    }
    let unsupported_paths = rels
        .iter()
        .filter_map(|rel| content_by_rel.get(rel).cloned())
        .collect::<Vec<_>>();
    outcome.set_unsupported_file_types(unsupported_file_types(root_path, &unsupported_paths));

    outcome.scanned_files = rels.len();
    outcome.durations.discovery_ms = discovery_start.elapsed().as_millis() as u64;

    let mut hash_by_rel = HashMap::new();
    for rel in &rels {
        let abs = root_path.join(rel);
        if abs.exists()
            && let Ok(hash) = hasher::file_content_hash(&abs)
        {
            hash_by_rel.insert(rel.clone(), hash);
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

    let indexing_start = Instant::now();
    for rel in rels {
        let abs = root_path.join(&rel);
        let parent = parent_files.get(&rel);
        let overlay = overlay_files.get(&rel);
        let current_hash = hash_by_rel.get(&rel).map(String::as_str);
        let indexable = ast_by_rel.contains_key(&rel) || content_by_rel.contains_key(&rel);
        let action =
            overlay_reconcile_action(abs.exists(), current_hash, parent, overlay, indexable);
        let file_vectors_synced = if matches!(
            action,
            OverlayReconcileAction::Inherit
                | OverlayReconcileAction::Tombstone
                | OverlayReconcileAction::DeleteOverlay
        ) {
            db::file_vectors_synced(conn, overlay_project_id, &rel)?
        } else {
            None
        };

        match action {
            OverlayReconcileAction::Index if ast_by_rel.contains_key(&rel) => {
                match index_file(
                    conn,
                    &abs,
                    overlay_project_id,
                    root_path,
                    DEFAULT_EXCLUDES,
                    &import_context,
                    semantic_resolver.as_deref_mut(),
                )? {
                    Some(counts) => outcome.add_counts(counts),
                    None => outcome.skipped_files += 1,
                }
            }
            OverlayReconcileAction::Index if content_by_rel.contains_key(&rel) => {
                match index_content_only(
                    conn,
                    &abs,
                    overlay_project_id,
                    root_path,
                    DEFAULT_EXCLUDES,
                )? {
                    Some(counts) => outcome.add_counts(counts),
                    None => outcome.skipped_files += 1,
                }
            }
            OverlayReconcileAction::Inherit => {
                cleanup_deleted_file_projections(ctx, &rel, &mut outcome, file_vectors_synced);
                api::delete_file_facts(conn, overlay_project_id, &rel)?;
                outcome.skipped_files += 1;
            }
            OverlayReconcileAction::Tombstone => {
                cleanup_deleted_file_projections(ctx, &rel, &mut outcome, file_vectors_synced);
                write_tombstone(conn, overlay_project_id, &rel)?;
                outcome.tombstones_indexed += 1;
            }
            OverlayReconcileAction::DeleteOverlay => {
                cleanup_deleted_file_projections(ctx, &rel, &mut outcome, file_vectors_synced);
                api::delete_file_facts(conn, overlay_project_id, &rel)?;
            }
            OverlayReconcileAction::Index => {
                anyhow::bail!("overlay index action selected for non-indexable path `{rel}`")
            }
            OverlayReconcileAction::Skip => {
                outcome.skipped_files += 1;
            }
        }
    }
    // Resolve cross-file local-import calls against the overlay's own symbols.
    // Calls into inherited (not re-indexed) files miss and degrade to unresolved,
    // matching pre-resolution behavior; no project-wide file scan is performed.
    resolve_local_import_calls(conn, overlay_project_id, &outcome.indexed_file_paths)?;
    outcome.durations.indexing_ms = indexing_start.elapsed().as_millis() as u64;

    let stats_start = Instant::now();
    refresh_project_stats(
        conn,
        root_path,
        overlay_project_id,
        start.elapsed().as_millis() as u64,
        Some(ast_by_rel.len() + content_by_rel.len()),
    );
    outcome.durations.stats_ms = stats_start.elapsed().as_millis() as u64;
    outcome.durations.total_ms = start.elapsed().as_millis() as u64;

    attach_projection_sync(&mut outcome, request);
    Ok(outcome)
}

fn overlay_reconcile_candidates(
    request: &IndexRequest,
    root_path: &Path,
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
    } else if request.full {
        rels.extend(ast_by_rel.keys().cloned());
        rels.extend(content_by_rel.keys().cloned());
        rels.extend(parent_files.keys().cloned());
        rels.extend(overlay_files.keys().cloned());
    } else if let Ok(status_paths) = git_status_relative_paths(root_path) {
        rels.extend(status_paths);
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

fn indexed_file_states(
    conn: &mut Client,
    project_id: &str,
) -> anyhow::Result<HashMap<String, IndexedFileState>> {
    let mut files = HashMap::new();
    for row in conn.query(
        "SELECT file_path, content_hash, language
         FROM code_indexed_files
         WHERE project_id = $1",
        &[&project_id],
    )? {
        files.insert(
            row.try_get("file_path")?,
            IndexedFileState {
                content_hash: row.try_get("content_hash")?,
                language: row.try_get("language")?,
            },
        );
    }
    Ok(files)
}

fn git_status_relative_paths(root_path: &Path) -> anyhow::Result<HashSet<String>> {
    let mut child = Command::new("git")
        .arg("-C")
        .arg(root_path)
        .args([
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--no-renames",
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("spawn git status")?;
    let timeout = git_status_timeout();
    let output = match child.wait_timeout(timeout)? {
        Some(_) => child.wait_with_output()?,
        None => {
            let _ = child.kill();
            let output = child.wait_with_output()?;
            let stderr = compact_stderr(&output.stderr);
            if stderr.is_empty() {
                anyhow::bail!("git status timed out after {}ms", timeout.as_millis());
            }
            anyhow::bail!(
                "git status timed out after {}ms: {stderr}",
                timeout.as_millis()
            );
        }
    };
    if !output.status.success() {
        let stderr = compact_stderr(&output.stderr);
        if stderr.is_empty() {
            anyhow::bail!("git status failed");
        }
        anyhow::bail!("git status failed: {stderr}");
    }

    let mut paths = HashSet::new();
    // Porcelain v1 `-z` entries are NUL-delimited records with two status
    // bytes, a separating space, then the changed path.
    for entry in output.stdout.split(|b| *b == 0) {
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

fn write_tombstone(conn: &mut Client, project_id: &str, rel: &str) -> anyhow::Result<()> {
    let mut tx = conn.transaction().context("start tombstone transaction")?;
    let mut sink = PostgresCodeFactSink::new(&mut tx);
    sink.delete_file_facts(project_id, rel)?;
    sink.upsert_file(&IndexedFile {
        id: IndexedFile::make_id(project_id, rel),
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
        for byte in [b' ', b'M', b'A', b'D', b'R', b'C', b'U', b'?', b'!'] {
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
