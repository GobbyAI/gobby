use gobby_core::config::{resolve_embedding_config, resolve_qdrant_config};

use crate::error::search_error_to_wiki_error;
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
    let mut bm25_backend = search_support::StoreBm25Backend {
        hits: search_support::store_search_hits(&store, &search_scope, &query),
    };
    let mut semantic_backend = search_support::UnavailableSemanticBackend;
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
    search_scope: wiki_search::SearchScope,
    query: String,
    limit: usize,
) -> Result<CommandOutcome, WikiError> {
    let mut conn = gobby_core::postgres::connect_readonly(database_url).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to PostgreSQL for gwiki search: {error}"),
        }
    })?;
    let (embedding, qdrant) = {
        let mut source = search_support::PostgresConfigSource { conn: &mut conn };
        (
            resolve_embedding_config(&mut source),
            resolve_qdrant_config(&mut source),
        )
    };
    let mut bm25_backend = wiki_search::bm25::PostgresBm25Backend::new(&mut conn);
    let mut semantic_backend = wiki_search::semantic::GobbySemanticBackend::new(
        embedding,
        qdrant,
        wiki_search::semantic::OpenAiEmbeddingBackend,
        wiki_search::semantic::GobbyQdrantBackend,
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
    search_scope: wiki_search::SearchScope,
    query: String,
    limit: usize,
) -> Result<CommandOutcome, WikiError>
where
    B: wiki_search::bm25::Bm25SearchBackend,
    S: wiki_search::semantic::SemanticSearchBackend,
{
    let response = wiki_search::search(
        bm25_backend,
        semantic_backend,
        wiki_search::SearchRequest {
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
    let output = SearchOutput::new(
        output_scope.clone(),
        query.clone(),
        limit,
        results,
        degradations,
    );
    render(output)
}

fn render(output: SearchOutput) -> Result<CommandOutcome, WikiError> {
    let scope = output.scope.clone();
    let query = output.query.clone();
    let text = render_text(&query, &scope, &output.results);
    let payload = serde_json::to_value(&output).map_err(|error| WikiError::Json {
        action: "serialize search output",
        path: None,
        source: error.to_string(),
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
