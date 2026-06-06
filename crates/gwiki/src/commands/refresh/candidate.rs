use std::fs;
use std::path::{Path, PathBuf};

use crate::commands::index;
use crate::ingest;
use crate::ingest::url::{UrlIngestFailure, UrlSnapshot};
use crate::scope::ResolvedScope;
use crate::sources::{SourceManifest, SourceRecord};
use crate::{ScopeIdentity, WikiError};

use super::model::{ChangedRefresh, RefreshFailure, RefreshResult, RefreshSinks, RefreshedSource};
use super::selection::{SelectionFailure, local_file_replay, selection_failure};
use super::vault::{raw_source_path, remove_relative_file, source_asset_paths_for_id};

pub(crate) fn refresh_url_candidate(
    vault_root: &Path,
    record: &SourceRecord,
    fetch: &mut impl FnMut(&SourceRecord, &str) -> Result<UrlSnapshot, UrlIngestFailure>,
    fetched_at: &str,
    refreshed: &mut Vec<RefreshedSource>,
    unchanged: &mut Vec<RefreshResult>,
    failed: &mut Vec<RefreshFailure>,
) -> Result<(), WikiError> {
    match fetch(record, fetched_at) {
        Ok(snapshot) => {
            let source_hash = gobby_core::indexing::content_hash(&snapshot.body);
            let raw_path = raw_source_path(&record.id)?;
            if source_hash == record.content_hash {
                unchanged.push(RefreshResult {
                    id: record.id.clone(),
                    location: record.location.clone(),
                    source_kind: record.kind.clone(),
                    replay_kind: "url",
                    raw_path,
                    content_hash: record.content_hash.clone(),
                    changed: false,
                });
                return Ok(());
            }

            let final_url = snapshot.final_url.clone();
            match refresh_changed_url_source(vault_root, record, snapshot) {
                Ok(change) => refreshed.push(RefreshedSource {
                    old_id: record.id.clone(),
                    new_id: change.result.record.id.clone(),
                    location: record.location.clone(),
                    source_kind: record.kind.clone(),
                    replay_kind: "url",
                    final_url: Some(final_url),
                    raw_path: change.result.raw_path,
                    previous_raw_path: change.previous_raw_path,
                    removed_paths: change.removed_paths,
                    changed: true,
                    source: change.result.record,
                }),
                Err(error) => failed.push(RefreshFailure {
                    id: record.id.clone(),
                    location: Some(record.location.clone()),
                    source_kind: Some(record.kind.clone()),
                    code: error.code().to_string(),
                    message: error.to_string(),
                }),
            }
        }
        Err(error) => failed.push(RefreshFailure {
            id: record.id.clone(),
            location: Some(record.location.clone()),
            source_kind: Some(record.kind.clone()),
            code: error.code,
            message: error.message,
        }),
    }
    Ok(())
}

pub(crate) fn refresh_local_candidate(
    scope: &ResolvedScope,
    output_scope: &ScopeIdentity,
    record: &SourceRecord,
    fetched_at: &str,
    sinks: &mut RefreshSinks<'_>,
) -> Result<(), WikiError> {
    let Some((path, replay_options)) = local_file_replay(record) else {
        sinks.failed.push(selection_failure(
            record,
            SelectionFailure::MissingReplayMetadata,
        ));
        return Ok(());
    };
    let source_hash = match local_file_hash(record, path) {
        Ok(hash) => hash,
        Err(failure) => {
            sinks.failed.push(failure);
            return Ok(());
        }
    };
    let raw_path = raw_source_path(&record.id)?;
    if source_hash == record.content_hash {
        sinks.unchanged.push(RefreshResult {
            id: record.id.clone(),
            location: record.location.clone(),
            source_kind: record.kind.clone(),
            replay_kind: "local_file",
            raw_path,
            content_hash: record.content_hash.clone(),
            changed: false,
        });
        return Ok(());
    }

    let options = match replay_options.to_ingest_file_options() {
        Ok(options) => options,
        Err(error) => {
            sinks.failed.push(RefreshFailure {
                id: record.id.clone(),
                location: Some(record.location.clone()),
                source_kind: Some(record.kind.clone()),
                code: error.code().to_string(),
                message: error.to_string(),
            });
            return Ok(());
        }
    };
    let (ai_context, options) =
        match index::resolve_ingest_file_ai_context(output_scope, &options, "gwiki refresh") {
            Ok(resolved) => resolved,
            Err(error) => {
                sinks.failed.push(RefreshFailure {
                    id: record.id.clone(),
                    location: Some(record.location.clone()),
                    source_kind: Some(record.kind.clone()),
                    code: error.code().to_string(),
                    message: error.to_string(),
                });
                return Ok(());
            }
        };

    match refresh_changed_local_source(
        scope.root(),
        output_scope,
        record,
        path,
        &ai_context,
        &options,
        fetched_at,
    ) {
        Ok(change) => {
            sinks.degradations.extend(change.degradations);
            sinks.refreshed.push(RefreshedSource {
                old_id: record.id.clone(),
                new_id: change.result.record.id.clone(),
                location: record.location.clone(),
                source_kind: record.kind.clone(),
                replay_kind: "local_file",
                final_url: None,
                raw_path: change.result.raw_path,
                previous_raw_path: change.previous_raw_path,
                removed_paths: change.removed_paths,
                changed: true,
                source: change.result.record,
            });
        }
        Err(error) => sinks.failed.push(RefreshFailure {
            id: record.id.clone(),
            location: Some(record.location.clone()),
            source_kind: Some(record.kind.clone()),
            code: error.code().to_string(),
            message: error.to_string(),
        }),
    }
    Ok(())
}

fn local_file_hash(record: &SourceRecord, path: &Path) -> Result<String, RefreshFailure> {
    match fs::metadata(path) {
        Ok(metadata) if metadata.is_file() => {}
        Ok(_) => {
            return Err(local_file_failure(
                record,
                "invalid_local_file",
                format!("local replay path `{}` is not a file", path.display()),
            ));
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Err(local_file_failure(
                record,
                "missing_local_file",
                format!("local replay path `{}` was not found", path.display()),
            ));
        }
        Err(error) => {
            return Err(local_file_failure(
                record,
                "unreadable_local_file",
                format!(
                    "failed to stat local replay path `{}`: {error}",
                    path.display()
                ),
            ));
        }
    }

    gobby_core::indexing::file_content_hash(path).map_err(|error| {
        local_file_failure(
            record,
            "unreadable_local_file",
            format!(
                "failed to hash local replay path `{}`: {error}",
                path.display()
            ),
        )
    })
}

fn local_file_failure(record: &SourceRecord, code: &str, message: String) -> RefreshFailure {
    RefreshFailure {
        id: record.id.clone(),
        location: Some(record.location.clone()),
        source_kind: Some(record.kind.clone()),
        code: code.to_string(),
        message,
    }
}

fn refresh_changed_url_source(
    vault_root: &Path,
    previous: &SourceRecord,
    snapshot: UrlSnapshot,
) -> Result<ChangedRefresh, WikiError> {
    let previous_raw_path = raw_source_path(&previous.id)?;
    let mut previous_paths = vec![previous_raw_path.clone()];
    previous_paths.extend(source_asset_paths_for_id(vault_root, &previous.id)?);

    let result = ingest::url::ingest_snapshot_without_index(vault_root, snapshot)?;

    finalize_changed_refresh(
        vault_root,
        previous,
        previous_raw_path,
        previous_paths,
        result,
        Vec::new(),
    )
}

fn refresh_changed_local_source(
    vault_root: &Path,
    scope: &ScopeIdentity,
    previous: &SourceRecord,
    path: &Path,
    ai_context: &gobby_core::ai_context::AiContext,
    options: &crate::IngestFileOptions,
    fetched_at: &str,
) -> Result<ChangedRefresh, WikiError> {
    let previous_raw_path = raw_source_path(&previous.id)?;
    let mut previous_paths = vec![previous_raw_path.clone()];
    previous_paths.extend(source_asset_paths_for_id(vault_root, &previous.id)?);

    let local_result = ingest::file::ingest_path_without_index(
        vault_root, scope, ai_context, options, path, fetched_at,
    )?;
    let result = local_result.result;

    finalize_changed_refresh(
        vault_root,
        previous,
        previous_raw_path,
        previous_paths,
        result,
        local_result.degradations,
    )
}

fn finalize_changed_refresh(
    vault_root: &Path,
    previous: &SourceRecord,
    previous_raw_path: PathBuf,
    previous_paths: Vec<PathBuf>,
    result: ingest::IngestResult,
    degradations: Vec<String>,
) -> Result<ChangedRefresh, WikiError> {
    let mut removed_paths = Vec::new();
    for previous_path in previous_paths {
        if previous_path == result.raw_path
            || result
                .asset_path
                .as_ref()
                .is_some_and(|asset| *asset == previous_path)
        {
            continue;
        }
        if remove_relative_file(vault_root, &previous_path)? {
            removed_paths.push(previous_path);
        }
    }

    SourceManifest::update(vault_root, |manifest| {
        let before = manifest.entries.len();
        manifest.entries.retain(|entry| entry.id != previous.id);
        Ok(manifest.entries.len() != before)
    })?;

    Ok(ChangedRefresh {
        result,
        previous_raw_path,
        removed_paths,
        degradations,
    })
}
