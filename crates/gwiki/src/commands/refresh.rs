use std::collections::BTreeSet;
use std::fs;
use std::path::{Component, Path, PathBuf};

use serde::Serialize;
use serde_json::json;

use crate::commands::index;
use crate::ingest;
use crate::ingest::url::{UrlIngestFailure, UrlSnapshot};
use crate::scope::ResolvedScope;
use crate::sources::{SourceManifest, SourceRecord};
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
        match fetch(record, &fetched_at) {
            Ok(snapshot) => {
                let source_hash = gobby_core::indexing::content_hash(&snapshot.body);
                let raw_path = raw_source_path(&record.id)?;
                if source_hash == record.content_hash {
                    unchanged.push(UnchangedRefresh {
                        id: record.id.clone(),
                        location: record.location.clone(),
                        raw_path,
                        content_hash: record.content_hash.clone(),
                        changed: false,
                    });
                    continue;
                }

                let final_url = snapshot.final_url.clone();
                match refresh_changed_source(scope.root(), record, snapshot) {
                    Ok(change) => refreshed.push(RefreshedSource {
                        old_id: record.id.clone(),
                        new_id: change.result.record.id.clone(),
                        location: record.location.clone(),
                        final_url,
                        raw_path: change.result.raw_path,
                        previous_raw_path: change.previous_raw_path,
                        removed_paths: change.removed_paths,
                        changed: true,
                        source: change.result.record,
                    }),
                    Err(error) => failed.push(RefreshFailure {
                        id: record.id.clone(),
                        location: Some(record.location.clone()),
                        code: error.code().to_string(),
                        message: error.to_string(),
                    }),
                }
            }
            Err(error) => failed.push(RefreshFailure {
                id: record.id.clone(),
                location: Some(record.location.clone()),
                code: error.code,
                message: error.message,
            }),
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

fn refresh_changed_source(
    vault_root: &Path,
    previous: &SourceRecord,
    snapshot: UrlSnapshot,
) -> Result<ChangedRefresh, WikiError> {
    let previous_raw_path = raw_source_path(&previous.id)?;
    let mut previous_paths = vec![previous_raw_path.clone()];
    previous_paths.extend(source_asset_paths_for_id(vault_root, &previous.id)?);

    let result = ingest::url::ingest_snapshot_without_index(vault_root, snapshot)?;
    SourceManifest::update(vault_root, |manifest| {
        let before = manifest.entries.len();
        manifest.entries.retain(|entry| entry.id != previous.id);
        Ok(manifest.entries.len() != before)
    })?;

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

    Ok(ChangedRefresh {
        result,
        previous_raw_path,
        removed_paths,
    })
}

fn select_sources(entries: &[SourceRecord], source_ids: &[String]) -> Selection {
    if source_ids.is_empty() {
        let mut planned = Vec::new();
        let mut skipped = Vec::new();
        for record in entries {
            if is_url_source(record) {
                planned.push(RefreshPlan::from_record(record));
            } else {
                skipped.push(SkippedRefresh {
                    id: record.id.clone(),
                    location: record.location.clone(),
                    code: "unsupported_source_kind".to_string(),
                    message: format!(
                        "source `{}` is not URL-backed and cannot be refreshed by v1 refresh",
                        record.id
                    ),
                });
            }
        }
        return Selection {
            candidates: planned.iter().map(|plan| plan.record.clone()).collect(),
            planned,
            skipped,
            failed: Vec::new(),
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
                code: "not_found".to_string(),
                message: format!("source `{id}` was not found"),
            });
            continue;
        };
        if is_url_source(record) {
            planned.push(RefreshPlan::from_record(record));
        } else {
            failed.push(RefreshFailure {
                id: record.id.clone(),
                location: Some(record.location.clone()),
                code: "unsupported_source_kind".to_string(),
                message: format!(
                    "source `{}` is not URL-backed and cannot be refreshed by v1 refresh",
                    record.id
                ),
            });
        }
    }

    Selection {
        candidates: planned.iter().map(|plan| plan.record.clone()).collect(),
        planned,
        skipped: Vec::new(),
        failed,
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
    raw_path: PathBuf,
    content_hash: String,
}

impl RefreshPlan {
    fn from_record(record: &SourceRecord) -> Self {
        Self {
            record: record.clone(),
            id: record.id.clone(),
            location: record.location.clone(),
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
    final_url: String,
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
    raw_path: PathBuf,
    content_hash: String,
    changed: bool,
}

#[derive(Debug, Serialize)]
struct RefreshFailure {
    id: String,
    location: Option<String>,
    code: String,
    message: String,
}

#[derive(Debug, Serialize)]
struct SkippedRefresh {
    id: String,
    location: String,
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
    use crate::sources::{CompileStatus, IngestionMethod, SourceDraft, SourceKind};

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

    fn seed_file(root: &Path) -> SourceRecord {
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
    fn explicit_unsupported_and_missing_sources_fail_structurally() {
        let temp = tempfile::tempdir().expect("tempdir");
        let file = seed_file(temp.path());

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
            "unsupported_source_kind"
        );
        assert_eq!(outcome.result.payload["failed"][1]["code"], "not_found");
    }

    #[test]
    fn all_source_refresh_skips_unsupported_records() {
        let temp = tempfile::tempdir().expect("tempdir");
        let url = seed_url(temp.path(), "https://example.test/a", "then", b"same");
        let file = seed_file(temp.path());

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
