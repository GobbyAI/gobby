use std::collections::BTreeSet;
use std::fs;
use std::path::{Component, Path, PathBuf};

use serde::Serialize;
use serde_json::json;

use crate::commands::index;
use crate::ingest;
use crate::ingest::url::{UrlIngestFailure, UrlSnapshot};
use crate::scope::ResolvedScope;
use crate::sources::{SourceKind, SourceManifest, SourceRecord, SourceReplay, SourceReplayOptions};
use crate::support::counts::IndexCounts;
use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::support::time::collect_timestamp;
use crate::{CommandOutcome, ScopeIdentity, ScopeSelection, WikiError};

pub(crate) fn execute(
    selection: ScopeSelection,
    source_ids: Vec<String>,
    dry_run: bool,
) -> Result<CommandOutcome, WikiError> {
    execute_with_fetcher(selection, source_ids, dry_run, |record, fetched_at| {
        ingest::url::fetch_url_snapshot(refresh_url(record), fetched_at)
    })
}

fn execute_with_fetcher(
    selection: ScopeSelection,
    source_ids: Vec<String>,
    dry_run: bool,
    mut fetch: impl FnMut(&SourceRecord, &str) -> Result<UrlSnapshot, UrlIngestFailure>,
) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    execute_resolved_with_fetcher(scope, source_ids, dry_run, |record, fetched_at| {
        fetch(record, fetched_at)
    })
}

fn execute_resolved_with_fetcher(
    scope: ResolvedScope,
    source_ids: Vec<String>,
    dry_run: bool,
    mut fetch: impl FnMut(&SourceRecord, &str) -> Result<UrlSnapshot, UrlIngestFailure>,
) -> Result<CommandOutcome, WikiError> {
    ensure_scope_root(scope.root())?;
    let output_scope = resolved_scope_identity(&scope);
    let manifest = SourceManifest::read(scope.root())?;
    let explicit = !source_ids.is_empty();
    let Selection {
        candidates,
        planned,
        skipped,
        mut failed,
    } = select_sources(&manifest.entries, &source_ids);

    if dry_run {
        return Ok(render_refresh(RefreshRender {
            scope: output_scope,
            dry_run,
            planned,
            refreshed: Vec::new(),
            unchanged: Vec::new(),
            failed,
            skipped,
            indexed: None,
            index_status: IndexStatus::not_run(),
            degradations: Vec::new(),
            explicit,
        }));
    }

    let fetched_at = collect_timestamp().map_err(|error| WikiError::Config {
        detail: format!("failed to read system clock: {error}"),
    })?;
    let mut refreshed = Vec::new();
    let mut unchanged = Vec::new();
    let mut degradations = Vec::new();

    for record in &candidates {
        match replay_kind(record) {
            Ok(ReplayKind::Url) => {
                refresh_url_candidate(
                    scope.root(),
                    record,
                    &mut fetch,
                    &fetched_at,
                    &mut refreshed,
                    &mut unchanged,
                    &mut failed,
                )?;
            }
            Ok(ReplayKind::LocalFile) => {
                let mut sinks = RefreshSinks {
                    refreshed: &mut refreshed,
                    unchanged: &mut unchanged,
                    failed: &mut failed,
                    degradations: &mut degradations,
                };
                refresh_local_candidate(&scope, &output_scope, record, &fetched_at, &mut sinks)?;
            }
            Err(error) => {
                failed.push(selection_failure(record, error));
            }
        }
    }

    let (indexed, index_status) = if refreshed.is_empty() {
        (None, IndexStatus::not_run())
    } else {
        match index::index_resolved_scope(&scope) {
            Ok(counts) => {
                let indexed = IndexedCounts::from(counts);
                (Some(indexed.clone()), IndexStatus::indexed(indexed))
            }
            Err(error) => {
                degradations.push(format!("index_failed:{error}"));
                (None, IndexStatus::degraded())
            }
        }
    };

    Ok(render_refresh(RefreshRender {
        scope: output_scope,
        dry_run,
        planned,
        refreshed,
        unchanged,
        failed,
        skipped,
        indexed,
        index_status,
        degradations,
        explicit,
    }))
}

fn refresh_url_candidate(
    vault_root: &Path,
    record: &SourceRecord,
    fetch: &mut impl FnMut(&SourceRecord, &str) -> Result<UrlSnapshot, UrlIngestFailure>,
    fetched_at: &str,
    refreshed: &mut Vec<RefreshedSource>,
    unchanged: &mut Vec<UnchangedRefresh>,
    failed: &mut Vec<RefreshFailure>,
) -> Result<(), WikiError> {
    match fetch(record, fetched_at) {
        Ok(snapshot) => {
            let source_hash = gobby_core::indexing::content_hash(&snapshot.body);
            let raw_path = raw_source_path(&record.id)?;
            if source_hash == record.content_hash {
                unchanged.push(UnchangedRefresh {
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

fn refresh_local_candidate(
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
        sinks.unchanged.push(UnchangedRefresh {
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

    let mut removed_paths = Vec::new();
    for path in previous_paths {
        if path == result.raw_path
            || result
                .asset_path
                .as_ref()
                .is_some_and(|asset| *asset == path)
        {
            continue;
        }
        if remove_relative_file(vault_root, &path)? {
            removed_paths.push(path);
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
        degradations: Vec::new(),
    })
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

    let mut removed_paths = Vec::new();
    for path in previous_paths {
        if path == result.raw_path
            || result
                .asset_path
                .as_ref()
                .is_some_and(|asset| *asset == path)
        {
            continue;
        }
        if remove_relative_file(vault_root, &path)? {
            removed_paths.push(path);
        }
    }

    if previous.id != result.record.id {
        SourceManifest::update(vault_root, |manifest| {
            let before = manifest.entries.len();
            manifest.entries.retain(|entry| entry.id != previous.id);
            Ok(manifest.entries.len() != before)
        })?;
    }

    Ok(ChangedRefresh {
        result,
        previous_raw_path,
        removed_paths,
        degradations: local_result.degradations,
    })
}

fn select_sources(entries: &[SourceRecord], source_ids: &[String]) -> Selection {
    if source_ids.is_empty() {
        let mut planned = Vec::new();
        let mut skipped = Vec::new();
        let mut failed = Vec::new();
        for record in entries {
            match replay_kind(record) {
                Ok(_) => {
                    planned.push(RefreshPlan::from_record(record));
                }
                Err(SelectionFailure::MissingReplayMetadata) => {
                    failed.push(selection_failure(
                        record,
                        SelectionFailure::MissingReplayMetadata,
                    ));
                }
                Err(SelectionFailure::UnsupportedSourceKind) => {
                    skipped.push(SkippedRefresh {
                        id: record.id.clone(),
                        location: record.location.clone(),
                        source_kind: record.kind.clone(),
                        code: "unsupported_source_kind".to_string(),
                        message: format!(
                            "source `{}` has kind `{}` and does not have a refresh replay contract",
                            record.id, record.kind
                        ),
                    });
                }
            }
        }
        return Selection {
            candidates: planned.iter().map(|plan| plan.record.clone()).collect(),
            planned,
            skipped,
            failed,
        };
    }

    let mut seen = BTreeSet::new();
    let mut planned = Vec::new();
    let mut failed = Vec::new();
    for id in source_ids {
        if !seen.insert(id.clone()) {
            continue;
        }
        let Some(record) = entries.iter().find(|entry| entry.id == *id) else {
            failed.push(RefreshFailure {
                id: id.clone(),
                location: None,
                source_kind: None,
                code: "not_found".to_string(),
                message: format!("source `{id}` was not found"),
            });
            continue;
        };
        match replay_kind(record) {
            Ok(_) => planned.push(RefreshPlan::from_record(record)),
            Err(error) => {
                failed.push(selection_failure(record, error));
            }
        }
    }

    Selection {
        candidates: planned.iter().map(|plan| plan.record.clone()).collect(),
        planned,
        skipped: Vec::new(),
        failed,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ReplayKind {
    Url,
    LocalFile,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SelectionFailure {
    MissingReplayMetadata,
    UnsupportedSourceKind,
}

fn replay_kind(record: &SourceRecord) -> Result<ReplayKind, SelectionFailure> {
    if is_url_source(record) {
        return Ok(ReplayKind::Url);
    }
    if local_file_replay(record).is_some() {
        return Ok(ReplayKind::LocalFile);
    }
    if is_local_file_source_kind(&record.kind) {
        Err(SelectionFailure::MissingReplayMetadata)
    } else {
        Err(SelectionFailure::UnsupportedSourceKind)
    }
}

fn replay_kind_name(record: &SourceRecord) -> &'static str {
    match replay_kind(record) {
        Ok(ReplayKind::Url) => "url",
        Ok(ReplayKind::LocalFile) => "local_file",
        Err(_) => "unsupported",
    }
}

fn local_file_replay(record: &SourceRecord) -> Option<(&Path, &SourceReplayOptions)> {
    match record.replay.as_ref()? {
        SourceReplay::LocalFile { path, options } => Some((path.as_path(), options)),
    }
}

fn is_local_file_source_kind(kind: &SourceKind) -> bool {
    matches!(
        kind,
        SourceKind::Audio
            | SourceKind::Image
            | SourceKind::Video
            | SourceKind::Pdf
            | SourceKind::Office
            | SourceKind::Html
            | SourceKind::Markdown
            | SourceKind::Text
            | SourceKind::File
    )
}

fn selection_failure(record: &SourceRecord, error: SelectionFailure) -> RefreshFailure {
    match error {
        SelectionFailure::MissingReplayMetadata => RefreshFailure {
            id: record.id.clone(),
            location: Some(record.location.clone()),
            source_kind: Some(record.kind.clone()),
            code: "missing_replay_metadata".to_string(),
            message: format!(
                "source `{}` has kind `{}` but no local replay metadata",
                record.id, record.kind
            ),
        },
        SelectionFailure::UnsupportedSourceKind => RefreshFailure {
            id: record.id.clone(),
            location: Some(record.location.clone()),
            source_kind: Some(record.kind.clone()),
            code: "unsupported_source_kind".to_string(),
            message: format!(
                "source `{}` has kind `{}` and does not have a refresh replay contract",
                record.id, record.kind
            ),
        },
    }
}

fn is_url_source(record: &SourceRecord) -> bool {
    is_http_url(&record.location) || is_http_url(&record.canonical_location)
}

fn refresh_url(record: &SourceRecord) -> &str {
    if is_http_url(&record.location) {
        &record.location
    } else {
        &record.canonical_location
    }
}

fn is_http_url(value: &str) -> bool {
    let lower = value.trim().to_ascii_lowercase();
    lower.starts_with("http://") || lower.starts_with("https://")
}

fn raw_source_path(id: &str) -> Result<PathBuf, WikiError> {
    if id.trim().is_empty()
        || id.contains('/')
        || id.contains('\\')
        || Path::new(id)
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(WikiError::InvalidInput {
            field: "source_id",
            message: format!("unsafe source id `{id}`"),
        });
    }
    Ok(Path::new("raw").join(format!("{id}.md")))
}

fn source_asset_paths_for_id(vault_root: &Path, id: &str) -> Result<Vec<PathBuf>, WikiError> {
    let asset_dir = vault_root.join("raw/assets");
    if !asset_dir.exists() {
        return Ok(Vec::new());
    }
    let prefix = format!("{id}.");
    let mut paths = Vec::new();
    for entry in fs::read_dir(&asset_dir).map_err(|error| WikiError::Io {
        action: "read raw source assets",
        path: Some(asset_dir.clone()),
        source: error,
    })? {
        let entry = entry.map_err(|error| WikiError::Io {
            action: "read raw source asset entry",
            path: Some(asset_dir.clone()),
            source: error,
        })?;
        let file_name = entry.file_name();
        if file_name
            .to_str()
            .is_some_and(|name| name.starts_with(&prefix))
        {
            paths.push(Path::new("raw/assets").join(file_name));
        }
    }
    Ok(paths)
}

fn remove_relative_file(vault_root: &Path, relative_path: &Path) -> Result<bool, WikiError> {
    let full_path = vault_root.join(relative_path);
    match fs::remove_file(&full_path) {
        Ok(()) => Ok(true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(WikiError::Io {
            action: "remove superseded raw source path",
            path: Some(full_path),
            source: error,
        }),
    }
}

fn ensure_scope_root(root: &Path) -> Result<(), WikiError> {
    if root.is_dir() {
        Ok(())
    } else {
        Err(WikiError::NotFound {
            resource: "wiki scope",
            id: root.display().to_string(),
        })
    }
}

fn render_refresh(render: RefreshRender) -> CommandOutcome {
    let status = refresh_status(
        render.dry_run,
        render.refreshed.len(),
        render.unchanged.len(),
        render.failed.len(),
    );
    let exit_code = if render.explicit
        && !render.dry_run
        && !render.failed.is_empty()
        && render.refreshed.is_empty()
        && render.unchanged.is_empty()
    {
        1
    } else {
        0
    };
    let payload = json!({
        "command": "refresh",
        "scope": render.scope,
        "status": status,
        "dry_run": render.dry_run,
        "planned": render.planned,
        "refreshed": render.refreshed,
        "unchanged": render.unchanged,
        "failed": render.failed,
        "skipped": render.skipped,
        "indexed": render.indexed,
        "index_status": render.index_status,
        "degradations": render.degradations,
    });
    let text = format!(
        "Refresh sources\nScope: {}\nStatus: {status}\nPlanned: {}\nRefreshed: {}\nUnchanged: {}\nFailed: {}\nSkipped: {}",
        render.scope,
        render.planned.len(),
        render.refreshed.len(),
        render.unchanged.len(),
        render.failed.len(),
        render.skipped.len()
    );
    let mut outcome = super::scoped_outcome("refresh", &render.scope, payload, text);
    outcome.exit_code = exit_code;
    outcome
}

fn refresh_status(
    dry_run: bool,
    refreshed: usize,
    unchanged: usize,
    failed: usize,
) -> &'static str {
    if dry_run {
        "dry_run"
    } else if failed > 0 && refreshed == 0 && unchanged == 0 {
        "failed"
    } else if failed > 0 {
        "partial"
    } else if refreshed > 0 {
        "refreshed"
    } else {
        "unchanged"
    }
}

#[derive(Debug)]
struct Selection {
    candidates: Vec<SourceRecord>,
    planned: Vec<RefreshPlan>,
    skipped: Vec<SkippedRefresh>,
    failed: Vec<RefreshFailure>,
}

#[derive(Debug)]
struct ChangedRefresh {
    result: ingest::IngestResult,
    previous_raw_path: PathBuf,
    removed_paths: Vec<PathBuf>,
    degradations: Vec<String>,
}

struct RefreshSinks<'a> {
    refreshed: &'a mut Vec<RefreshedSource>,
    unchanged: &'a mut Vec<UnchangedRefresh>,
    failed: &'a mut Vec<RefreshFailure>,
    degradations: &'a mut Vec<String>,
}

#[derive(Debug)]
struct RefreshRender {
    scope: ScopeIdentity,
    dry_run: bool,
    planned: Vec<RefreshPlan>,
    refreshed: Vec<RefreshedSource>,
    unchanged: Vec<UnchangedRefresh>,
    failed: Vec<RefreshFailure>,
    skipped: Vec<SkippedRefresh>,
    indexed: Option<IndexedCounts>,
    index_status: IndexStatus,
    degradations: Vec<String>,
    explicit: bool,
}

#[derive(Debug, Clone, Serialize)]
struct RefreshPlan {
    #[serde(skip)]
    record: SourceRecord,
    id: String,
    location: String,
    source_kind: SourceKind,
    replay_kind: &'static str,
    raw_path: PathBuf,
    content_hash: String,
}

impl RefreshPlan {
    fn from_record(record: &SourceRecord) -> Self {
        Self {
            record: record.clone(),
            id: record.id.clone(),
            location: record.location.clone(),
            source_kind: record.kind.clone(),
            replay_kind: replay_kind_name(record),
            raw_path: raw_source_path(&record.id).unwrap_or_else(|_| PathBuf::from("raw")),
            content_hash: record.content_hash.clone(),
        }
    }
}

#[derive(Debug, Serialize)]
struct RefreshedSource {
    old_id: String,
    new_id: String,
    location: String,
    source_kind: SourceKind,
    replay_kind: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    final_url: Option<String>,
    raw_path: PathBuf,
    previous_raw_path: PathBuf,
    removed_paths: Vec<PathBuf>,
    changed: bool,
    source: SourceRecord,
}

#[derive(Debug, Serialize)]
struct UnchangedRefresh {
    id: String,
    location: String,
    source_kind: SourceKind,
    replay_kind: &'static str,
    raw_path: PathBuf,
    content_hash: String,
    changed: bool,
}

#[derive(Debug, Serialize)]
struct RefreshFailure {
    id: String,
    location: Option<String>,
    source_kind: Option<SourceKind>,
    code: String,
    message: String,
}

#[derive(Debug, Serialize)]
struct SkippedRefresh {
    id: String,
    location: String,
    source_kind: SourceKind,
    code: String,
    message: String,
}

#[derive(Debug, Clone, Serialize)]
struct IndexedCounts {
    documents: usize,
    chunks: usize,
    links: usize,
    sources: usize,
    ingestions: usize,
}

impl From<IndexCounts> for IndexedCounts {
    fn from(counts: IndexCounts) -> Self {
        Self {
            documents: counts.documents,
            chunks: counts.chunks,
            links: counts.links,
            sources: counts.sources,
            ingestions: counts.ingestions,
        }
    }
}

#[derive(Debug, Serialize)]
struct IndexStatus {
    status: &'static str,
    index_required: bool,
    indexed: Option<IndexedCounts>,
}

impl IndexStatus {
    fn not_run() -> Self {
        Self {
            status: "not_run",
            index_required: false,
            indexed: None,
        }
    }

    fn indexed(indexed: IndexedCounts) -> Self {
        Self {
            status: "indexed",
            index_required: false,
            indexed: Some(indexed),
        }
    }

    fn degraded() -> Self {
        Self {
            status: "degraded",
            index_required: false,
            indexed: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::IngestFileOptions;
    use crate::sources::{CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceReplay};

    fn test_scope(root: &Path) -> ResolvedScope {
        ResolvedScope::topic(
            "refresh-test".to_string(),
            root.to_path_buf(),
            root.join("registry.toml"),
        )
    }

    fn seed_url(root: &Path, location: &str, fetched_at: &str, body: &[u8]) -> SourceRecord {
        SourceManifest::register(
            root,
            SourceDraft {
                location: location.to_string(),
                kind: SourceKind::Url,
                fetched_at: fetched_at.to_string(),
                content: body.to_vec(),
                title: Some("Example".to_string()),
                citation: Some(location.to_string()),
                license: None,
                ingestion_method: IngestionMethod::Manual,
                compile_status: CompileStatus::Pending,
            },
        )
        .expect("register source")
    }

    fn seed_legacy_file(root: &Path) -> SourceRecord {
        SourceManifest::register(
            root,
            SourceDraft {
                location: "/tmp/source.txt".to_string(),
                kind: SourceKind::File,
                fetched_at: "2026-06-02T00:00:00Z".to_string(),
                content: b"file".to_vec(),
                title: Some("File".to_string()),
                citation: None,
                license: None,
                ingestion_method: IngestionMethod::Manual,
                compile_status: CompileStatus::Pending,
            },
        )
        .expect("register source")
    }

    fn seed_local_file(root: &Path, relative_path: &str, body: &[u8]) -> SourceRecord {
        let path = root.join(relative_path);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).expect("create source parent");
        }
        fs::write(&path, body).expect("write replay source");
        let kind = if relative_path.ends_with(".md") {
            SourceKind::Markdown
        } else if relative_path.ends_with(".txt") {
            SourceKind::Text
        } else {
            SourceKind::File
        };
        let record = SourceManifest::register(
            root,
            SourceDraft {
                location: relative_path.to_string(),
                kind,
                fetched_at: "2026-06-02T00:00:00Z".to_string(),
                content: body.to_vec(),
                title: Some("Local file".to_string()),
                citation: Some(relative_path.to_string()),
                license: None,
                ingestion_method: IngestionMethod::Manual,
                compile_status: CompileStatus::Pending,
            },
        )
        .expect("register local source");
        let replay = SourceReplay::local_file(
            path,
            &IngestFileOptions {
                no_ai: true,
                video_frame_interval_seconds: Some(0),
                ..IngestFileOptions::default()
            },
        );
        SourceManifest::update(root, |manifest| {
            manifest
                .entries
                .iter_mut()
                .find(|entry| entry.id == record.id)
                .expect("seeded local source")
                .replay = Some(replay);
            Ok(true)
        })
        .expect("write local replay metadata");
        SourceManifest::read(root)
            .expect("read manifest")
            .entries
            .into_iter()
            .find(|entry| entry.id == record.id)
            .expect("updated local source")
    }

    fn seed_unsupported_connector(root: &Path) -> SourceRecord {
        SourceManifest::register(
            root,
            SourceDraft {
                location: "stdin:source".to_string(),
                kind: SourceKind::Stdin,
                fetched_at: "2026-06-02T00:00:00Z".to_string(),
                content: b"stdin".to_vec(),
                title: Some("Stdin".to_string()),
                citation: None,
                license: None,
                ingestion_method: IngestionMethod::Manual,
                compile_status: CompileStatus::Pending,
            },
        )
        .expect("register unsupported connector")
    }

    fn snapshot(url: &str, body: &str) -> UrlSnapshot {
        UrlSnapshot {
            requested_url: url.to_string(),
            final_url: url.to_string(),
            fetched_at: "2026-06-02T00:00:00Z".to_string(),
            body: body.as_bytes().to_vec(),
            content_type: Some("text/html".to_string()),
        }
    }

    #[test]
    fn dry_run_plans_without_fetching_or_writing() {
        let temp = tempfile::tempdir().expect("tempdir");
        let record = seed_url(temp.path(), "https://example.test/a", "then", b"old");
        let mut fetched = false;

        let outcome = execute_resolved_with_fetcher(
            test_scope(temp.path()),
            vec![record.id.clone()],
            true,
            |_record, _fetched_at| {
                fetched = true;
                unreachable!("dry-run must not fetch")
            },
        )
        .expect("refresh dry-run");

        assert!(!fetched);
        assert_eq!(outcome.result.payload["status"], "dry_run");
        assert_eq!(outcome.result.payload["planned"][0]["id"], record.id);
        assert_eq!(
            SourceManifest::read(temp.path())
                .expect("read manifest")
                .entries
                .len(),
            1
        );
    }

    #[test]
    fn unchanged_content_does_not_rewrite_or_index() {
        let temp = tempfile::tempdir().expect("tempdir");
        let record = seed_url(temp.path(), "https://example.test/a", "then", b"same");

        let outcome = execute_resolved_with_fetcher(
            test_scope(temp.path()),
            vec![record.id.clone()],
            false,
            |record, _fetched_at| Ok(snapshot(&record.location, "same")),
        )
        .expect("refresh unchanged");

        assert_eq!(outcome.result.payload["status"], "unchanged");
        assert_eq!(outcome.result.payload["unchanged"][0]["id"], record.id);
        assert_eq!(outcome.result.payload["index_status"]["status"], "not_run");
        assert_eq!(
            SourceManifest::read(temp.path())
                .expect("read manifest")
                .entries
                .len(),
            1
        );
    }

    #[test]
    fn unchanged_local_file_does_not_replay_or_index() {
        let temp = tempfile::tempdir().expect("tempdir");
        let record = seed_local_file(temp.path(), "notes.md", b"# Same\n");

        let outcome = execute_resolved_with_fetcher(
            test_scope(temp.path()),
            vec![record.id.clone()],
            false,
            |_record, _fetched_at| unreachable!("local refresh does not fetch URLs"),
        )
        .expect("refresh local unchanged");

        assert_eq!(outcome.result.payload["status"], "unchanged");
        assert_eq!(outcome.result.payload["unchanged"][0]["id"], record.id);
        assert_eq!(
            outcome.result.payload["unchanged"][0]["replay_kind"],
            "local_file"
        );
        assert_eq!(outcome.result.payload["index_status"]["status"], "not_run");
        assert_eq!(
            SourceManifest::read(temp.path())
                .expect("read manifest")
                .entries
                .len(),
            1
        );
    }

    #[test]
    fn changed_content_replaces_manifest_and_removes_old_raw() {
        let temp = tempfile::tempdir().expect("tempdir");
        let record = seed_url(temp.path(), "https://example.test/a", "then", b"old");
        let old_raw = temp
            .path()
            .join(raw_source_path(&record.id).expect("raw path"));
        fs::write(&old_raw, "old raw").expect("write old raw");

        let outcome = execute_resolved_with_fetcher(
            test_scope(temp.path()),
            vec![record.id.clone()],
            false,
            |record, _fetched_at| {
                Ok(snapshot(
                    &record.location,
                    "<html><title>New</title><body>new</body></html>",
                ))
            },
        )
        .expect("refresh changed");

        assert_eq!(outcome.result.payload["status"], "refreshed");
        let refreshed = &outcome.result.payload["refreshed"][0];
        assert_eq!(refreshed["old_id"], record.id);
        assert_ne!(refreshed["new_id"], refreshed["old_id"]);
        assert!(!old_raw.exists());

        let manifest = SourceManifest::read(temp.path()).expect("read manifest");
        assert_eq!(manifest.entries.len(), 1);
        assert_eq!(
            manifest.entries[0].id,
            refreshed["new_id"].as_str().unwrap()
        );
    }

    #[test]
    fn changed_local_file_replays_and_removes_old_raw_assets() {
        let temp = tempfile::tempdir().expect("tempdir");
        let record = seed_local_file(temp.path(), "artifact.bin", b"old");
        let old_raw = temp
            .path()
            .join(raw_source_path(&record.id).expect("raw path"));
        let old_asset = temp
            .path()
            .join("raw")
            .join("assets")
            .join(format!("{}.bin", record.id));
        fs::create_dir_all(old_asset.parent().expect("asset parent")).expect("asset dir");
        fs::write(&old_raw, "old raw").expect("write old raw");
        fs::write(&old_asset, "old asset").expect("write old asset");
        fs::write(temp.path().join("artifact.bin"), b"new").expect("change source");

        let outcome = execute_resolved_with_fetcher(
            test_scope(temp.path()),
            vec![record.id.clone()],
            false,
            |_record, _fetched_at| unreachable!("local refresh does not fetch URLs"),
        )
        .expect("refresh local changed");

        assert_eq!(outcome.result.payload["status"], "refreshed");
        let refreshed = &outcome.result.payload["refreshed"][0];
        assert_eq!(refreshed["old_id"], record.id);
        assert_eq!(refreshed["replay_kind"], "local_file");
        let new_id = refreshed["new_id"].as_str().expect("new source id");
        assert_ne!(new_id, record.id);
        assert!(!old_raw.exists());
        assert!(!old_asset.exists());
        assert!(temp.path().join(format!("raw/{new_id}.md")).is_file());
        assert!(
            temp.path()
                .join(format!("raw/assets/{new_id}.bin"))
                .is_file()
        );
        assert_eq!(outcome.result.payload["index_status"]["status"], "indexed");

        let manifest = SourceManifest::read(temp.path()).expect("read manifest");
        assert_eq!(manifest.entries.len(), 1);
        assert_eq!(manifest.entries[0].id, new_id);
        assert!(manifest.entries[0].replay.is_some());
    }

    #[test]
    fn explicit_unsupported_and_missing_sources_fail_structurally() {
        let temp = tempfile::tempdir().expect("tempdir");
        let file = seed_legacy_file(temp.path());

        let outcome = execute_resolved_with_fetcher(
            test_scope(temp.path()),
            vec![file.id.clone(), "missing".to_string()],
            false,
            |_record, _fetched_at| unreachable!("unsupported and missing do not fetch"),
        )
        .expect("refresh unsupported");

        assert_eq!(outcome.exit_code, 1);
        assert_eq!(outcome.result.payload["status"], "failed");
        assert_eq!(
            outcome.result.payload["failed"].as_array().unwrap().len(),
            2
        );
        assert_eq!(
            outcome.result.payload["failed"][0]["code"],
            "missing_replay_metadata"
        );
        assert_eq!(outcome.result.payload["failed"][1]["code"], "not_found");
    }

    #[test]
    fn all_source_refresh_skips_unsupported_records() {
        let temp = tempfile::tempdir().expect("tempdir");
        let url = seed_url(temp.path(), "https://example.test/a", "then", b"same");
        let file = seed_unsupported_connector(temp.path());

        let outcome = execute_resolved_with_fetcher(
            test_scope(temp.path()),
            Vec::new(),
            false,
            |record, _fetched_at| Ok(snapshot(&record.location, "same")),
        )
        .expect("refresh all");

        assert_eq!(outcome.result.payload["status"], "unchanged");
        assert_eq!(outcome.result.payload["planned"][0]["id"], url.id);
        assert_eq!(outcome.result.payload["skipped"][0]["id"], file.id);
    }
}
