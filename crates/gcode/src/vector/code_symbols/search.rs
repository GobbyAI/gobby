use std::collections::HashMap;

use crate::config::{CODE_SYMBOL_COLLECTION_PREFIX, Context, ProjectIndexScope};

use super::embedding::{embed_query_with_source, embedding_source_from_context};
use super::qdrant::{collection_name, vector_search};
use super::types::{CodeSymbolVectorSearchHit, CodeSymbolVectorSearchRequest};

type RankedHit = (String, f64);
type ProjectSearchFailure = (String, SearchError);

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SearchError {
    MissingQdrantConfig,
    MissingEmbeddingConfig,
    QueryEmbeddingFailed,
    InvalidCollectionName(gobby_core::qdrant::CollectionNameError),
    VectorSearch(String),
}

impl std::fmt::Display for SearchError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::MissingQdrantConfig => write!(f, "Qdrant config is missing"),
            Self::MissingEmbeddingConfig => write!(f, "embedding config is missing"),
            Self::QueryEmbeddingFailed => write!(f, "query embedding failed"),
            Self::InvalidCollectionName(error) => write!(f, "{error}"),
            Self::VectorSearch(error) => write!(f, "semantic vector search failed: {error}"),
        }
    }
}

impl std::error::Error for SearchError {}

pub fn search_code_symbols(
    ctx: &Context,
    request: &CodeSymbolVectorSearchRequest,
) -> Result<Vec<CodeSymbolVectorSearchHit>, SearchError> {
    let qdrant_config = match &ctx.qdrant {
        Some(config) => config,
        None => return Err(SearchError::MissingQdrantConfig),
    };

    let embedding_source = match embedding_source_from_context(ctx) {
        Some(source) => source,
        None => return Err(SearchError::MissingEmbeddingConfig),
    };

    let embedding = match embed_query_with_source(&embedding_source, &request.query) {
        Some(embedding) => embedding,
        None => return Err(SearchError::QueryEmbeddingFailed),
    };

    let collection = collection_name(&request.collection_prefix, &request.project_id)
        .map_err(SearchError::InvalidCollectionName)?;
    match vector_search(qdrant_config, &collection, &embedding, request.limit) {
        Ok(hits) => Ok(hits
            .into_iter()
            .map(|(symbol_id, score)| CodeSymbolVectorSearchHit { symbol_id, score })
            .collect()),
        Err(error) => Err(SearchError::VectorSearch(error.to_string())),
    }
}

/// Semantic search is a full-stack ranking signal. Returning an empty result on
/// transport/config errors lets degraded hybrid-search callers keep lexical and
/// graph sources instead of failing the whole user query.
pub fn semantic_search(ctx: &Context, query: &str, limit: usize) -> Vec<(String, f64)> {
    let Some(qdrant_config) = &ctx.qdrant else {
        log::warn!(
            "semantic vector search skipped: {}",
            SearchError::MissingQdrantConfig
        );
        return Vec::new();
    };
    let Some(embedding_source) = embedding_source_from_context(ctx) else {
        log::warn!(
            "semantic vector search skipped: {}",
            SearchError::MissingEmbeddingConfig
        );
        return Vec::new();
    };
    let Some(embedding) = embed_query_with_source(&embedding_source, query) else {
        log::warn!(
            "semantic vector search skipped: {}",
            SearchError::QueryEmbeddingFailed
        );
        return Vec::new();
    };

    let project_ids = visible_vector_project_ids(ctx);
    let (hits, failures) = search_visible_projects(&project_ids, limit, |project_id| {
        let collection = collection_name(CODE_SYMBOL_COLLECTION_PREFIX, project_id)
            .map_err(SearchError::InvalidCollectionName)?;
        vector_search(qdrant_config, &collection, &embedding, limit)
            .map_err(|error| SearchError::VectorSearch(error.to_string()))
    });

    for (project_id, error) in failures {
        if hits.is_empty() || project_id != ctx.project_id {
            log::warn!("semantic vector search degraded for project {project_id}: {error}");
        } else {
            log::debug!("overlay semantic vectors unavailable for project {project_id}: {error}");
        }
    }

    hits
}

fn visible_vector_project_ids(ctx: &Context) -> Vec<&str> {
    match &ctx.index_scope {
        ProjectIndexScope::Single => vec![ctx.project_id.as_str()],
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } => vec![overlay_project_id.as_str(), parent_project_id.as_str()],
    }
}

fn search_visible_projects<F>(
    project_ids: &[&str],
    limit: usize,
    mut search: F,
) -> (Vec<RankedHit>, Vec<ProjectSearchFailure>)
where
    F: FnMut(&str) -> Result<Vec<RankedHit>, SearchError>,
{
    let mut scores_by_symbol = HashMap::<String, f64>::new();
    let mut failures = Vec::new();

    for project_id in project_ids {
        match search(project_id) {
            Ok(hits) => {
                for (symbol_id, score) in hits {
                    scores_by_symbol
                        .entry(symbol_id)
                        .and_modify(|existing| *existing = existing.max(score))
                        .or_insert(score);
                }
            }
            Err(error) => failures.push(((*project_id).to_string(), error)),
        }
    }

    let mut hits = scores_by_symbol.into_iter().collect::<Vec<_>>();
    hits.sort_by(|(left_id, left_score), (right_id, right_score)| {
        right_score
            .total_cmp(left_score)
            .then_with(|| left_id.cmp(right_id))
    });
    hits.truncate(limit);
    (hits, failures)
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use super::*;

    #[test]
    fn overlay_vector_projects_include_overlay_then_parent() {
        let mut ctx = test_context();
        ctx.index_scope = ProjectIndexScope::Overlay {
            overlay_project_id: "overlay".to_string(),
            overlay_root: PathBuf::from("/tmp/overlay"),
            parent_project_id: "parent".to_string(),
            parent_root: PathBuf::from("/tmp/parent"),
        };

        assert_eq!(visible_vector_project_ids(&ctx), vec!["overlay", "parent"]);
    }

    #[test]
    fn single_project_search_keeps_one_vector_scope() {
        let ctx = test_context();

        assert_eq!(visible_vector_project_ids(&ctx), vec!["single"]);
    }

    #[test]
    fn overlay_search_uses_parent_hits_when_overlay_collection_is_missing() {
        let project_ids = ["overlay", "parent"];

        let (hits, failures) = search_visible_projects(&project_ids, 10, |project_id| {
            if project_id == "overlay" {
                Err(SearchError::VectorSearch(
                    "overlay collection not found".to_string(),
                ))
            } else {
                Ok(vec![("parent-symbol".to_string(), 0.9)])
            }
        });

        assert_eq!(hits, vec![("parent-symbol".to_string(), 0.9)]);
        assert_eq!(failures.len(), 1);
        assert_eq!(failures[0].0, "overlay");
    }

    #[test]
    fn overlay_and_parent_hits_are_deduplicated_and_ranked() {
        let project_ids = ["overlay", "parent"];

        let (hits, failures) = search_visible_projects(&project_ids, 3, |project_id| {
            if project_id == "overlay" {
                Ok(vec![
                    ("shared".to_string(), 0.8),
                    ("overlay-only".to_string(), 0.7),
                ])
            } else {
                Ok(vec![
                    ("shared".to_string(), 0.9),
                    ("parent-only".to_string(), 0.85),
                ])
            }
        });

        assert!(failures.is_empty());
        assert_eq!(
            hits,
            vec![
                ("shared".to_string(), 0.9),
                ("parent-only".to_string(), 0.85),
                ("overlay-only".to_string(), 0.7),
            ]
        );
    }

    fn test_context() -> Context {
        Context {
            database_url: String::new(),
            project_root: PathBuf::from("/tmp/project"),
            project_id: "single".to_string(),
            quiet: true,
            falkordb: None,
            qdrant: None,
            embedding: None,
            code_vectors: Default::default(),
            indexing: Default::default(),
            daemon_url: None,
            index_scope: ProjectIndexScope::Single,
        }
    }
}
