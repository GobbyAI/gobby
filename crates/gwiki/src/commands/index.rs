use std::path::{Path, PathBuf};

use gobby_core::ai::effective_config::ai_source_for_conn;
#[cfg(feature = "ai")]
use gobby_core::ai::effective_route;
use gobby_core::ai_context::AiContext;
use gobby_core::config::{
    AiCapability, AiRouting, ConfigSource, resolve_falkordb_config, resolve_qdrant_config,
};
use gobby_core::degradation::{DegradationKind, ServiceState};
use gobby_core::progress::ProgressBar;
use postgres::Client;
use serde_json::json;

use crate::ingest;
use crate::progress::{ProgressOptions, ProgressPhase, ProgressSink};
use crate::search::SearchScope;
use crate::support::config::{index_options_from_conn, qdrant_config_has_url};
use crate::support::counts::{IndexCounts, postgres_index_counts};
use crate::support::env::database_url_for;
use crate::support::scope::{
    resolve_command_scope, resolved_scope_identity, search_scope_for_resolved,
    store_scope_for_search,
};
use crate::support::text::degradation_label;
use crate::support::time::collect_timestamp;
use crate::{
    CommandOutcome, IngestFileOptions, RunOptions, ScopeIdentity, ScopeKind, ScopeSelection,
    WikiError, indexer, store, vault, vector,
};

mod render;

use render::{render_ingest_file, render_ingest_url};

const VIDEO_FRAME_INTERVAL_KEY: &str = "gwiki.ingest.video_frame_interval_seconds";
const QDRANT_SERVICE: &str = "qdrant";
const FALKORDB_SERVICE: &str = "falkordb";

struct IndexReport {
    counts: IndexCounts,
    degradations: Vec<DegradationKind>,
}

pub(crate) struct StderrWikiProgress {
    quiet: bool,
    bar: Option<ProgressBar>,
}

impl StderrWikiProgress {
    pub(crate) fn new(quiet: bool) -> Self {
        Self { quiet, bar: None }
    }
}

impl ProgressSink for StderrWikiProgress {
    fn start(&mut self, phase: ProgressPhase, total: usize) {
        let mut bar = ProgressBar::new(total, self.quiet);
        bar.draw(phase_label(phase));
        self.bar = Some(bar);
    }

    fn advance(&mut self, phase: ProgressPhase, item: &str) {
        if let Some(bar) = self.bar.as_mut() {
            bar.tick(format!("{} {item}", phase_label(phase)));
        }
    }

    fn finish(&mut self, _phase: ProgressPhase) {
        if let Some(bar) = self.bar.as_mut() {
            bar.finish();
        }
        self.bar = None;
    }
}

fn phase_label(phase: ProgressPhase) -> &'static str {
    match phase {
        ProgressPhase::IngestFile => "ingest-file",
        ProgressPhase::IngestUrl => "ingest-url",
        ProgressPhase::SessionArchive => "session-archive",
        ProgressPhase::VaultIndex => "index",
        ProgressPhase::VectorSync => "qdrant",
        ProgressPhase::GraphSync => "falkor",
    }
}

pub(crate) fn execute(
    selection: ScopeSelection,
    run_options: RunOptions,
    force: bool,
) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    ensure_scope_root(&scope)?;
    let output_scope = resolved_scope_identity(&scope);
    let mut progress = StderrWikiProgress::new(run_options.quiet);
    let mut progress_options = ProgressOptions::with_sink(&mut progress);
    let report = index_resolved_scope_report(&scope, &mut progress_options, force)?;
    // Keep the deterministic catalog (`code/INDEX.md`, `knowledge/INDEX.md`,
    // `_index.md`) in sync with on-disk vault state after indexing. Previously
    // only `compile`/`recap` regenerated it, so the codewiki nightly flow
    // (`gwiki code` -> `gwiki index`, which rewrites/removes module pages)
    // left `code/INDEX.md` stale: it kept linking deleted synthetic-cluster
    // module pages, growing `curated_broken_link_count` every heal.
    crate::catalog::regenerate(scope.root(), &output_scope)?;
    Ok(render_index(output_scope, scope.root(), report))
}

pub(crate) fn index_resolved_scope(
    scope: &crate::scope::ResolvedScope,
) -> Result<IndexCounts, WikiError> {
    Ok(index_resolved_scope_report(scope, &mut ProgressOptions::default(), false)?.counts)
}

fn index_resolved_scope_report(
    scope: &crate::scope::ResolvedScope,
    progress: &mut ProgressOptions<'_>,
    force: bool,
) -> Result<IndexReport, WikiError> {
    if let Some(database_url) = database_url_for("gwiki index")? {
        let mut conn = connect_postgres_index(&database_url, "gwiki index")?;
        let search_scope = search_scope_for_resolved(scope);
        let mut index_options = index_options_from_conn(&mut conn)?;
        index_options.force = force;
        {
            let mut store = postgres_store_for_search(&mut conn, &search_scope);
            indexer::index_vault(scope.root(), &mut store, index_options, progress)?;
        }
        let mut degradations = Vec::new();
        if let Some(degradation) =
            sync_qdrant_vectors(&mut conn, &search_scope, "gwiki index", progress)?
        {
            degradations.push(degradation);
        }
        if let Some(degradation) =
            sync_falkor_graph(&mut conn, &search_scope, "gwiki index", progress)?
        {
            degradations.push(degradation);
        }
        let counts = indexed_counts_for_postgres(&mut conn, &search_scope, true)?;
        return Ok(IndexReport {
            counts,
            degradations,
        });
    }

    Err(WikiError::from(
        gobby_core::grant::GrantError::DaemonRequired,
    ))
}

pub(crate) fn execute_ingest_file(
    path: PathBuf,
    selection: ScopeSelection,
    options: IngestFileOptions,
    run_options: RunOptions,
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
    let fetched_at = collect_timestamp()?;
    let mut progress = StderrWikiProgress::new(run_options.quiet);
    let mut progress_options = ProgressOptions::with_sink(&mut progress);
    if let Some(database_url) = database_url_for("gwiki ingest-file")? {
        let mut conn = connect_postgres_index(&database_url, "gwiki ingest-file")?;
        let (ai_context, options) = {
            let mut source = ai_source_for_conn(&mut conn).map_err(|error| WikiError::Config {
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
                ingest::file::LocalFileSnapshot {
                    path: &path,
                    fetched_at: &fetched_at,
                },
                &mut progress_options,
            )?
        };
        sync_qdrant_vectors_for_paths(
            &mut conn,
            &search_scope,
            &[PathBuf::from("raw/INDEX.md")],
            "gwiki ingest-file",
            &mut progress_options,
        )?;
        let counts = indexed_counts_for_postgres(&mut conn, &search_scope, true)?;
        log::debug!("gwiki ingest-file deferred full Falkor graph reconciliation to gwiki index");
        crate::log::append_sources_ingested(scope.root(), &output_scope, &fetched_at, [&result])?;
        return Ok(render_ingest_file(&path, output_scope, &result, counts));
    }

    Err(WikiError::from(
        gobby_core::grant::GrantError::DaemonRequired,
    ))
}

pub(crate) fn execute_ingest_url(
    urls: Vec<String>,
    selection: ScopeSelection,
    max_age_hours: u64,
    run_options: RunOptions,
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
    let mut progress = StderrWikiProgress::new(run_options.quiet);
    let mut progress_options = ProgressOptions::with_sink(&mut progress);
    if let Some(database_url) = database_url_for("gwiki ingest-url")? {
        let mut conn = connect_postgres_index(&database_url, "gwiki ingest-url")?;
        let search_scope = search_scope_for_resolved(&scope);
        let result = {
            let mut store = postgres_store_for_search(&mut conn, &search_scope);
            ingest::url::ingest_urls(
                scope.root(),
                &mut store,
                &urls,
                &fetched_at,
                max_age_hours,
                &mut progress_options,
            )?
        };
        let counts =
            indexed_counts_for_postgres(&mut conn, &search_scope, !result.accepted.is_empty())?;
        if !result.accepted.is_empty() {
            sync_qdrant_vectors_for_paths(
                &mut conn,
                &search_scope,
                &[PathBuf::from("raw/INDEX.md")],
                "gwiki ingest-url",
                &mut progress_options,
            )?;
            log::debug!(
                "gwiki ingest-url deferred full Falkor graph reconciliation to gwiki index"
            );
        }
        crate::log::append_sources_ingested(
            scope.root(),
            &output_scope,
            &fetched_at,
            result.accepted.iter().map(|accepted| &accepted.result),
        )?;
        return Ok(render_ingest_url(output_scope, &result, counts));
    }

    Err(WikiError::from(
        gobby_core::grant::GrantError::DaemonRequired,
    ))
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
    if let Some(database_url) = database_url_for(command)? {
        let mut conn = connect_postgres_index(&database_url, command)?;
        let mut source = ai_source_for_conn(&mut conn).map_err(|error| WikiError::Config {
            detail: format!("failed to resolve AI config for {command}: {error}"),
        })?;
        return resolve_ingest_ai_context(project_id, options, &mut source);
    }

    Err(WikiError::from(
        gobby_core::grant::GrantError::DaemonRequired,
    ))
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

pub(crate) fn connect_postgres_index(
    database_url: &str,
    command: &str,
) -> Result<Client, WikiError> {
    gobby_core::postgres::connect_readwrite(database_url).map_err(|error| WikiError::Config {
        detail: format!("failed to connect to PostgreSQL for {command}: {error}"),
    })
}

pub(crate) fn postgres_store_for_search<'a>(
    conn: &'a mut Client,
    search_scope: &SearchScope,
) -> store::PostgresWikiStore<'a> {
    store::PostgresWikiStore::new(conn, store_scope_for_search(search_scope))
}

pub(crate) fn sync_falkor_graph(
    conn: &mut Client,
    search_scope: &SearchScope,
    command: &'static str,
    progress: &mut ProgressOptions<'_>,
) -> Result<Option<DegradationKind>, WikiError> {
    let mut source = ai_source_for_conn(conn).map_err(|error| WikiError::Config {
        detail: format!("failed to resolve FalkorDB config for {command}: {error}"),
    })?;
    let Some(falkor) = resolve_falkordb_config(&mut source) else {
        log::warn!("{command}: FalkorDB config not found; skipping gwiki graph sync");
        return Ok(Some(not_configured_degradation(FALKORDB_SERVICE)));
    };
    if let Err(error) =
        crate::falkor_graph::sync_scope_from_postgres(conn, search_scope, &falkor, progress)
    {
        log::warn!(
            "{command}: FalkorDB graph sync failed; continuing with PostgreSQL index: {error}"
        );
        return Ok(Some(unreachable_degradation(FALKORDB_SERVICE, error)));
    }
    Ok(None)
}

pub(crate) fn sync_qdrant_vectors(
    conn: &mut Client,
    search_scope: &SearchScope,
    command: &'static str,
    progress: &mut ProgressOptions<'_>,
) -> Result<Option<DegradationKind>, WikiError> {
    sync_qdrant_vectors_inner(conn, search_scope, None, command, progress)
}

fn sync_qdrant_vectors_for_paths(
    conn: &mut Client,
    search_scope: &SearchScope,
    paths: &[PathBuf],
    command: &'static str,
    progress: &mut ProgressOptions<'_>,
) -> Result<Option<DegradationKind>, WikiError> {
    sync_qdrant_vectors_inner(conn, search_scope, Some(paths), command, progress)
}

fn sync_qdrant_vectors_inner(
    conn: &mut Client,
    search_scope: &SearchScope,
    paths: Option<&[PathBuf]>,
    command: &'static str,
    progress: &mut ProgressOptions<'_>,
) -> Result<Option<DegradationKind>, WikiError> {
    let (embedding, qdrant) = {
        let mut source = ai_source_for_conn(conn).map_err(|error| WikiError::Config {
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
    let outcome = match paths {
        Some(paths) => vector::sync_scope_vectors_for_paths(
            search_scope,
            paths,
            &mut source,
            &mut embedder,
            &mut store,
            progress,
        ),
        None => vector::sync_scope_vectors(
            search_scope,
            &mut source,
            &mut embedder,
            &mut store,
            progress,
        ),
    };
    let outcome = match outcome {
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
            let _ = source;
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
            AiRouting::Daemon => {
                log::warn!(
                    "gwiki was built without ai support; daemon-backed embeddings are disabled"
                );
                AiRouting::Off
            }
        }
    }
}

pub(crate) fn indexed_counts_for_postgres(
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
    fn quiet_progress_sink_disables_progress_bar() {
        let mut progress = StderrWikiProgress::new(true);

        progress.start(ProgressPhase::VaultIndex, 2);

        assert!(progress.bar.as_ref().is_some_and(|bar| !bar.is_enabled()));
        progress.advance(ProgressPhase::VaultIndex, "notes/page.md");
        progress.finish(ProgressPhase::VaultIndex);
        assert!(progress.bar.is_none());
    }

    #[test]
    #[cfg(not(feature = "ai"))]
    fn auto_embedding_route_falls_back_to_direct_without_ai() {
        let mut source = TestConfigSource { value: None };
        let context = AiContext::resolve(None, &mut source);

        assert_eq!(effective_embedding_route(&context), AiRouting::Daemon);
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
