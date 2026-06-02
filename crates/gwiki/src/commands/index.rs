use std::path::{Path, PathBuf};

use gobby_core::ai_context::{AiConfigSource, AiContext, LocalAiConfigSource};
use gobby_core::config::ConfigSource;
use serde_json::json;

use crate::ingest::{self, IngestResult};
use crate::support::counts::{IndexCounts, index_counts, postgres_index_counts};
use crate::support::env::database_url_from_env;
use crate::support::scope::{
    resolve_command_scope, resolved_scope_identity, search_scope_for_resolved,
    store_scope_for_search,
};
use crate::support::search::PostgresConfigSource;
use crate::support::time::collect_timestamp;
use crate::{
    CommandOutcome, IngestFileOptions, ScopeIdentity, ScopeKind, ScopeSelection, WikiError,
    indexer, store, vault,
};

const VIDEO_FRAME_INTERVAL_KEY: &str = "gwiki.ingest.video_frame_interval_seconds";

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    ensure_scope_root(&scope)?;
    let output_scope = resolved_scope_identity(&scope);
    let counts = index_resolved_scope(&scope)?;
    Ok(render_index(output_scope, scope.root(), counts))
}

pub(crate) fn index_resolved_scope(
    scope: &crate::scope::ResolvedScope,
) -> Result<IndexCounts, WikiError> {
    if let Some(database_url) = database_url_from_env() {
        let mut conn = gobby_core::postgres::connect_readwrite(&database_url).map_err(|error| {
            WikiError::Config {
                detail: format!("failed to connect to PostgreSQL for gwiki index: {error}"),
            }
        })?;
        let search_scope = search_scope_for_resolved(scope);
        let mut store =
            store::PostgresWikiStore::new(&mut conn, store_scope_for_search(&search_scope));
        indexer::index_vault(scope.root(), &mut store)?;
        return postgres_index_counts(&mut conn, &search_scope);
    }

    let mut store = store::MemoryWikiStore::default();
    indexer::index_vault(scope.root(), &mut store)?;
    Ok(index_counts(&store))
}

pub(crate) fn execute_ingest_file(
    path: PathBuf,
    selection: ScopeSelection,
    options: IngestFileOptions,
) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    // Vault initialization is idempotent here; ingest only needs the paths to exist.
    let initialized = vault::initialize(&scope)?;
    if !initialized.directories.is_empty() || !initialized.files.is_empty() {
        log::debug!(
            "initialized gwiki vault paths before ingest-file: directories={:?} files={:?}",
            initialized.directories,
            initialized.files
        );
    }
    let output_scope = resolved_scope_identity(&scope);
    let project_id = ai_project_id(&output_scope);
    let gobby_home = gobby_home()?;
    let fetched_at = collect_timestamp().map_err(|error| WikiError::Config {
        detail: format!("failed to read system clock: {error}"),
    })?;
    if let Some(database_url) = database_url_from_env() {
        let mut conn = gobby_core::postgres::connect_readwrite(&database_url).map_err(|error| {
            WikiError::Config {
                detail: format!("failed to connect to PostgreSQL for gwiki ingest-file: {error}"),
            }
        })?;
        let (ai_context, options) = {
            let primary = PostgresConfigSource { conn: &mut conn };
            let mut source = AiConfigSource::with_primary_from_gobby_home(primary, &gobby_home)
                .map_err(|error| WikiError::Config {
                    detail: format!("failed to resolve AI config for gwiki ingest-file: {error}"),
                })?;
            resolve_ingest_ai_context(project_id, &options, &mut source)?
        };
        let search_scope = search_scope_for_resolved(&scope);
        let result = {
            let mut store =
                store::PostgresWikiStore::new(&mut conn, store_scope_for_search(&search_scope));
            ingest::file::ingest_path(
                scope.root(),
                &mut store,
                &output_scope,
                &ai_context,
                &options,
                &path,
                &fetched_at,
            )?
        };
        let counts = postgres_index_counts(&mut conn, &search_scope)?;
        return Ok(render_ingest_file(&path, output_scope, &result, counts));
    }

    let mut source =
        LocalAiConfigSource::from_gobby_home(&gobby_home).map_err(|error| WikiError::Config {
            detail: format!("failed to resolve AI config for gwiki ingest-file: {error}"),
        })?;
    let (ai_context, options) = resolve_ingest_ai_context(project_id, &options, &mut source)?;
    let mut store = store::MemoryWikiStore::default();
    let result = ingest::file::ingest_path(
        scope.root(),
        &mut store,
        &output_scope,
        &ai_context,
        &options,
        &path,
        &fetched_at,
    )?;
    let counts = index_counts(&store);
    Ok(render_ingest_file(&path, output_scope, &result, counts))
}

pub(crate) fn execute_ingest_url(
    urls: Vec<String>,
    selection: ScopeSelection,
) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    // Vault initialization is idempotent here; ingest only needs the paths to exist.
    let initialized = vault::initialize(&scope)?;
    if !initialized.directories.is_empty() || !initialized.files.is_empty() {
        log::debug!(
            "initialized gwiki vault paths before ingest-url: directories={:?} files={:?}",
            initialized.directories,
            initialized.files
        );
    }
    let output_scope = resolved_scope_identity(&scope);
    let fetched_at = collect_timestamp().map_err(|error| WikiError::Config {
        detail: format!("failed to read system clock: {error}"),
    })?;
    if let Some(database_url) = database_url_from_env() {
        let mut conn = gobby_core::postgres::connect_readwrite(&database_url).map_err(|error| {
            WikiError::Config {
                detail: format!("failed to connect to PostgreSQL for gwiki ingest-url: {error}"),
            }
        })?;
        let search_scope = search_scope_for_resolved(&scope);
        let result = {
            let mut store =
                store::PostgresWikiStore::new(&mut conn, store_scope_for_search(&search_scope));
            ingest::url::ingest_urls(scope.root(), &mut store, &urls, &fetched_at)?
        };
        let counts = if result.accepted.is_empty() {
            IndexCounts::default()
        } else {
            postgres_index_counts(&mut conn, &search_scope)?
        };
        return Ok(render_ingest_url(output_scope, &result, counts));
    }

    let mut store = store::MemoryWikiStore::default();
    let result = ingest::url::ingest_urls(scope.root(), &mut store, &urls, &fetched_at)?;
    let counts = index_counts(&store);
    Ok(render_ingest_url(output_scope, &result, counts))
}

fn resolve_ingest_ai_context(
    project_id: Option<String>,
    options: &IngestFileOptions,
    source: &mut impl ConfigSource,
) -> Result<(AiContext, IngestFileOptions), WikiError> {
    let mut context = AiContext::resolve(project_id, source);
    let mut options = options.clone();
    if options.video_frame_interval_seconds.is_none() {
        options.video_frame_interval_seconds = Some(resolve_video_frame_interval_seconds(source)?);
    }
    options.apply_to_ai_context(&mut context);
    Ok((context, options))
}

fn resolve_video_frame_interval_seconds(source: &mut impl ConfigSource) -> Result<u32, WikiError> {
    let Some(raw_value) = source.config_value(VIDEO_FRAME_INTERVAL_KEY) else {
        return Ok(ingest::video::DEFAULT_FRAME_INTERVAL_SECONDS);
    };
    let value = source
        .resolve_value(&raw_value)
        .map_err(|error| WikiError::Config {
            detail: format!("failed to resolve {VIDEO_FRAME_INTERVAL_KEY}: {error}"),
        })?;
    value
        .trim()
        .parse::<u32>()
        .map_err(|error| WikiError::Config {
            detail: format!("invalid {VIDEO_FRAME_INTERVAL_KEY} value `{value}`: {error}"),
        })
}

fn ai_project_id(scope: &ScopeIdentity) -> Option<String> {
    (scope.kind == ScopeKind::Project).then(|| scope.id.clone())
}

fn gobby_home() -> Result<PathBuf, WikiError> {
    if let Some(home) = std::env::var_os("GOBBY_HOME") {
        return Ok(PathBuf::from(home));
    }

    dirs::home_dir()
        .map(|home| home.join(".gobby"))
        .ok_or_else(|| WikiError::Config {
            detail: "failed to resolve home directory for gwiki AI config".to_string(),
        })
}

fn render_index(scope: ScopeIdentity, root: &Path, counts: IndexCounts) -> CommandOutcome {
    let payload = json!({
        "command": "index",
        "scope": scope,
        "status": "indexed",
        "root": root,
        "indexed": {
            "documents": counts.documents,
            "chunks": counts.chunks,
            "links": counts.links,
            "sources": counts.sources,
            "ingestions": counts.ingestions,
        },
    });
    let text = format!(
        "Index complete
Scope: {scope}
Documents: {}
Chunks: {}
Links: {}
Sources: {}
Ingestions: {}",
        counts.documents, counts.chunks, counts.links, counts.sources, counts.ingestions
    );
    super::scoped_outcome("index", &scope, payload, text)
}

fn render_ingest_file(
    path: &Path,
    scope: ScopeIdentity,
    result: &IngestResult,
    counts: IndexCounts,
) -> CommandOutcome {
    let payload = json!({
        "command": "ingest-file",
        "scope": scope,
        "status": "ingested",
        "path": path,
        "raw_path": &result.raw_path,
        "asset_path": &result.asset_path,
        "source": {
            "id": &result.record.id,
            "kind": &result.record.kind,
            "content_hash": &result.record.content_hash,
            "location": &result.record.location,
        },
        "indexed": {
            "documents": counts.documents,
            "chunks": counts.chunks,
            "links": counts.links,
            "sources": counts.sources,
            "ingestions": counts.ingestions,
        },
    });
    let text = format!(
        "Ingested file
Scope: {scope}
Raw: {}
Asset: {}
Source: {} ({})
Content hash: {}
Documents: {}
Chunks: {}
Links: {}
Sources: {}
Ingestions: {}",
        ingest::path_to_string(&result.raw_path),
        result
            .asset_path
            .as_ref()
            .map(|path| ingest::path_to_string(path))
            .unwrap_or_else(|| "<none>".to_string()),
        result.record.location,
        result.record.kind,
        result.record.content_hash,
        counts.documents,
        counts.chunks,
        counts.links,
        counts.sources,
        counts.ingestions
    );
    super::scoped_outcome("ingest-file", &scope, payload, text)
}

fn render_ingest_url(
    scope: ScopeIdentity,
    result: &ingest::url::UrlBatchIngest,
    counts: IndexCounts,
) -> CommandOutcome {
    let accepted = result
        .accepted
        .iter()
        .map(|accepted| {
            json!({
                "requested_url": &accepted.requested_url,
                "final_url": &accepted.final_url,
                "raw_path": &accepted.result.raw_path,
                "source": {
                    "id": &accepted.result.record.id,
                    "kind": &accepted.result.record.kind,
                    "content_hash": &accepted.result.record.content_hash,
                    "location": &accepted.result.record.location,
                },
            })
        })
        .collect::<Vec<_>>();
    let failed = result
        .failed
        .iter()
        .map(|failure| {
            json!({
                "url": &failure.url,
                "code": &failure.code,
                "message": &failure.message,
            })
        })
        .collect::<Vec<_>>();
    let payload = json!({
        "command": "ingest-url",
        "scope": scope,
        "status": result.status(),
        "accepted": accepted,
        "failed": failed,
        "indexed": {
            "documents": counts.documents,
            "chunks": counts.chunks,
            "links": counts.links,
            "sources": counts.sources,
            "ingestions": counts.ingestions,
        },
    });
    let text = format!(
        "Ingested URLs
Scope: {scope}
Status: {}
Accepted: {}
Failed: {}
Documents: {}
Chunks: {}
Links: {}
Sources: {}
Ingestions: {}",
        result.status(),
        result.accepted.len(),
        result.failed.len(),
        counts.documents,
        counts.chunks,
        counts.links,
        counts.sources,
        counts.ingestions
    );
    let mut outcome = super::scoped_outcome("ingest-url", &scope, payload, text);
    outcome.exit_code = result.exit_code();
    outcome
}

fn ensure_scope_root(scope: &crate::scope::ResolvedScope) -> Result<(), WikiError> {
    if scope.root().is_dir() {
        return Ok(());
    }
    Err(WikiError::InvalidScope {
        detail: format!(
            "wiki scope root is missing or not a directory: {}",
            scope.root().display()
        ),
    })
}
