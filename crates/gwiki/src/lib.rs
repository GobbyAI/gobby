use std::fmt;
use std::path::{Path, PathBuf};

use gobby_core::config::{ConfigSource, resolve_embedding_config, resolve_qdrant_config};
use gobby_core::degradation::{DegradationKind, ServiceState};
use gobby_core::setup::{SetupContext, SetupError, StandaloneSetup};
use serde_json::json;

pub mod audit;
pub mod citations;
pub mod collect;
pub mod commands;
pub mod compile;
pub mod credibility;
pub mod daemon;
pub mod events;
pub mod exports;
pub mod frontmatter;
pub mod graph;
pub mod health;
pub mod indexer;
pub mod ingest;
pub mod links;
pub mod lint;
pub mod log;
pub mod markdown;
pub mod models;
pub mod output;
pub mod provenance;
pub mod registry;
pub mod research;
pub mod schema;
pub mod scope;
pub mod search;
pub mod session;
pub mod setup;
pub mod sources;
pub mod store;
pub mod synthesis;
pub mod transcribe;
pub mod vault;
pub mod video;
pub mod vision;

/// Parsed gwiki command passed in from the binary.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    Init {
        scope: ScopeSelection,
    },
    Setup {
        scope: ScopeSelection,
    },
    Index {
        scope: ScopeSelection,
    },
    Collect {
        scope: ScopeSelection,
    },
    IngestFile {
        path: PathBuf,
        scope: ScopeSelection,
    },
    Search {
        query: String,
        scope: ScopeSelection,
        limit: usize,
    },
    Backlinks {
        page: String,
        scope: ScopeSelection,
    },
    LinkSuggest {
        scope: ScopeSelection,
        limit: usize,
    },
    Research(research::ResearchOptions),
    Compile {
        topic: Option<String>,
        outline: Vec<String>,
        target_kind: synthesis::ArticleKind,
        target_page: Option<PathBuf>,
        write_intent: bool,
        scope: ScopeSelection,
    },
    Export {
        scope: ScopeSelection,
        command: exports::ExportCommand,
    },
    Audit {
        scope: ScopeSelection,
    },
    Lint {
        scope: ScopeSelection,
    },
    Health {
        scope: ScopeSelection,
    },
    Status {
        scope: ScopeSelection,
    },
}

/// Shared scope flags accepted by shell commands.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScopeSelection {
    project: bool,
    topic: Option<String>,
}

impl ScopeSelection {
    pub fn global() -> Self {
        Self {
            project: false,
            topic: None,
        }
    }

    pub fn project() -> Self {
        Self {
            project: true,
            topic: None,
        }
    }

    pub fn topic(topic: impl Into<String>) -> Self {
        Self {
            project: false,
            topic: Some(topic.into()),
        }
    }

    pub fn identity(&self) -> ScopeIdentity {
        if let Some(topic) = &self.topic {
            return ScopeIdentity::topic(topic.clone());
        }

        ScopeIdentity::project("current")
    }

    pub fn is_project(&self) -> bool {
        self.project
    }

    pub fn topic_name(&self) -> Option<&str> {
        self.topic.as_deref()
    }
}

impl Default for ScopeSelection {
    fn default() -> Self {
        Self::global()
    }
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct ScopeIdentity {
    pub kind: String,
    pub id: String,
}

impl ScopeIdentity {
    pub fn project(id: impl Into<String>) -> Self {
        Self {
            kind: "project".to_string(),
            id: id.into(),
        }
    }

    pub fn topic(id: impl Into<String>) -> Self {
        Self {
            kind: "topic".to_string(),
            id: id.into(),
        }
    }
}

impl fmt::Display for ScopeIdentity {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}:{}", self.kind, self.id)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct CommandOutcome {
    pub status_messages: Vec<String>,
    pub result: CommandResult,
}

#[derive(Debug, Clone, PartialEq)]
pub struct CommandResult {
    pub payload: serde_json::Value,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WikiError {
    NotImplemented {
        command: &'static str,
        detail: &'static str,
    },
    InvalidScope {
        detail: String,
    },
    Config {
        detail: String,
    },
    Io {
        action: &'static str,
        path: Option<PathBuf>,
        source: String,
    },
    Json {
        action: &'static str,
        path: Option<PathBuf>,
        source: String,
    },
    Registry {
        detail: String,
    },
    Daemon {
        endpoint: &'static str,
        message: String,
    },
    InvalidInput {
        field: &'static str,
        message: String,
    },
}

impl WikiError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::NotImplemented { .. } => "not_implemented",
            Self::InvalidScope { .. } => "invalid_scope",
            Self::Config { .. } => "config_error",
            Self::Io { .. } => "io_error",
            Self::Json { .. } => "json_error",
            Self::Registry { .. } => "registry_error",
            Self::Daemon { .. } => "daemon_error",
            Self::InvalidInput { .. } => "invalid_input",
        }
    }
}

impl fmt::Display for WikiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotImplemented { command, detail } => {
                write!(f, "{command}: {detail} ({})", self.code())
            }
            Self::InvalidScope { detail } | Self::Config { detail } | Self::Registry { detail } => {
                write!(f, "{detail} ({})", self.code())
            }
            Self::Io {
                action,
                path,
                source,
            }
            | Self::Json {
                action,
                path,
                source,
            } => match path {
                Some(path) => write!(
                    f,
                    "{action} failed for {}: {source} ({})",
                    path.display(),
                    self.code()
                ),
                None => write!(f, "{action} failed: {source} ({})", self.code()),
            },
            Self::Daemon { endpoint, message } => {
                write!(f, "{endpoint}: {message} ({})", self.code())
            }
            Self::InvalidInput { field, message } => {
                write!(f, "{field}: {message} ({})", self.code())
            }
        }
    }
}

impl std::error::Error for WikiError {}

pub fn run(command: Command) -> Result<CommandOutcome, WikiError> {
    match command {
        Command::Init { scope } => init(scope),
        Command::Setup { scope } => run_setup(scope),
        Command::Index { scope } => run_index(scope),
        Command::Collect { scope } => collect(scope),
        Command::IngestFile { path, scope } => run_ingest_file(path, scope),
        Command::Search {
            query,
            scope,
            limit,
        } => run_search(query, scope, limit),
        Command::Backlinks { page, scope } => run_backlinks(page, scope),
        Command::LinkSuggest { scope, limit } => run_link_suggest(scope, limit),
        Command::Research(options) => run_research(options),
        Command::Compile {
            topic,
            outline,
            target_kind,
            target_page,
            write_intent,
            scope,
        } => run_compile(
            topic,
            outline,
            target_kind,
            target_page,
            write_intent,
            scope,
        ),
        Command::Export { scope, command } => run_export(scope, command),
        Command::Audit { scope } => run_audit(scope),
        Command::Lint { scope } => run_lint(scope),
        Command::Health { scope } => run_health(scope),
        Command::Status { scope } => Ok(commands::status::run(scope.identity())),
    }
}

pub fn resolve_research_scope(
    selection: &ScopeSelection,
) -> Result<session::ResearchScope, WikiError> {
    let scope = resolve_command_scope(selection)?;
    Ok(session::ResearchScope::from(&scope))
}

fn run_setup(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let setup = setup::default_setup();
    let objects = setup
        .postgres_objects()
        .map_err(setup_error_to_wiki_error)?;
    let object_payloads = objects
        .iter()
        .map(|object| {
            json!({
                "name": object.name,
                "kind": postgres_object_kind(object.kind),
                "store": "postgres",
            })
        })
        .collect::<Vec<_>>();

    let (status, created, skipped, failed) = if let Some(database_url) = database_url_from_env() {
        let mut client =
            gobby_core::postgres::connect_readwrite(&database_url).map_err(|error| {
                WikiError::Config {
                    detail: format!("failed to connect to PostgreSQL for gwiki setup: {error}"),
                }
            })?;
        let mut ctx = SetupContext {
            pg: Some(&mut client),
            falkor_config: None,
            qdrant_config: None,
            non_interactive: true,
        };
        let report = setup.create(&mut ctx).map_err(setup_error_to_wiki_error)?;
        let status = if report.failed.is_empty() {
            "created"
        } else {
            "failed"
        };
        (status, report.created, report.skipped, report.failed)
    } else {
        ("ready", Vec::new(), Vec::new(), Vec::new())
    };

    Ok(commands::setup::run(
        output_scope,
        status,
        object_payloads,
        created,
        skipped,
        failed,
        setup::SETUP_OWNERSHIP_NOTE,
    ))
}

fn run_index(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let (scope, output_scope, _, store) = indexed_store_for_selection(&selection)?;
    let counts = index_counts(&store);
    Ok(commands::index::run(output_scope, scope.root(), counts))
}

fn run_ingest_file(path: PathBuf, selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    vault::initialize(&scope)?;
    let output_scope = resolved_scope_identity(&scope);
    let mut store = store::MemoryWikiStore::default();
    let result = ingest::file::ingest_path(scope.root(), &mut store, &path, &collect_timestamp())?;
    let counts = index_counts(&store);
    Ok(commands::index::ingest_file(
        &path,
        output_scope,
        &result,
        counts,
    ))
}

fn run_search(
    query: String,
    selection: ScopeSelection,
    limit: usize,
) -> Result<CommandOutcome, WikiError> {
    if let Some(database_url) = database_url_from_env() {
        let scope = resolve_command_scope(&selection)?;
        return run_search_attached(
            &database_url,
            resolved_scope_identity(&scope),
            search_scope_for_resolved(&scope),
            query,
            limit,
        );
    }

    let (_, output_scope, search_scope, store) = indexed_store_for_selection(&selection)?;
    let mut bm25_backend = StoreBm25Backend {
        hits: store_search_hits(&store, &search_scope, &query),
    };
    let mut semantic_backend = UnavailableSemanticBackend;
    run_search_with_backends(
        &mut bm25_backend,
        &mut semantic_backend,
        output_scope,
        search_scope,
        query,
        limit,
    )
}

fn run_search_attached(
    database_url: &str,
    output_scope: ScopeIdentity,
    search_scope: search::SearchScope,
    query: String,
    limit: usize,
) -> Result<CommandOutcome, WikiError> {
    let mut conn = gobby_core::postgres::connect_readonly(database_url).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to PostgreSQL for gwiki search: {error}"),
        }
    })?;
    let (embedding, qdrant) = {
        let mut source = PostgresConfigSource { conn: &mut conn };
        (
            resolve_embedding_config(&mut source),
            resolve_qdrant_config(&mut source),
        )
    };
    let mut bm25_backend = search::bm25::PostgresBm25Backend::new(&mut conn);
    let mut semantic_backend = search::semantic::GobbySemanticBackend::new(
        embedding,
        qdrant,
        search::semantic::OpenAiEmbeddingBackend,
        search::semantic::GobbyQdrantBackend,
    );
    run_search_with_backends(
        &mut bm25_backend,
        &mut semantic_backend,
        output_scope,
        search_scope,
        query,
        limit,
    )
}

fn run_search_with_backends<B, S>(
    bm25_backend: &mut B,
    semantic_backend: &mut S,
    output_scope: ScopeIdentity,
    search_scope: search::SearchScope,
    query: String,
    limit: usize,
) -> Result<CommandOutcome, WikiError>
where
    B: search::bm25::Bm25SearchBackend,
    S: search::semantic::SemanticSearchBackend,
{
    let response = search::search(
        bm25_backend,
        semantic_backend,
        search::SearchRequest {
            query: query.clone(),
            scope: search_scope,
            limit,
            include_semantic: true,
        },
    )
    .map_err(search_error_to_wiki_error)?;
    let results = response
        .results
        .into_iter()
        .map(|result| output::SearchResultOutput {
            title: result.title,
            wiki_page: result.path,
            source_path: result.source_path,
            snippet: result.snippet,
            score: result.score,
            sources: result
                .sources
                .iter()
                .map(|source| source.as_str().to_string())
                .collect(),
            explanations: result
                .explanations
                .iter()
                .map(|explanation| output::SearchSourceExplanationOutput {
                    source: explanation.source.as_str().to_string(),
                    rank: explanation.rank,
                    score: explanation.score,
                })
                .collect(),
        })
        .collect::<Vec<_>>();
    let degradations = response
        .degradations
        .iter()
        .map(degradation_label)
        .collect::<Vec<_>>();
    let output = output::SearchOutput::new(
        output_scope.clone(),
        query.clone(),
        limit,
        results,
        degradations,
    );
    commands::search::run(output)
}

fn run_backlinks(page: String, selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let (_, output_scope, search_scope, store) = indexed_store_for_selection(&selection)?;
    let graph = memory_graph_from_store(&store, &search_scope);
    let backlinks = graph.backlinks(&search_scope, PathBuf::from(&page));
    Ok(commands::backlinks::run(&page, output_scope, &backlinks))
}

fn run_link_suggest(selection: ScopeSelection, limit: usize) -> Result<CommandOutcome, WikiError> {
    let (_, output_scope, search_scope, store) = indexed_store_for_selection(&selection)?;
    let graph = memory_graph_from_store(&store, &search_scope);
    let suggestions = graph.link_suggestions(&search_scope, limit);
    Ok(commands::backlinks::link_suggest(
        output_scope,
        limit,
        &suggestions,
    ))
}

fn collect(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let cwd = std::env::current_dir().map_err(|error| WikiError::Io {
        action: "read current directory",
        path: None,
        source: error.to_string(),
    })?;
    let scope = scope::resolve(&selection, &cwd)?;

    vault::initialize(&scope)?;
    let output_scope = resolved_scope_identity(&scope);
    let report = collect::collect_inbox(scope.root(), &collect_timestamp())?;
    Ok(commands::collect::run(output_scope, scope.root(), report))
}

fn run_research(options: research::ResearchOptions) -> Result<CommandOutcome, WikiError> {
    let outcome = research::run(options)?;
    let session = outcome.session;
    let message = outcome.message;
    let output_scope = research_scope_identity(&session.scope);
    let payload = serde_json::json!({
        "command": "research",
        "scope": output_scope,
        "status": "checkpointed",
        "session": session,
    });

    Ok(commands::scoped_outcome(
        "research",
        &output_scope,
        payload,
        message,
    ))
}

fn run_compile(
    topic: Option<String>,
    outline: Vec<String>,
    target_kind: synthesis::ArticleKind,
    target_page: Option<PathBuf>,
    write_intent: bool,
    scope: ScopeSelection,
) -> Result<CommandOutcome, WikiError> {
    let resolved_scope = resolve_command_scope(&scope)?;
    let research_scope = session::ResearchScope::from(&resolved_scope);
    let mut session = session::ResearchSession::load_checkpoint(research_scope.root())?;
    let topic = topic.unwrap_or_else(|| {
        session
            .compile_state
            .as_ref()
            .map(|state| state.topic.clone())
            .unwrap_or_else(|| session.question.clone())
    });
    let target_page = target_page.map(|path| {
        if path.is_absolute() {
            path
        } else {
            research_scope.root().join(path)
        }
    });
    let daemon_report = daemon::probe_daemon_capabilities();
    let outcome = compile::compile_to_wiki_with_options(
        &mut session,
        compile::CompileRequest {
            topic,
            outline,
            target_page,
            write_intent,
        },
        compile::WikiCompileOptions {
            target_kind,
            daemon_synthesis_available: daemon_report.synthesis.available,
        },
    )?;
    let output_scope = resolved_scope_identity(&resolved_scope);
    let payload = serde_json::json!({
        "command": "compile",
        "scope": output_scope,
        "status": "compiled",
        "daemon_synthesis_available": daemon_report.synthesis.available,
        "article_path": outcome.article_path,
        "source_paths": outcome.source_paths,
        "index_path": outcome.index_path,
        "handoff_id": outcome.handoff_id,
        "page_writes": outcome.page_writes,
        "prompt": outcome.prompt,
    });
    let text = format!(
        "Compiled wiki article\nScope: {output_scope}\nArticle: {}",
        outcome.article_path.display()
    );
    Ok(commands::scoped_outcome(
        "compile",
        &output_scope,
        payload,
        text,
    ))
}

fn run_export(
    selection: ScopeSelection,
    command: exports::ExportCommand,
) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let artifacts = exports::run(scope.root(), command)?;
    let output = exports::ExportOutput::new(output_scope.clone(), artifacts);
    let payload = serde_json::to_value(&output).map_err(|error| WikiError::Json {
        action: "serialize export output",
        path: None,
        source: error.to_string(),
    })?;
    let paths = output
        .artifacts
        .iter()
        .map(|artifact| artifact.path.display().to_string())
        .collect::<Vec<_>>()
        .join(", ");
    let text = format!("Exported wiki artifacts\nScope: {output_scope}\nArtifacts: {paths}");
    Ok(commands::scoped_outcome(
        "export",
        &output_scope,
        payload,
        text,
    ))
}

fn run_audit(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let report = audit::run(scope.root(), output_scope.clone())?;
    let payload = serde_json::to_value(&report).map_err(|error| WikiError::Json {
        action: "serialize audit report",
        path: None,
        source: error.to_string(),
    })?;
    Ok(commands::scoped_outcome(
        "audit",
        &output_scope,
        payload,
        audit::render_text(&report),
    ))
}

fn run_lint(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let report = lint::run(scope.root(), output_scope.clone())?;
    let payload = serde_json::to_value(&report).map_err(|error| WikiError::Json {
        action: "serialize lint report",
        path: None,
        source: error.to_string(),
    })?;
    Ok(commands::scoped_outcome(
        "lint",
        &output_scope,
        payload,
        lint::render_text(&report),
    ))
}

fn run_health(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let report = health::run(scope.root(), output_scope.clone())?;
    let payload = serde_json::to_value(&report).map_err(|error| WikiError::Json {
        action: "serialize health report",
        path: None,
        source: error.to_string(),
    })?;
    Ok(commands::scoped_outcome(
        "health",
        &output_scope,
        payload,
        health::render_text(&report),
    ))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct IndexCounts {
    documents: usize,
    chunks: usize,
    links: usize,
    sources: usize,
    ingestions: usize,
}

struct StoreBm25Backend {
    hits: Vec<search::WikiSearchResult>,
}

impl search::bm25::Bm25SearchBackend for StoreBm25Backend {
    fn search_bm25(
        &mut self,
        _request: &search::bm25::Bm25SearchRequest,
    ) -> Result<Vec<search::WikiSearchResult>, search::SearchError> {
        Ok(self.hits.clone())
    }
}

struct UnavailableSemanticBackend;

impl search::semantic::SemanticSearchBackend for UnavailableSemanticBackend {
    fn search_semantic(
        &mut self,
        _request: search::semantic::SemanticSearchRequest,
    ) -> Result<search::semantic::SemanticSearchOutcome, search::SearchError> {
        Ok(search::semantic::SemanticSearchOutcome {
            hits: Vec::new(),
            degradation: Some(DegradationKind::ServiceUnavailable {
                service: "qdrant".to_string(),
                state: ServiceState::NotConfigured,
            }),
        })
    }
}

struct PostgresConfigSource<'a> {
    conn: &'a mut postgres::Client,
}

impl ConfigSource for PostgresConfigSource<'_> {
    fn config_value(&mut self, key: &str) -> Option<String> {
        gobby_core::postgres::read_config_value(self.conn, key)
            .ok()
            .flatten()
            .and_then(|raw| gobby_core::config::decode_config_value(&raw))
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        if value.contains("$secret:") {
            anyhow::bail!("secret resolution is not available in gwiki search config");
        }

        gobby_core::config::resolve_env_pattern(value)?
            .ok_or_else(|| anyhow::anyhow!("unresolved environment pattern in `{value}`"))
    }
}

fn indexed_store_for_selection(
    selection: &ScopeSelection,
) -> Result<
    (
        scope::ResolvedScope,
        ScopeIdentity,
        search::SearchScope,
        store::MemoryWikiStore,
    ),
    WikiError,
> {
    let scope = resolve_command_scope(selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let search_scope = search_scope_for_resolved(&scope);
    let mut store = store::MemoryWikiStore::default();
    if scope.root().is_dir() {
        indexer::index_vault(scope.root(), &mut store).map_err(index_error_to_wiki_error)?;
    }

    Ok((scope, output_scope, search_scope, store))
}

fn search_scope_for_resolved(scope: &scope::ResolvedScope) -> search::SearchScope {
    if let Some(topic) = scope.topic_name() {
        return search::SearchScope::topic(topic);
    }
    if let Some(project_id) = scope.project_id() {
        return search::SearchScope::project(project_id);
    }
    search::SearchScope::project("current")
}

fn index_counts(store: &store::MemoryWikiStore) -> IndexCounts {
    IndexCounts {
        documents: store.documents.len(),
        chunks: store.chunks.values().map(Vec::len).sum(),
        links: store.links.values().map(Vec::len).sum(),
        sources: store.sources.len(),
        ingestions: store.ingestions.len(),
    }
}

fn store_search_hits(
    store: &store::MemoryWikiStore,
    scope: &search::SearchScope,
    query: &str,
) -> Vec<search::WikiSearchResult> {
    let tokens = query_tokens(query);
    if tokens.is_empty() {
        return Vec::new();
    }

    let mut ranked = Vec::new();
    for document in store.documents.values() {
        if !search::bm25::is_keyword_searchable_path(&document.path.to_string_lossy()) {
            continue;
        }

        let document_score = keyword_score(
            &format!(
                "{}\n{}",
                document.title.as_deref().unwrap_or_default(),
                document.body
            ),
            &tokens,
        );
        if document_score > 0 {
            ranked.push((
                document_score,
                search::WikiSearchResult {
                    id: format!("document:{}", display_path(&document.path)),
                    title: document.title.clone(),
                    scope: scope.clone(),
                    path: document.path.clone(),
                    source_path: document.path.clone(),
                    hit_kind: search::SearchHitKind::Document,
                    snippet: snippet_from_text(&document.body),
                    score: document_score as f64,
                    sources: vec![search::SearchSource::Bm25],
                    explanations: Vec::new(),
                    chunk: None,
                    provenance: search::SearchProvenance {
                        document_path: document.path.clone(),
                        source_path: document.path.clone(),
                        source_kind: document_kind_name(document.kind).to_string(),
                        content_hash: Some(document.content_hash.clone()),
                    },
                },
            ));
        }

        if let Some(chunks) = store.chunks.get(&document.path) {
            for chunk in chunks {
                let chunk_score = keyword_score(
                    &format!(
                        "{}\n{}",
                        chunk.heading.as_deref().unwrap_or_default(),
                        chunk.content
                    ),
                    &tokens,
                );
                if chunk_score == 0 {
                    continue;
                }
                ranked.push((
                    chunk_score,
                    search::WikiSearchResult {
                        id: format!("chunk:{}:{}", display_path(&chunk.path), chunk.chunk_index),
                        title: document.title.clone(),
                        scope: scope.clone(),
                        path: chunk.path.clone(),
                        source_path: document.path.clone(),
                        hit_kind: search::SearchHitKind::Chunk,
                        snippet: snippet_from_text(&chunk.content),
                        score: chunk_score as f64,
                        sources: vec![search::SearchSource::Bm25],
                        explanations: Vec::new(),
                        chunk: Some(search::ChunkProvenance {
                            chunk_index: chunk.chunk_index,
                            byte_start: chunk.byte_start,
                            byte_end: chunk.byte_end,
                            heading: chunk.heading.clone(),
                        }),
                        provenance: search::SearchProvenance {
                            document_path: document.path.clone(),
                            source_path: document.path.clone(),
                            source_kind: document_kind_name(document.kind).to_string(),
                            content_hash: Some(document.content_hash.clone()),
                        },
                    },
                ));
            }
        }
    }

    ranked.sort_by(|(left_score, left), (right_score, right)| {
        right_score
            .cmp(left_score)
            .then_with(|| left.path.cmp(&right.path))
            .then_with(|| left.id.cmp(&right.id))
    });
    ranked
        .into_iter()
        .map(|(rank, mut result)| {
            result.score = rank as f64;
            result
        })
        .collect()
}

fn memory_graph_from_store(
    store: &store::MemoryWikiStore,
    scope: &search::SearchScope,
) -> graph::MemoryWikiGraph {
    let documents = store
        .documents
        .values()
        .map(|document| graph::WikiGraphDocument {
            scope: scope.clone(),
            path: document.path.clone(),
            title: document.title.clone(),
        })
        .collect::<Vec<_>>();
    let links = store
        .links
        .values()
        .flat_map(|links| links.iter())
        .filter_map(|link| {
            resolve_graph_target(&link.target, store).map(|target| graph::WikiGraphLink {
                scope: scope.clone(),
                source_path: link.path.clone(),
                raw_target: link.target.clone(),
                target,
            })
        })
        .collect::<Vec<_>>();
    let sources = store
        .sources
        .values()
        .map(|source| graph::WikiGraphSource {
            scope: scope.clone(),
            source_path: source.path.clone(),
            document_path: source.path.clone(),
        })
        .collect::<Vec<_>>();

    let mut graph = graph::MemoryWikiGraph::default();
    graph.replace_facts(graph::WikiGraphFacts {
        documents,
        links,
        sources,
    });
    graph
}

fn resolve_graph_target(
    raw_target: &str,
    store: &store::MemoryWikiStore,
) -> Option<graph::WikiGraphLinkTarget> {
    let trimmed = raw_target.trim();
    if trimmed.is_empty()
        || trimmed.starts_with("http://")
        || trimmed.starts_with("https://")
        || trimmed.starts_with("mailto:")
    {
        return None;
    }

    let normalized = trimmed
        .split('#')
        .next()
        .unwrap_or_default()
        .trim()
        .trim_start_matches('/')
        .replace('\\', "/");
    if normalized.is_empty() {
        return None;
    }

    let direct = PathBuf::from(&normalized);
    if store.documents.contains_key(&direct) {
        return Some(graph::WikiGraphLinkTarget::Resolved(direct));
    }

    let target_slug = slugify(normalized.trim_end_matches(".md"));
    for document in store.documents.values() {
        let file_slug = document
            .path
            .file_stem()
            .and_then(|value| value.to_str())
            .map(slugify);
        let title_slug = document.title.as_deref().map(slugify);
        if file_slug.as_deref() == Some(target_slug.as_str())
            || title_slug.as_deref() == Some(target_slug.as_str())
        {
            return Some(graph::WikiGraphLinkTarget::Resolved(document.path.clone()));
        }
    }

    Some(graph::WikiGraphLinkTarget::Unresolved(trimmed.to_string()))
}

fn query_tokens(query: &str) -> Vec<String> {
    query
        .split(|ch: char| !ch.is_alphanumeric())
        .filter(|token| !token.is_empty())
        .map(str::to_ascii_lowercase)
        .collect()
}

fn keyword_score(text: &str, tokens: &[String]) -> usize {
    let haystack = text.to_ascii_lowercase();
    tokens
        .iter()
        .map(|token| haystack.matches(token).count())
        .sum()
}

fn snippet_from_text(text: &str) -> String {
    let snippet = text
        .lines()
        .find(|line| !line.trim().is_empty())
        .unwrap_or_default()
        .trim();
    if snippet.len() <= 240 {
        return snippet.to_string();
    }

    format!("{}...", &snippet[..240])
}

fn degradation_label(degradation: &DegradationKind) -> String {
    match degradation {
        DegradationKind::ServiceUnavailable { service, state } => match state {
            ServiceState::Available => format!("{service}_available"),
            ServiceState::NotConfigured => format!("{service}_not_configured"),
            ServiceState::Unreachable { .. } => format!("{service}_unreachable"),
        },
        DegradationKind::PartialSearch { .. } => "partial_search".to_string(),
        DegradationKind::StaleIndex { .. } => "stale_index".to_string(),
        DegradationKind::SkippedArtifacts { .. } => "skipped_artifacts".to_string(),
    }
}

fn document_kind_name(kind: store::WikiDocumentKind) -> &'static str {
    match kind {
        store::WikiDocumentKind::SourceCatalog => "source_catalog",
        store::WikiDocumentKind::SourceNote => "source_note",
        store::WikiDocumentKind::Concept => "concept",
        store::WikiDocumentKind::Topic => "topic",
    }
}

fn postgres_object_kind(kind: setup::GwikiPostgresObjectKind) -> &'static str {
    match kind {
        setup::GwikiPostgresObjectKind::Table => "table",
        setup::GwikiPostgresObjectKind::Index => "index",
    }
}

fn setup_error_to_wiki_error(error: SetupError) -> WikiError {
    WikiError::Config {
        detail: format!("gwiki setup failed: {error}"),
    }
}

fn index_error_to_wiki_error(error: indexer::IndexError) -> WikiError {
    WikiError::InvalidInput {
        field: "vault_root",
        message: error.to_string(),
    }
}

fn search_error_to_wiki_error(error: search::SearchError) -> WikiError {
    WikiError::InvalidInput {
        field: "query",
        message: error.to_string(),
    }
}

fn database_url_from_env() -> Option<String> {
    [
        "GWIKI_DATABASE_URL",
        "GOBBY_POSTGRES_DSN",
        "GCODE_DATABASE_URL",
    ]
    .into_iter()
    .find_map(|name| {
        std::env::var(name)
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
    })
}

fn display_path(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

fn slugify(value: &str) -> String {
    let mut slug = String::new();
    let mut last_was_dash = false;
    for ch in value.chars().flat_map(char::to_lowercase) {
        if ch.is_ascii_alphanumeric() {
            slug.push(ch);
            last_was_dash = false;
        } else if !last_was_dash && !slug.is_empty() {
            slug.push('-');
            last_was_dash = true;
        }
    }
    slug.trim_matches('-').to_string()
}

fn resolve_command_scope(selection: &ScopeSelection) -> Result<scope::ResolvedScope, WikiError> {
    let cwd = std::env::current_dir().map_err(|error| WikiError::Io {
        action: "read current directory",
        path: None,
        source: error.to_string(),
    })?;
    scope::resolve(selection, &cwd)
}

fn research_scope_identity(scope: &session::ResearchScope) -> ScopeIdentity {
    match scope {
        session::ResearchScope::Project { .. } => ScopeIdentity::project("current"),
        session::ResearchScope::Topic { name, .. } => ScopeIdentity::topic(name.clone()),
    }
}

fn init(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let cwd = std::env::current_dir().map_err(|error| WikiError::Io {
        action: "read current directory",
        path: None,
        source: error.to_string(),
    })?;
    let scope = scope::resolve(&selection, &cwd)?;

    vault::initialize(&scope)?;
    registry::register_scope(scope.registry_path(), &scope)?;

    let output_scope = resolved_scope_identity(&scope);
    let required_paths = vault::required_paths();
    Ok(commands::init::run(
        output_scope,
        scope.root(),
        &required_paths,
    ))
}

fn resolved_scope_identity(scope: &scope::ResolvedScope) -> ScopeIdentity {
    if let Some(topic) = scope.topic_name() {
        return ScopeIdentity::topic(topic);
    }

    if let Some(project_id) = scope.project_id() {
        return ScopeIdentity::project(project_id);
    }

    ScopeIdentity::project("current")
}

fn collect_timestamp() -> String {
    let millis = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or_default();
    format!("unix-ms:{millis}")
}

#[cfg(test)]
mod lib {
    mod tests {
        use crate::ScopeSelection;

        #[test]
        fn scope_selection_constructors_express_allowed_states() {
            let global = ScopeSelection::global();
            assert!(!global.is_project());
            assert_eq!(global.topic_name(), None);
            assert_eq!(ScopeSelection::default(), global);

            let project = ScopeSelection::project();
            assert!(project.is_project());
            assert_eq!(project.topic_name(), None);

            let topic = ScopeSelection::topic("ops");
            assert!(!topic.is_project());
            assert_eq!(topic.topic_name(), Some("ops"));
        }

        #[test]
        fn crate_has_no_gcode_dependency() {
            let manifest =
                std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/Cargo.toml"))
                    .expect("manifest is readable");
            let manifest: toml::Value = toml::from_str(&manifest).expect("manifest is valid TOML");
            let dependencies = manifest
                .get("dependencies")
                .and_then(toml::Value::as_table)
                .expect("manifest has dependencies table");

            assert!(
                dependencies.contains_key("gobby-core"),
                "gobby-wiki must depend on gobby-core"
            );
            assert!(
                !dependencies.contains_key("gobby-code"),
                "gobby-wiki must not depend on gobby-code"
            );
        }
    }
}
