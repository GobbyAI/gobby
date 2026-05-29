pub mod bm25;
pub mod semantic;

use std::collections::BTreeMap;
use std::fmt;
use std::path::PathBuf;

use gobby_core::degradation::DegradationKind;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub enum SearchScope {
    Project { project_id: String },
    Topic { topic: String },
}

impl SearchScope {
    pub fn project(project_id: impl Into<String>) -> Self {
        Self::Project {
            project_id: project_id.into(),
        }
    }

    pub fn topic(topic: impl Into<String>) -> Self {
        Self::Topic {
            topic: topic.into(),
        }
    }

    pub fn scope_kind(&self) -> &'static str {
        match self {
            Self::Project { .. } => "project",
            Self::Topic { .. } => "topic",
        }
    }

    pub fn scope_value(&self) -> &str {
        match self {
            Self::Project { project_id } => project_id,
            Self::Topic { topic } => topic,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub enum SearchSource {
    Bm25,
    Semantic,
}

impl SearchSource {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Bm25 => "bm25",
            Self::Semantic => "semantic",
        }
    }

    fn from_source_name(source: &str) -> Option<Self> {
        match source {
            "bm25" => Some(Self::Bm25),
            "semantic" => Some(Self::Semantic),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum SearchHitKind {
    Document,
    Chunk,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChunkProvenance {
    pub chunk_index: usize,
    pub byte_start: usize,
    pub byte_end: usize,
    pub heading: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SearchProvenance {
    pub document_path: PathBuf,
    pub source_path: PathBuf,
    pub source_kind: String,
    pub content_hash: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WikiSearchResult {
    pub id: String,
    pub title: Option<String>,
    pub scope: SearchScope,
    pub path: PathBuf,
    pub source_path: PathBuf,
    pub hit_kind: SearchHitKind,
    pub snippet: String,
    pub score: f64,
    pub sources: Vec<SearchSource>,
    pub chunk: Option<ChunkProvenance>,
    pub provenance: SearchProvenance,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SearchRequest {
    pub query: String,
    pub scope: SearchScope,
    pub limit: usize,
    pub include_semantic: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WikiSearchResponse {
    pub results: Vec<WikiSearchResult>,
    pub degradations: Vec<DegradationKind>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SearchError {
    Backend(String),
}

impl fmt::Display for SearchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Backend(message) => write!(f, "wiki search backend failed: {message}"),
        }
    }
}

impl std::error::Error for SearchError {}

pub fn search<B, S>(
    bm25_backend: &mut B,
    semantic_backend: &mut S,
    request: SearchRequest,
) -> Result<WikiSearchResponse, SearchError>
where
    B: bm25::Bm25SearchBackend,
    S: semantic::SemanticSearchBackend,
{
    let bm25_hits = bm25::search_bm25(
        bm25_backend,
        bm25::Bm25SearchRequest {
            query: request.query.clone(),
            scope: request.scope.clone(),
            limit: request.limit,
        },
    )?;

    let semantic_outcome = if request.include_semantic {
        semantic_backend.search_semantic(semantic::SemanticSearchRequest {
            query: request.query,
            scope: request.scope,
            limit: request.limit,
        })?
    } else {
        semantic::SemanticSearchOutcome {
            hits: Vec::new(),
            degradation: None,
        }
    };

    let mut degradations = Vec::new();
    if let Some(degradation) = semantic_outcome.degradation {
        degradations.push(degradation);
        degradations.push(DegradationKind::PartialSearch {
            available: vec![SearchSource::Bm25.as_str().to_string()],
            unavailable: vec![SearchSource::Semantic.as_str().to_string()],
        });
    }

    let results = fuse_results(bm25_hits, semantic_outcome.hits);
    Ok(WikiSearchResponse {
        results,
        degradations,
    })
}

fn fuse_results(
    bm25_hits: Vec<WikiSearchResult>,
    semantic_hits: Vec<WikiSearchResult>,
) -> Vec<WikiSearchResult> {
    let bm25_ids = bm25_hits
        .iter()
        .map(|hit| hit.id.clone())
        .collect::<Vec<_>>();
    let semantic_ids = semantic_hits
        .iter()
        .map(|hit| hit.id.clone())
        .collect::<Vec<_>>();

    let mut by_id = BTreeMap::new();
    for hit in bm25_hits.into_iter().chain(semantic_hits) {
        by_id.entry(hit.id.clone()).or_insert(hit);
    }

    let mut sources = Vec::new();
    if !bm25_ids.is_empty() {
        sources.push((SearchSource::Bm25.as_str(), bm25_ids));
    }
    if !semantic_ids.is_empty() {
        sources.push((SearchSource::Semantic.as_str(), semantic_ids));
    }

    gobby_core::search::rrf_merge(sources)
        .into_iter()
        .filter_map(|fused| {
            let mut result = by_id.remove(&fused.id)?;
            result.score = fused.score;
            result.sources = fused
                .sources
                .iter()
                .filter_map(|source| SearchSource::from_source_name(source))
                .collect();
            Some(result)
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn semantic_unavailable_degrades() {
        let mut bm25 = bm25::MemoryBm25Backend::new(vec![search_result(
            "bm25:wiki/topics/rust.md:0",
            SearchScope::project("project-1"),
            "wiki/topics/rust.md",
        )]);
        let mut semantic = semantic::UnavailableSemanticBackend;

        let response = search(
            &mut bm25,
            &mut semantic,
            SearchRequest {
                query: "ownership".to_string(),
                scope: SearchScope::project("project-1"),
                limit: 10,
                include_semantic: true,
            },
        )
        .expect("search degrades to bm25");

        assert_eq!(response.results.len(), 1);
        assert_eq!(response.results[0].sources, vec![SearchSource::Bm25]);
        assert!(response.degradations.iter().any(|degradation| {
            matches!(
                degradation,
                gobby_core::degradation::DegradationKind::ServiceUnavailable { service, .. }
                    if service == "qdrant"
            )
        }));
        assert!(response.degradations.iter().any(|degradation| {
            matches!(
                degradation,
                gobby_core::degradation::DegradationKind::PartialSearch {
                    available,
                    unavailable
                } if available.as_slice() == ["bm25"]
                    && unavailable.as_slice() == ["semantic"]
            )
        }));
    }

    fn search_result(id: &str, scope: SearchScope, path: &str) -> WikiSearchResult {
        WikiSearchResult {
            id: id.to_string(),
            title: Some("Rust".to_string()),
            scope: scope.clone(),
            path: path.into(),
            source_path: "raw/INDEX.md".into(),
            hit_kind: SearchHitKind::Chunk,
            snippet: "Ownership and borrowing".to_string(),
            score: 1.0,
            sources: vec![SearchSource::Bm25],
            chunk: Some(ChunkProvenance {
                chunk_index: 0,
                byte_start: 0,
                byte_end: 24,
                heading: Some("Rust".to_string()),
            }),
            provenance: SearchProvenance {
                document_path: path.into(),
                source_path: "raw/INDEX.md".into(),
                source_kind: "topic".to_string(),
                content_hash: Some("hash".to_string()),
            },
        }
    }
}
