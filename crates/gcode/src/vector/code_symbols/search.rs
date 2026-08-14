use std::collections::{HashMap, HashSet};

use crate::config::{CODE_SYMBOL_COLLECTION_PREFIX, Context, ProjectIndexScope};
use crate::{db, visibility};

use super::embedding::{embed_query_with_source, embedding_source_from_context};
use super::qdrant::{collection_name, vector_search};
#[cfg(test)]
use super::types::{CodeSymbolVectorSearchHit, CodeSymbolVectorSearchRequest};

type RankedHit = (String, f64);
type ProjectSearchFailure = (String, SearchError);
const POST_FILTER_OVERFETCH_FACTOR: usize = 4;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SearchError {
    MissingQdrantConfig,
    MissingEmbeddingConfig,
    QueryEmbeddingFailed,
    InvalidCollectionName(gobby_core::qdrant::CollectionNameError),
    VectorSearch(String),
    Visibility(String),
}

impl std::fmt::Display for SearchError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::MissingQdrantConfig => write!(f, "Qdrant config is missing"),
            Self::MissingEmbeddingConfig => write!(f, "embedding config is missing"),
            Self::QueryEmbeddingFailed => write!(f, "query embedding failed"),
            Self::InvalidCollectionName(error) => write!(f, "{error}"),
            Self::VectorSearch(error) => write!(f, "semantic vector search failed: {error}"),
            Self::Visibility(error) => {
                write!(f, "semantic vector visibility lookup failed: {error}")
            }
        }
    }
}

impl std::error::Error for SearchError {}

#[cfg(test)]
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
    let fetch_limit = post_filter_fetch_limit(request.limit);
    match vector_search(qdrant_config, &collection, &embedding, fetch_limit) {
        Ok(hits) if hits.is_empty() => Ok(Vec::new()),
        Ok(hits) => Ok(post_filter_ranked_hits(ctx, hits, request.limit)?
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
    if ctx.runtime_config_capture_degraded() {
        log::debug!(
            "semantic vector search skipped: runtime configuration capture degraded to local \
             defaults"
        );
        return Vec::new();
    }
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

    let fetch_limit = post_filter_fetch_limit(limit);
    let project_ids = visible_vector_project_ids(ctx);
    let (hits, failures) = search_visible_projects(&project_ids, fetch_limit, |project_id| {
        let collection = collection_name(CODE_SYMBOL_COLLECTION_PREFIX, project_id)
            .map_err(SearchError::InvalidCollectionName)?;
        vector_search(qdrant_config, &collection, &embedding, fetch_limit)
            .map_err(|error| SearchError::VectorSearch(error.to_string()))
    });

    for (project_id, error) in failures {
        if hits.is_empty() || project_id != ctx.project_id {
            log::warn!("semantic vector search degraded for project {project_id}: {error}");
        } else {
            log::debug!("overlay semantic vectors unavailable for project {project_id}: {error}");
        }
    }

    match post_filter_ranked_hits(ctx, hits, limit) {
        Ok(hits) => hits,
        Err(error) => {
            log::warn!("semantic vector search skipped: {error}");
            Vec::new()
        }
    }
}

fn post_filter_fetch_limit(limit: usize) -> usize {
    limit.saturating_mul(POST_FILTER_OVERFETCH_FACTOR)
}

fn post_filter_ranked_hits(
    ctx: &Context,
    hits: Vec<RankedHit>,
    limit: usize,
) -> Result<Vec<RankedHit>, SearchError> {
    let ids = hits
        .iter()
        .map(|(symbol_id, _)| symbol_id.clone())
        .collect::<Vec<_>>();
    let mut conn = db::connect_readonly(&ctx.database_url)
        .map_err(|error| SearchError::Visibility(error.to_string()))?;
    let visible_ids = visibility::visible_symbols_by_ids(&mut conn, ctx, &ids)
        .map_err(|error| SearchError::Visibility(error.to_string()))?
        .into_iter()
        .map(|symbol| symbol.id)
        .collect::<HashSet<_>>();

    Ok(retain_ranked_hits(hits, &visible_ids, limit))
}

fn retain_ranked_hits(
    hits: Vec<RankedHit>,
    visible_ids: &HashSet<String>,
    limit: usize,
) -> Vec<RankedHit> {
    hits.into_iter()
        .filter(|(symbol_id, _)| visible_ids.contains(symbol_id))
        .take(limit)
        .collect()
}

#[cfg(test)]
mod post_filter_tests {
    use super::*;

    #[test]
    fn overfetch_preserves_visible_hits_below_stale_projection_rows() {
        let hits = vec![
            ("stale-1".to_string(), 1.0),
            ("stale-2".to_string(), 0.9),
            ("stale-3".to_string(), 0.8),
            ("current-1".to_string(), 0.7),
            ("current-2".to_string(), 0.6),
        ];
        let visible_ids = HashSet::from(["current-1".to_string(), "current-2".to_string()]);

        assert_eq!(post_filter_fetch_limit(2), 8);
        assert_eq!(
            retain_ranked_hits(hits, &visible_ids, 2),
            vec![
                ("current-1".to_string(), 0.7),
                ("current-2".to_string(), 0.6),
            ]
        );
    }
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
    fn scoped_capture_degrade_disables_semantic_search() {
        use std::net::TcpListener;
        use std::sync::Arc;
        use std::sync::atomic::{AtomicUsize, Ordering};
        use std::thread;

        use crate::config::{EmbeddingConfig, QdrantConfig};

        let hits = Arc::new(AtomicUsize::new(0));
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test transport");
        let addr = listener.local_addr().expect("listener addr");
        let transport_hits = Arc::clone(&hits);
        thread::spawn(move || {
            for stream in listener.incoming() {
                if stream.is_ok() {
                    transport_hits.fetch_add(1, Ordering::SeqCst);
                }
            }
        });
        let url = format!("http://{addr}");
        let mut ctx = test_context();
        ctx.qdrant = Some(QdrantConfig {
            url: Some(url.clone()),
            api_key: None,
        });
        ctx.embedding = Some(EmbeddingConfig {
            api_base: url,
            model: "test-embed".to_string(),
            api_key: Some("test".to_string()),
            query_prefix: None,
            timeout_seconds: 1,
        });

        ctx.set_runtime_config_capture_degraded_for_test(true);
        assert!(ctx.runtime_config_capture_degraded());
        assert!(semantic_search(&ctx, "query", 10).is_empty());
        assert_eq!(hits.load(Ordering::SeqCst), 0);
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
            runtime_config_capture_degraded: false,
            indexing: Default::default(),
            daemon_url: None,
            grant_ai: None,
            index_scope: ProjectIndexScope::Single,
        }
    }
}
