#[cfg(feature = "ai")]
use gobby_core::ai::effective_route;
use gobby_core::ai_context::{AiConfigSource, AiContext};
use gobby_core::config::{
    AiCapability, AiRouting, resolve_embedding_config, resolve_falkordb_config,
    resolve_qdrant_config,
};

use crate::output::{SearchOutput, SearchResultOutput};
use crate::search as wiki_search;
use crate::support::env::database_url_from_env;
use crate::support::scope::{
    indexed_store_for_selection, resolve_command_scope, resolved_scope_identity,
    search_scope_for_resolved,
};
use crate::support::search as search_support;
use crate::support::text::degradation_label;
use crate::{CommandOutcome, ScopeIdentity, ScopeSelection, WikiError};

pub(crate) fn execute(
    query: String,
    selection: ScopeSelection,
    limit: usize,
    include_semantic: bool,
) -> Result<CommandOutcome, WikiError> {
    render(retrieve(query, selection, limit, include_semantic)?)
}

pub(crate) fn retrieve(
    query: String,
    selection: ScopeSelection,
    limit: usize,
    include_semantic: bool,
) -> Result<SearchOutput, WikiError> {
    if let Some(database_url) = database_url_from_env() {
        let scope = resolve_command_scope(&selection)?;
        return run_search_attached(
            &database_url,
            resolved_scope_identity(&scope),
            search_scope_for_resolved(&scope),
            query,
            limit,
            include_semantic,
        );
    }

    let (_, output_scope, search_scope, store) = indexed_store_for_selection(&selection)?;
    let mut bm25_backend = search_support::StoreBm25Backend {
        hits: search_support::store_search_hits(&store, &search_scope, &query).into(),
    };
    let mut semantic_backend = search_support::UnavailableSemanticBackend;
    let graph = crate::support::graph::memory_graph_from_store(&store, &search_scope);
    let mut graph_backend = wiki_search::graph_boost::MemoryGraphBoostBackend::new(graph);
    run_search_with_backends(
        &mut bm25_backend,
        &mut semantic_backend,
        &mut graph_backend,
        SearchExecutionInput {
            output_scope,
            search_scope,
            query,
            limit,
            include_semantic,
        },
    )
}

fn run_search_attached(
    database_url: &str,
    output_scope: ScopeIdentity,
    search_scope: wiki_search::SearchScope,
    query: String,
    limit: usize,
    include_semantic: bool,
) -> Result<SearchOutput, WikiError> {
    let mut conn = gobby_core::postgres::connect_readonly(database_url).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to PostgreSQL for gwiki search: {error}"),
        }
    })?;
    let gobby_home = gobby_home()?;
    let embedding = {
        let primary = search_support::PostgresConfigSource { conn: &mut conn };
        let mut source = AiConfigSource::with_primary_from_gobby_home(primary, &gobby_home)
            .map_err(|error| WikiError::Config {
                detail: format!("failed to resolve AI config for gwiki search: {error}"),
            })?;
        let ai_context = AiContext::resolve(None, &mut source);
        resolve_semantic_embedding(&ai_context, &mut source)
    };
    let qdrant = {
        let primary = search_support::PostgresConfigSource { conn: &mut conn };
        let mut source = AiConfigSource::with_primary_from_gobby_home(primary, &gobby_home)
            .map_err(|error| WikiError::Config {
                detail: format!("failed to resolve Qdrant config for gwiki search: {error}"),
            })?;
        resolve_qdrant_config(&mut source)
    };
    let falkor = {
        let primary = search_support::PostgresConfigSource { conn: &mut conn };
        let mut source = AiConfigSource::with_primary_from_gobby_home(primary, &gobby_home)
            .map_err(|error| WikiError::Config {
                detail: format!("failed to resolve FalkorDB config for gwiki search: {error}"),
            })?;
        resolve_falkordb_config(&mut source)
    };
    let embedding = embedding.ok_or_else(|| required_search_config("embedding endpoint"))?;
    let qdrant = qdrant
        .filter(|config| {
            config
                .url
                .as_deref()
                .is_some_and(|url| !url.trim().is_empty())
        })
        .ok_or_else(|| required_search_config("Qdrant"))?;
    let falkor = falkor.ok_or_else(|| required_search_config("FalkorDB"))?;
    let mut graph_backend = wiki_search::graph_boost::FalkorGraphBoostBackend::new(&falkor)?;
    let mut bm25_backend = wiki_search::bm25::PostgresBm25Backend::new(&mut conn);
    let mut semantic_backend = wiki_search::semantic::GobbySemanticBackend::new(
        Some(embedding),
        Some(qdrant),
        wiki_search::semantic::OpenAiEmbeddingBackend::new(),
        wiki_search::semantic::GobbyQdrantBackend,
    );
    run_search_with_backends(
        &mut bm25_backend,
        &mut semantic_backend,
        &mut graph_backend,
        SearchExecutionInput {
            output_scope,
            search_scope,
            query,
            limit,
            include_semantic,
        },
    )
}

fn required_search_config(service: &'static str) -> WikiError {
    WikiError::Config {
        detail: format!(
            "gwiki search requires {service}; run `gwiki setup --standalone` or attach to Gobby's full datastore stack"
        ),
    }
}

fn resolve_semantic_embedding(
    context: &AiContext,
    source: &mut impl gobby_core::config::ConfigSource,
) -> Option<wiki_search::semantic::SemanticEmbedding> {
    match effective_embedding_route(context) {
        AiRouting::Off => None,
        AiRouting::Daemon => {
            #[cfg(feature = "ai")]
            {
                Some(wiki_search::semantic::SemanticEmbedding::Daemon(Box::new(
                    context.clone(),
                )))
            }
            #[cfg(not(feature = "ai"))]
            {
                None
            }
        }
        AiRouting::Direct => {
            resolve_embedding_config(source).map(wiki_search::semantic::SemanticEmbedding::Direct)
        }
        AiRouting::Auto => {
            #[cfg(feature = "ai")]
            {
                Some(wiki_search::semantic::SemanticEmbedding::Daemon(Box::new(
                    context.clone(),
                )))
            }
            #[cfg(not(feature = "ai"))]
            {
                resolve_embedding_config(source)
                    .map(wiki_search::semantic::SemanticEmbedding::Direct)
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
                eprintln!(
                    "warning: gwiki was built without ai support; daemon-backed embeddings are disabled"
                );
                AiRouting::Off
            }
            AiRouting::Auto => {
                eprintln!(
                    "warning: gwiki was built without ai support; auto embedding route cannot use the daemon"
                );
                AiRouting::Auto
            }
        }
    }
}

fn gobby_home() -> Result<std::path::PathBuf, WikiError> {
    gobby_core::gobby_home().map_err(|error| WikiError::Config {
        detail: format!("failed to resolve Gobby home for gwiki search config: {error}"),
    })
}

struct SearchExecutionInput {
    output_scope: ScopeIdentity,
    search_scope: wiki_search::SearchScope,
    query: String,
    limit: usize,
    include_semantic: bool,
}

fn run_search_with_backends<B, S, G>(
    bm25_backend: &mut B,
    semantic_backend: &mut S,
    graph_backend: &mut G,
    input: SearchExecutionInput,
) -> Result<SearchOutput, WikiError>
where
    B: wiki_search::bm25::Bm25SearchBackend,
    S: wiki_search::semantic::SemanticSearchBackend,
    G: wiki_search::graph_boost::GraphBoostBackend,
{
    let response = wiki_search::search(
        bm25_backend,
        semantic_backend,
        graph_backend,
        wiki_search::SearchRequest {
            query: input.query.clone(),
            scope: input.search_scope,
            limit: input.limit,
            include_semantic: input.include_semantic,
        },
    )?;
    let results = response
        .results
        .into_iter()
        .map(|result| SearchResultOutput {
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
                .map(|explanation| crate::output::SearchSourceExplanationOutput {
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
    Ok(SearchOutput::new(
        input.output_scope,
        input.query,
        input.limit,
        results,
        degradations,
    ))
}

fn render(output: SearchOutput) -> Result<CommandOutcome, WikiError> {
    let scope = output.scope.clone();
    let query = output.query.clone();
    let text = render_text(&query, &scope, &output.results);
    let payload = serde_json::to_value(&output).map_err(|error| WikiError::Json {
        action: "serialize search output",
        path: None,
        source: error,
    })?;

    Ok(super::scoped_outcome("search", &scope, payload, text))
}

fn render_text(query: &str, scope: &ScopeIdentity, results: &[SearchResultOutput]) -> String {
    let mut text = format!("Search results for \"{query}\"\nScope: {scope}\n");
    if results.is_empty() {
        text.push_str("No results");
        return text;
    }

    for result in results {
        text.push_str("- ");
        text.push_str(&result.wiki_page.display().to_string());
        if let Some(title) = &result.title {
            text.push_str(" | ");
            text.push_str(title);
        }
        text.push_str(" | ");
        text.push_str(&result.snippet);
        text.push('\n');
    }
    text
}
