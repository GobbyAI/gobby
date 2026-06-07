pub mod bm25;
pub mod graph_boost;
pub mod rrf;
pub mod semantic;

use std::collections::HashSet;
use std::fmt;
use std::path::{Path, PathBuf};

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
    Graph,
    Semantic,
}

impl SearchSource {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Bm25 => "bm25",
            Self::Graph => "graph",
            Self::Semantic => "semantic",
        }
    }

    pub(crate) fn from_source_name(source: &str) -> Option<Self> {
        match source {
            "bm25" => Some(Self::Bm25),
            "graph" => Some(Self::Graph),
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

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct SearchSourceExplanation {
    pub source: SearchSource,
    pub rank: usize,
    pub score: f64,
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
    pub explanations: Vec<SearchSourceExplanation>,
    pub chunk: Option<ChunkProvenance>,
    pub provenance: SearchProvenance,
}

impl WikiSearchResult {
    pub fn fusion_key(&self) -> String {
        format!(
            "{}:{}:{}",
            self.scope.scope_kind(),
            self.scope.scope_value(),
            normalized_path(&self.path)
        )
    }
}

fn normalized_path(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
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

const GRAPH_SEED_LIMIT: usize = 5;

pub fn search<B, S, G>(
    bm25_backend: &mut B,
    semantic_backend: &mut S,
    graph_backend: &mut G,
    request: SearchRequest,
) -> Result<WikiSearchResponse, SearchError>
where
    B: bm25::Bm25SearchBackend,
    S: semantic::SemanticSearchBackend,
    G: graph_boost::GraphBoostBackend,
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
            query: request.query.clone(),
            scope: request.scope.clone(),
            limit: request.limit,
        })?
    } else {
        semantic::SemanticSearchOutcome {
            hits: Vec::new(),
            degradation: None,
        }
    };
    let semantic_hits = semantic_outcome.hits;
    let seed_paths = graph_seed_paths(&bm25_hits, &semantic_hits, GRAPH_SEED_LIMIT);
    let graph_outcome = graph_backend.search_graph_boost(graph_boost::GraphBoostRequest {
        scope: request.scope.clone(),
        seed_paths,
        limit: request.limit,
    })?;
    let graph_unavailable = graph_outcome
        .degradation
        .as_ref()
        .is_some_and(search_source_unavailable);

    let mut degradations = Vec::new();
    let mut unavailable_sources = Vec::new();
    let semantic_unavailable = semantic_outcome
        .degradation
        .as_ref()
        .is_some_and(search_source_unavailable);
    if let Some(degradation) = semantic_outcome.degradation {
        degradations.push(degradation);
        if semantic_unavailable {
            unavailable_sources.push(SearchSource::Semantic.as_str().to_string());
        }
    }
    if let Some(degradation) = graph_outcome.degradation {
        degradations.push(degradation);
        if graph_unavailable {
            unavailable_sources.push(SearchSource::Graph.as_str().to_string());
        }
    }
    if !unavailable_sources.is_empty() {
        degradations.push(DegradationKind::PartialSearch {
            available: available_sources(
                request.include_semantic,
                semantic_unavailable,
                graph_unavailable,
            ),
            unavailable: unavailable_sources,
        });
    }

    Ok(rrf::fuse_sources(
        bm25_hits,
        semantic_hits,
        graph_outcome.hits,
        degradations,
        request.limit,
    ))
}

fn graph_seed_paths(
    bm25_hits: &[WikiSearchResult],
    semantic_hits: &[WikiSearchResult],
    limit: usize,
) -> Vec<PathBuf> {
    let mut seen = HashSet::new();
    let mut paths = Vec::new();
    for hit in bm25_hits.iter().chain(semantic_hits) {
        let path = hit.provenance.document_path.clone();
        if seen.insert(path.clone()) {
            paths.push(path);
        }
        if paths.len() == limit {
            break;
        }
    }
    paths
}

fn available_sources(
    include_semantic: bool,
    semantic_unavailable: bool,
    graph_unavailable: bool,
) -> Vec<String> {
    let mut available = vec![SearchSource::Bm25.as_str().to_string()];
    if !graph_unavailable {
        available.push(SearchSource::Graph.as_str().to_string());
    }
    if include_semantic && !semantic_unavailable {
        available.push(SearchSource::Semantic.as_str().to_string());
    }
    available
}

fn search_source_unavailable(degradation: &DegradationKind) -> bool {
    matches!(degradation, DegradationKind::ServiceUnavailable { .. })
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
        let mut graph = graph_boost::NoopGraphBoostBackend;

        let response = search(
            &mut bm25,
            &mut semantic,
            &mut graph,
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
                } if available.as_slice() == ["bm25", "graph"]
                    && unavailable.as_slice() == ["semantic"]
            )
        }));
    }

    #[test]
    fn graph_linked_pages_enter_search_results() {
        let scope = SearchScope::project("project-1");
        let mut bm25 = bm25::MemoryBm25Backend::new(vec![search_result(
            "document:wiki/topics/seed.md",
            scope.clone(),
            "wiki/topics/seed.md",
        )]);
        let mut semantic = semantic::UnavailableSemanticBackend;
        let mut graph = graph_boost::MemoryGraphBoostBackend::new(memory_graph(scope.clone()));

        let response = search(
            &mut bm25,
            &mut semantic,
            &mut graph,
            SearchRequest {
                query: "seed".to_string(),
                scope,
                limit: 10,
                include_semantic: false,
            },
        )
        .expect("search includes graph neighborhood");

        let linked = response
            .results
            .iter()
            .find(|result| result.path.as_path() == std::path::Path::new("wiki/topics/linked.md"))
            .expect("linked page is included");
        assert!(linked.sources.contains(&SearchSource::Graph));
    }

    #[test]
    fn combined_partial_search_reports_all_unavailable_sources_once() {
        let scope = SearchScope::project("project-1");
        let mut bm25 = bm25::MemoryBm25Backend::new(vec![search_result(
            "bm25:wiki/topics/rust.md:0",
            scope.clone(),
            "wiki/topics/rust.md",
        )]);
        let mut semantic = semantic::UnavailableSemanticBackend;
        let mut graph = DegradedGraphBackend;

        let response = search(
            &mut bm25,
            &mut semantic,
            &mut graph,
            SearchRequest {
                query: "ownership".to_string(),
                scope,
                limit: 10,
                include_semantic: true,
            },
        )
        .expect("search degrades to bm25");

        let partials = response
            .degradations
            .iter()
            .filter_map(|degradation| match degradation {
                DegradationKind::PartialSearch {
                    available,
                    unavailable,
                } => Some((available, unavailable)),
                _ => None,
            })
            .collect::<Vec<_>>();

        assert_eq!(partials.len(), 1);
        assert_eq!(partials[0].0.as_slice(), ["bm25"]);
        assert_eq!(partials[0].1.as_slice(), ["semantic", "graph"]);
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
            explanations: Vec::new(),
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

    fn memory_graph(scope: SearchScope) -> crate::graph::MemoryWikiGraph {
        let mut graph = crate::graph::MemoryWikiGraph::default();
        graph.replace_facts(crate::graph::WikiGraphFacts {
            documents: vec![
                graph_doc(scope.clone(), "wiki/topics/seed.md"),
                graph_doc(scope.clone(), "wiki/topics/linked.md"),
            ],
            links: vec![crate::graph::WikiGraphLink {
                scope,
                source_path: PathBuf::from("wiki/topics/seed.md"),
                raw_target: "wiki/topics/linked.md".to_string(),
                target: crate::graph::WikiGraphLinkTarget::Resolved(PathBuf::from(
                    "wiki/topics/linked.md",
                )),
            }],
            sources: Vec::new(),
        });
        graph
    }

    fn graph_doc(scope: SearchScope, path: &str) -> crate::graph::WikiGraphDocument {
        crate::graph::WikiGraphDocument {
            scope,
            path: PathBuf::from(path),
            title: None,
        }
    }

    struct DegradedGraphBackend;

    impl graph_boost::GraphBoostBackend for DegradedGraphBackend {
        fn search_graph_boost(
            &mut self,
            _request: graph_boost::GraphBoostRequest,
        ) -> Result<graph_boost::GraphBoostOutcome, SearchError> {
            Ok(graph_boost::GraphBoostOutcome {
                hits: Vec::new(),
                degradation: Some(DegradationKind::ServiceUnavailable {
                    service: "gwiki_graph".to_string(),
                    state: gobby_core::degradation::ServiceState::Unreachable {
                        message: "offline".to_string(),
                    },
                }),
            })
        }
    }
}
