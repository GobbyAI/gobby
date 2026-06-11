use std::path::{Path, PathBuf};

#[cfg(feature = "ai")]
use gobby_core::ai::effective_route;
use gobby_core::ai_context::{AiConfigSource, AiContext, LocalAiConfigSource};
use gobby_core::config::{
    AiCapability, AiRouting, ConfigSource, resolve_embedding_config, resolve_falkordb_config,
    resolve_qdrant_config,
};
use gobby_core::degradation::{DegradationKind, ServiceState};
use postgres::Client;
use serde_json::json;

use crate::ingest::{self, IngestResult};
use crate::search::SearchScope;
use crate::support::config::{index_options_from_conn, local_index_options, qdrant_config_has_url};
use crate::support::counts::{IndexCounts, index_counts, postgres_index_counts};
use crate::support::env::database_url_for;
use crate::support::scope::{
    resolve_command_scope, resolved_scope_identity, search_scope_for_resolved,
    store_scope_for_search,
};
use crate::support::search::PostgresConfigSource;
use crate::support::text::degradation_label;
use crate::support::time::collect_timestamp;
use crate::{
    CommandOutcome, IngestFileOptions, ScopeIdentity, ScopeKind, ScopeSelection, WikiError,
    indexer, store, vault, vector,
};

const VIDEO_FRAME_INTERVAL_KEY: &str = "gwiki.ingest.video_frame_interval_seconds";
const QDRANT_SERVICE: &str = "qdrant";
const FALKORDB_SERVICE: &str = "falkordb";

struct IndexReport {
    counts: IndexCounts,
    degradations: Vec<DegradationKind>,
}

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    ensure_scope_root(&scope)?;
    let output_scope = resolved_scope_identity(&scope);
    let report = index_resolved_scope_report(&scope)?;
    Ok(render_index(output_scope, scope.root(), report))
}

pub(crate) fn index_resolved_scope(
    scope: &crate::scope::ResolvedScope,
) -> Result<IndexCounts, WikiError> {
    Ok(index_resolved_scope_report(scope)?.counts)
}

fn index_resolved_scope_report(
    scope: &crate::scope::ResolvedScope,
) -> Result<IndexReport, WikiError> {
    if let Some(database_url) = database_url_for("gwiki index")? {
        let mut conn = connect_postgres_index(&database_url, "gwiki index")?;
        let search_scope = search_scope_for_resolved(scope);
        let index_options = index_options_from_conn(&mut conn)?;
        {
            let mut store = postgres_store_for_search(&mut conn, &search_scope);
            indexer::index_vault_with_options(scope.root(), &mut store, index_options)?;
        }
        let mut degradations = Vec::new();
        if let Some(degradation) = sync_qdrant_vectors(&mut conn, &search_scope, "gwiki index")? {
            degradations.push(degradation);
        }
        if let Some(degradation) = sync_falkor_graph(&mut conn, &search_scope, "gwiki index")? {
            degradations.push(degradation);
        }
        let counts = indexed_counts_for_postgres(&mut conn, &search_scope, true)?;
        return Ok(IndexReport {
            counts,
            degradations,
        });
    }

    let index_options = local_index_options()?;
    let mut store = store::MemoryWikiStore::default();
    indexer::index_vault_with_options(scope.root(), &mut store, index_options)?;
    Ok(IndexReport {
        counts: index_counts(&store),
        degradations: Vec::new(),
    })
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
    let fetched_at = collect_timestamp()?;
    if let Some(database_url) = database_url_for("gwiki ingest-file")? {
        let mut conn = connect_postgres_index(&database_url, "gwiki ingest-file")?;
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
            let mut store = postgres_store_for_search(&mut conn, &search_scope);
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
        sync_qdrant_vectors(&mut conn, &search_scope, "gwiki ingest-file")?;
        let counts = indexed_counts_for_postgres(&mut conn, &search_scope, true)?;
        sync_falkor_graph(&mut conn, &search_scope, "gwiki ingest-file")?;
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
    let fetched_at = collect_timestamp()?;
    if let Some(database_url) = database_url_for("gwiki ingest-url")? {
        let mut conn = connect_postgres_index(&database_url, "gwiki ingest-url")?;
        let search_scope = search_scope_for_resolved(&scope);
        let result = {
            let mut store = postgres_store_for_search(&mut conn, &search_scope);
            ingest::url::ingest_urls(scope.root(), &mut store, &urls, &fetched_at)?
        };
        let counts =
            indexed_counts_for_postgres(&mut conn, &search_scope, !result.accepted.is_empty())?;
        if !result.accepted.is_empty() {
            sync_qdrant_vectors(&mut conn, &search_scope, "gwiki ingest-url")?;
            sync_falkor_graph(&mut conn, &search_scope, "gwiki ingest-url")?;
        }
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

pub(crate) fn resolve_ingest_file_ai_context(
    scope: &ScopeIdentity,
    options: &IngestFileOptions,
    command: &str,
) -> Result<(AiContext, IngestFileOptions), WikiError> {
    let project_id = ai_project_id(scope);
    let gobby_home = gobby_home()?;
    if let Some(database_url) = database_url_for(command)? {
        let mut conn = connect_postgres_index(&database_url, command)?;
        let primary = PostgresConfigSource { conn: &mut conn };
        let mut source = AiConfigSource::with_primary_from_gobby_home(primary, &gobby_home)
            .map_err(|error| WikiError::Config {
                detail: format!("failed to resolve AI config for {command}: {error}"),
            })?;
        return resolve_ingest_ai_context(project_id, options, &mut source);
    }

    let mut source =
        LocalAiConfigSource::from_gobby_home(&gobby_home).map_err(|error| WikiError::Config {
            detail: format!("failed to resolve AI config for {command}: {error}"),
        })?;
    resolve_ingest_ai_context(project_id, options, &mut source)
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
    let interval = value
        .trim()
        .parse::<u32>()
        .map_err(|error| WikiError::Config {
            detail: format!("invalid {VIDEO_FRAME_INTERVAL_KEY} value `{value}`: {error}"),
        })?;
    if interval == 0 {
        return Err(WikiError::Config {
            detail: format!(
                "invalid {VIDEO_FRAME_INTERVAL_KEY} value `{value}`: must be greater than 0"
            ),
        });
    }
    Ok(interval)
}

fn ai_project_id(scope: &ScopeIdentity) -> Option<String> {
    (scope.kind == ScopeKind::Project).then(|| scope.id.clone())
}

fn ai_project_id_for_search(scope: &SearchScope) -> Option<String> {
    match scope {
        SearchScope::Global => None,
        SearchScope::Project { project_id } => Some(project_id.clone()),
        SearchScope::Topic { .. } => None,
    }
}

fn gobby_home() -> Result<PathBuf, WikiError> {
    gobby_core::gobby_home().map_err(|error| WikiError::Config {
        detail: format!("failed to resolve Gobby home for gwiki AI config: {error}"),
    })
}

fn connect_postgres_index(database_url: &str, command: &str) -> Result<Client, WikiError> {
    gobby_core::postgres::connect_readwrite(database_url).map_err(|error| WikiError::Config {
        detail: format!("failed to connect to PostgreSQL for {command}: {error}"),
    })
}

fn postgres_store_for_search<'a>(
    conn: &'a mut Client,
    search_scope: &SearchScope,
) -> store::PostgresWikiStore<'a> {
    store::PostgresWikiStore::new(conn, store_scope_for_search(search_scope))
}

fn sync_falkor_graph(
    conn: &mut Client,
    search_scope: &SearchScope,
    command: &'static str,
) -> Result<Option<DegradationKind>, WikiError> {
    let gobby_home = gobby_home()?;
    let primary = PostgresConfigSource { conn };
    let mut source =
        AiConfigSource::with_primary_from_gobby_home(primary, &gobby_home).map_err(|error| {
            WikiError::Config {
                detail: format!("failed to resolve FalkorDB config for {command}: {error}"),
            }
        })?;
    let Some(falkor) = resolve_falkordb_config(&mut source) else {
        log::warn!("{command}: FalkorDB config not found; skipping gwiki graph sync");
        return Ok(Some(not_configured_degradation(FALKORDB_SERVICE)));
    };
    if let Err(error) = crate::falkor_graph::sync_scope_from_postgres(conn, search_scope, &falkor) {
        log::warn!(
            "{command}: FalkorDB graph sync failed; continuing with PostgreSQL index: {error}"
        );
        return Ok(Some(unreachable_degradation(FALKORDB_SERVICE, error)));
    }
    Ok(None)
}

fn sync_qdrant_vectors(
    conn: &mut Client,
    search_scope: &SearchScope,
    command: &'static str,
) -> Result<Option<DegradationKind>, WikiError> {
    let gobby_home = gobby_home()?;
    let (embedding, qdrant) = {
        let primary = PostgresConfigSource { conn };
        let mut source = AiConfigSource::with_primary_from_gobby_home(primary, &gobby_home)
            .map_err(|error| WikiError::Config {
                detail: format!("failed to resolve AI config for {command}: {error}"),
            })?;
        let ai_context = AiContext::resolve(ai_project_id_for_search(search_scope), &mut source);
        let embedding = resolve_vector_embedding(&ai_context, &mut source);
        let qdrant = resolve_qdrant_config(&mut source).filter(qdrant_config_has_url);
        (embedding, qdrant)
    };

    let Some(embedding) = embedding else {
        log::warn!("{command}: embedding config not found; skipping gwiki vector sync");
        return Ok(Some(not_configured_degradation(QDRANT_SERVICE)));
    };
    let Some(qdrant) = qdrant else {
        log::warn!("{command}: Qdrant config not found; skipping gwiki vector sync");
        return Ok(Some(not_configured_degradation(QDRANT_SERVICE)));
    };

    let mut source = vector::PostgresWikiVectorChunkSource::new(conn);
    let mut embedder = vector::GwikiEmbeddingBackend::new(embedding);
    let mut store = vector::GwikiQdrantVectorStore::new(qdrant);
    let outcome = match vector::sync_scope_vectors(
        search_scope,
        &mut source,
        &mut embedder,
        &mut store,
    ) {
        Ok(outcome) => outcome,
        Err(error) => {
            log::warn!(
                "{command}: Qdrant vector sync failed; continuing with PostgreSQL index: {error}"
            );
            return Ok(Some(qdrant_sync_degradation(error)));
        }
    };
    log::debug!(
        "{command}: synced gwiki Qdrant vectors: chunks={} upserted={} stale_paths_deleted={}",
        outcome.chunks,
        outcome.upserted,
        outcome.deleted_stale_paths
    );
    Ok(None)
}

fn qdrant_sync_degradation(error: vector::WikiVectorError) -> DegradationKind {
    unreachable_degradation(QDRANT_SERVICE, error)
}

fn not_configured_degradation(service: &'static str) -> DegradationKind {
    service_unavailable_degradation(service, ServiceState::NotConfigured)
}

fn unreachable_degradation(
    service: &'static str,
    error: impl std::fmt::Display,
) -> DegradationKind {
    service_unavailable_degradation(
        service,
        ServiceState::Unreachable {
            message: error.to_string(),
        },
    )
}

fn service_unavailable_degradation(service: &'static str, state: ServiceState) -> DegradationKind {
    DegradationKind::ServiceUnavailable {
        service: service.to_string(),
        state,
    }
}

fn resolve_vector_embedding(
    context: &AiContext,
    source: &mut impl ConfigSource,
) -> Option<crate::search::semantic::SemanticEmbedding> {
    match effective_embedding_route(context) {
        AiRouting::Off => None,
        AiRouting::Daemon => {
            #[cfg(feature = "ai")]
            {
                Some(crate::search::semantic::SemanticEmbedding::Daemon(
                    Box::new(context.clone()),
                ))
            }
            #[cfg(not(feature = "ai"))]
            {
                None
            }
        }
        AiRouting::Direct => {
            resolve_embedding_config(source).map(crate::search::semantic::SemanticEmbedding::Direct)
        }
        AiRouting::Auto => {
            #[cfg(feature = "ai")]
            {
                Some(crate::search::semantic::SemanticEmbedding::Daemon(
                    Box::new(context.clone()),
                ))
            }
            #[cfg(not(feature = "ai"))]
            {
                resolve_embedding_config(source)
                    .map(crate::search::semantic::SemanticEmbedding::Direct)
            }
        }
    }
}

fn effective_embedding_route(context: &AiContext) -> AiRouting {
    #[cfg(feature = "ai")]
    {
        effective_route(context, AiCapability::Embed)
    }
    #[cfg(not(feature = "ai"))]
    {
        match context.binding(AiCapability::Embed).routing {
            AiRouting::Off => AiRouting::Off,
            AiRouting::Direct => AiRouting::Direct,
            AiRouting::Daemon => {
                log::warn!(
                    "gwiki was built without ai support; daemon-backed embeddings are disabled"
                );
                AiRouting::Off
            }
            AiRouting::Auto => {
                log::warn!(
                    "gwiki was built without ai support; auto embedding route cannot use the daemon"
                );
                AiRouting::Direct
            }
        }
    }
}

fn indexed_counts_for_postgres(
    conn: &mut Client,
    search_scope: &SearchScope,
    should_count: bool,
) -> Result<IndexCounts, WikiError> {
    if should_count {
        postgres_index_counts(conn, search_scope)
    } else {
        Ok(IndexCounts::default())
    }
}

fn render_index(scope: ScopeIdentity, root: &Path, report: IndexReport) -> CommandOutcome {
    let IndexReport {
        counts,
        degradations,
    } = report;
    let degradation_labels = degradations
        .iter()
        .map(degradation_label)
        .collect::<Vec<_>>();
    let payload = json!({
        "command": "index",
        "scope": scope,
        "status": "indexed",
        "root": root.display().to_string(),
        "indexed": {
            "documents": counts.documents,
            "chunks": counts.chunks,
            "links": counts.links,
            "sources": counts.sources,
            "ingestions": counts.ingestions,
        },
        "degradations": degradations,
    });
    let degradations_text = if degradation_labels.is_empty() {
        "none".to_string()
    } else {
        degradation_labels.join(", ")
    };
    let mut text = format!(
        "Index complete
Scope: {scope}
Documents: {}
Chunks: {}
Links: {}
Sources: {}
Ingestions: {}",
        counts.documents, counts.chunks, counts.links, counts.sources, counts.ingestions
    );
    text.push_str("\nDegradations: ");
    text.push_str(&degradations_text);
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

#[cfg(test)]
mod tests {
    use super::*;

    struct TestConfigSource {
        value: Option<&'static str>,
    }

    impl ConfigSource for TestConfigSource {
        fn config_value(&mut self, key: &str) -> Option<String> {
            (key == VIDEO_FRAME_INTERVAL_KEY)
                .then(|| self.value.map(str::to_string))
                .flatten()
        }

        fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
            Ok(value.to_string())
        }
    }

    #[test]
    fn video_frame_interval_zero_is_invalid() {
        let mut source = TestConfigSource { value: Some("0") };
        let error = resolve_video_frame_interval_seconds(&mut source)
            .expect_err("zero interval must be invalid");

        assert!(matches!(error, WikiError::Config { .. }));
        assert!(error.to_string().contains("must be greater than 0"));
    }

    #[test]
    fn index_render_includes_empty_degradations() {
        let outcome = render_index(
            ScopeIdentity::project("project-1"),
            Path::new("/vault"),
            IndexReport {
                counts: sample_counts(),
                degradations: Vec::new(),
            },
        );

        assert!(
            outcome.result.payload["degradations"]
                .as_array()
                .expect("degradations array")
                .is_empty()
        );
        assert!(outcome.result.text.contains("Degradations: none"));
    }

    #[test]
    fn index_render_reports_qdrant_sync_failure_degradation() {
        let outcome = render_index(
            ScopeIdentity::project("project-1"),
            Path::new("/vault"),
            IndexReport {
                counts: sample_counts(),
                degradations: vec![qdrant_sync_degradation(vector::WikiVectorError::Qdrant(
                    "connection refused".to_string(),
                ))],
            },
        );

        let degradation = &outcome.result.payload["degradations"][0]["ServiceUnavailable"];
        assert_eq!(degradation["service"], "qdrant");
        assert_eq!(
            degradation["state"]["Unreachable"]["message"],
            "wiki vector qdrant error: connection refused"
        );
        assert!(
            outcome
                .result
                .text
                .contains("Degradations: qdrant_unreachable")
        );
    }

    #[test]
    #[cfg(not(feature = "ai"))]
    fn auto_embedding_route_falls_back_to_direct_without_ai() {
        let mut source = TestConfigSource { value: None };
        let context = AiContext::resolve(None, &mut source);

        assert_eq!(effective_embedding_route(&context), AiRouting::Direct);
    }

    fn sample_counts() -> IndexCounts {
        IndexCounts {
            documents: 3,
            chunks: 5,
            links: 7,
            sources: 11,
            ingestions: 13,
        }
    }
}
