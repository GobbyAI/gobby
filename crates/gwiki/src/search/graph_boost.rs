use std::collections::{BTreeMap, BTreeSet};
use std::path::{Component, Path, PathBuf};

use gobby_core::config::FalkorConfig;
use gobby_core::degradation::{DegradationKind, ServiceState};
use gobby_core::falkor::GraphClient;

use crate::graph::MemoryWikiGraph;
use crate::search::{
    SearchError, SearchHitKind, SearchProvenance, SearchScope, SearchSource, WikiSearchResult,
    bm25::is_keyword_searchable_path,
};
use crate::support::text::slugify;

const GRAPH_SOURCE_KIND: &str = "graph";
const GRAPH_SERVICE: &str = "gwiki_graph";
const DEFAULT_GRAPH_BOOST_DOCUMENT_QUERY_LIMIT: i64 = 10_000;
const DEFAULT_GRAPH_BOOST_LINK_QUERY_LIMIT: i64 = 50_000;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct GraphBoostConfig {
    pub document_query_limit: i64,
    pub link_query_limit: i64,
}

impl Default for GraphBoostConfig {
    fn default() -> Self {
        Self {
            document_query_limit: DEFAULT_GRAPH_BOOST_DOCUMENT_QUERY_LIMIT,
            link_query_limit: DEFAULT_GRAPH_BOOST_LINK_QUERY_LIMIT,
        }
    }
}

pub struct GraphBoostRequest {
    pub scope: SearchScope,
    pub seed_paths: Vec<PathBuf>,
    pub limit: usize,
}

pub struct GraphBoostOutcome {
    pub hits: Vec<WikiSearchResult>,
    pub degradation: Option<DegradationKind>,
}

pub trait GraphBoostBackend {
    fn search_graph_boost(
        &mut self,
        request: GraphBoostRequest,
    ) -> Result<GraphBoostOutcome, SearchError>;
}

pub struct NoopGraphBoostBackend;

impl GraphBoostBackend for NoopGraphBoostBackend {
    fn search_graph_boost(
        &mut self,
        _request: GraphBoostRequest,
    ) -> Result<GraphBoostOutcome, SearchError> {
        Ok(GraphBoostOutcome {
            hits: Vec::new(),
            degradation: None,
        })
    }
}

pub struct UnavailableGraphBoostBackend {
    degradation: DegradationKind,
}

impl UnavailableGraphBoostBackend {
    pub fn unreachable(message: String) -> Self {
        Self {
            degradation: graph_degradation(message),
        }
    }
}

impl GraphBoostBackend for UnavailableGraphBoostBackend {
    fn search_graph_boost(
        &mut self,
        _request: GraphBoostRequest,
    ) -> Result<GraphBoostOutcome, SearchError> {
        Ok(GraphBoostOutcome {
            hits: Vec::new(),
            degradation: Some(self.degradation.clone()),
        })
    }
}

impl<T: GraphBoostBackend + ?Sized> GraphBoostBackend for Box<T> {
    fn search_graph_boost(
        &mut self,
        request: GraphBoostRequest,
    ) -> Result<GraphBoostOutcome, SearchError> {
        (**self).search_graph_boost(request)
    }
}

pub struct MemoryGraphBoostBackend {
    graph: MemoryWikiGraph,
}

impl MemoryGraphBoostBackend {
    pub fn new(graph: MemoryWikiGraph) -> Self {
        Self { graph }
    }
}

impl GraphBoostBackend for MemoryGraphBoostBackend {
    fn search_graph_boost(
        &mut self,
        request: GraphBoostRequest,
    ) -> Result<GraphBoostOutcome, SearchError> {
        let ranked_paths =
            self.graph
                .related_paths(&request.scope, &request.seed_paths, request.limit);
        Ok(GraphBoostOutcome {
            hits: graph_boost_hits(request.scope, ranked_paths, request.limit),
            degradation: None,
        })
    }
}

pub struct FalkorGraphBoostBackend {
    client: GraphClient,
    config: GraphBoostConfig,
}

impl FalkorGraphBoostBackend {
    pub fn new(config: &FalkorConfig) -> Result<Self, SearchError> {
        Self::new_with_config(config, GraphBoostConfig::default())
    }

    pub fn new_with_config(
        falkor: &FalkorConfig,
        config: GraphBoostConfig,
    ) -> Result<Self, SearchError> {
        let client = GraphClient::from_config(falkor, crate::falkor_graph::FALKORDB_GRAPH_NAME)
            .map_err(|error| {
                SearchError::Backend(format!("connect to FalkorDB wiki graph: {error}"))
            })?;
        Ok(Self { client, config })
    }
}

impl GraphBoostBackend for FalkorGraphBoostBackend {
    fn search_graph_boost(
        &mut self,
        request: GraphBoostRequest,
    ) -> Result<GraphBoostOutcome, SearchError> {
        if request.limit == 0 || request.seed_paths.is_empty() {
            return Ok(GraphBoostOutcome {
                hits: Vec::new(),
                degradation: None,
            });
        }

        let data = match crate::falkor_graph::load_graph_boost_data(
            &mut self.client,
            &request.scope,
            self.config.document_query_limit,
            self.config.link_query_limit,
        ) {
            Ok(data) => data,
            Err(error) => {
                return Ok(GraphBoostOutcome {
                    hits: Vec::new(),
                    degradation: Some(graph_degradation(error.to_string())),
                });
            }
        };
        let ranked_paths = rank_link_neighborhood(
            &data.documents,
            &data.links,
            &request.seed_paths,
            request.limit,
        );
        Ok(GraphBoostOutcome {
            hits: graph_boost_hits(request.scope, ranked_paths, request.limit),
            degradation: data.degradation,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GraphBoostDocument {
    pub path: PathBuf,
    pub title: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GraphBoostLink {
    pub source_path: PathBuf,
    pub target_path: String,
}

pub fn rank_link_neighborhood(
    documents: &[GraphBoostDocument],
    links: &[GraphBoostLink],
    seed_paths: &[PathBuf],
    limit: usize,
) -> Vec<(PathBuf, f64)> {
    if seed_paths.is_empty() || limit == 0 {
        return Vec::new();
    }

    let document_paths = documents
        .iter()
        .map(|document| document.path.clone())
        .collect::<BTreeSet<_>>();
    let slug_targets = slug_target_map(documents);
    let seed_set = seed_paths.iter().cloned().collect::<BTreeSet<_>>();
    let mut scores = BTreeMap::<PathBuf, f64>::new();

    for (rank, seed_path) in seed_paths.iter().enumerate() {
        if !document_paths.contains(seed_path) {
            continue;
        }
        let seed_score = 1.0 / (rank + 1) as f64;
        for link in links {
            if !document_paths.contains(&link.source_path) {
                continue;
            }
            let Some(target_path) = resolve_graph_target(
                &link.source_path,
                &link.target_path,
                &document_paths,
                &slug_targets,
            ) else {
                continue;
            };

            let candidate = if &link.source_path == seed_path {
                Some((target_path, seed_score))
            } else if target_path == *seed_path {
                // Backlinks are useful context, but direct outbound neighbors
                // usually better express the user's starting point.
                Some((link.source_path.clone(), seed_score * 0.8))
            } else {
                None
            };
            let Some((path, score)) = candidate else {
                continue;
            };
            if seed_set.contains(&path) {
                continue;
            }
            *scores.entry(path).or_default() += score;
        }
    }

    let mut ranked = scores.into_iter().collect::<Vec<_>>();
    ranked.sort_by(|(left_path, left_score), (right_path, right_score)| {
        right_score
            .partial_cmp(left_score)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| left_path.cmp(right_path))
    });
    ranked.retain(|(path, _)| is_keyword_searchable_path(&path.to_string_lossy()));
    ranked.truncate(limit);
    ranked
}

pub fn graph_boost_hits(
    scope: SearchScope,
    ranked_paths: Vec<(PathBuf, f64)>,
    limit: usize,
) -> Vec<WikiSearchResult> {
    ranked_paths
        .into_iter()
        .filter(|(path, _)| is_keyword_searchable_path(&path.to_string_lossy()))
        .take(limit)
        .map(|(path, score)| graph_result(&scope, path, score))
        .collect()
}

fn graph_result(scope: &SearchScope, path: PathBuf, score: f64) -> WikiSearchResult {
    let id = format!("document:{}", path.to_string_lossy().replace('\\', "/"));
    let provenance = SearchProvenance {
        document_path: path.clone(),
        source_path: path.clone(),
        source_kind: GRAPH_SOURCE_KIND.to_string(),
        content_hash: None,
    };
    WikiSearchResult {
        id,
        title: None,
        scope: scope.clone(),
        source_path: path.clone(),
        path,
        hit_kind: SearchHitKind::Document,
        snippet: String::new(),
        score,
        sources: vec![SearchSource::Graph],
        explanations: Vec::new(),
        chunk: None,
        provenance,
    }
}

fn graph_degradation(message: String) -> DegradationKind {
    DegradationKind::ServiceUnavailable {
        service: GRAPH_SERVICE.to_string(),
        state: ServiceState::Unreachable { message },
    }
}

fn resolve_graph_target(
    source_path: &Path,
    raw_target: &str,
    document_paths: &BTreeSet<PathBuf>,
    slug_targets: &BTreeMap<String, PathBuf>,
) -> Option<PathBuf> {
    let trimmed = raw_target.trim();
    if is_external_target(trimmed) {
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
    if document_paths.contains(&direct) {
        return Some(direct);
    }

    if let Some(relative) = source_path
        .parent()
        .map(|parent| normalize_path(parent.join(&normalized)))
        .filter(|path| document_paths.contains(path))
    {
        return Some(relative);
    }

    let target_slug = slugify(normalized.strip_suffix(".md").unwrap_or(&normalized));
    slug_targets.get(&target_slug).cloned()
}

fn normalize_path(path: PathBuf) -> PathBuf {
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                normalized.pop();
            }
            Component::Normal(value) => normalized.push(value),
            Component::RootDir | Component::Prefix(_) => {}
        }
    }
    normalized
}

fn slug_target_map(documents: &[GraphBoostDocument]) -> BTreeMap<String, PathBuf> {
    let mut targets = BTreeMap::new();
    for document in documents {
        if let Some(file_slug) = document
            .path
            .file_stem()
            .and_then(|value| value.to_str())
            .map(slugify)
        {
            targets
                .entry(file_slug)
                .or_insert_with(|| document.path.clone());
        }
        if let Some(title_slug) = document.title.as_deref().map(slugify) {
            targets
                .entry(title_slug)
                .or_insert_with(|| document.path.clone());
        }
    }
    targets
}

fn is_external_target(target: &str) -> bool {
    target.is_empty()
        || target.contains("://")
        || target.starts_with("//")
        || target.starts_with("\\\\")
        || target.starts_with("mailto:")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rank_link_neighborhood_boosts_outbound_and_backlinks() {
        let documents = vec![
            document("knowledge/topics/seed.md", Some("Seed")),
            document("knowledge/topics/outbound.md", Some("Outbound")),
            document("knowledge/topics/backlink.md", Some("Backlink")),
        ];
        let links = vec![
            link("knowledge/topics/seed.md", "knowledge/topics/outbound.md"),
            link("knowledge/topics/backlink.md", "Seed"),
            link("knowledge/topics/outbound.md", "https://example.com"),
        ];

        let ranked = rank_link_neighborhood(
            &documents,
            &links,
            &[PathBuf::from("knowledge/topics/seed.md")],
            10,
        );

        assert_eq!(ranked.len(), 2);
        assert_eq!(
            ranked[0],
            (PathBuf::from("knowledge/topics/outbound.md"), 1.0)
        );
        assert_eq!(
            ranked[1],
            (PathBuf::from("knowledge/topics/backlink.md"), 0.8)
        );
    }

    #[test]
    fn rank_link_neighborhood_filters_non_searchable_before_truncating() {
        let documents = vec![
            document("knowledge/topics/seed.md", Some("Seed")),
            document("meta/high.md", Some("High")),
            document("knowledge/topics/low.md", Some("Low")),
        ];
        let links = vec![
            link("knowledge/topics/seed.md", "meta/high.md"),
            link("knowledge/topics/low.md", "Seed"),
        ];

        let ranked = rank_link_neighborhood(
            &documents,
            &links,
            &[PathBuf::from("knowledge/topics/seed.md")],
            1,
        );

        assert_eq!(
            ranked,
            vec![(PathBuf::from("knowledge/topics/low.md"), 0.8)]
        );
    }

    #[test]
    fn rank_link_neighborhood_resolves_targets_relative_to_source() {
        let documents = vec![
            document("knowledge/topics/seed.md", Some("Seed")),
            document("knowledge/topics/outbound.md", Some("Outbound")),
        ];
        let links = vec![link("knowledge/topics/seed.md", "outbound.md")];

        let ranked = rank_link_neighborhood(
            &documents,
            &links,
            &[PathBuf::from("knowledge/topics/seed.md")],
            10,
        );

        assert_eq!(
            ranked,
            vec![(PathBuf::from("knowledge/topics/outbound.md"), 1.0)]
        );
    }

    #[test]
    fn graph_boost_hits_marks_graph_source() {
        let hits = graph_boost_hits(
            SearchScope::topic("docs"),
            vec![(PathBuf::from("knowledge/topics/linked.md"), 1.0)],
            10,
        );

        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].sources, vec![SearchSource::Graph]);
        assert_eq!(
            hits[0].provenance.document_path,
            PathBuf::from("knowledge/topics/linked.md")
        );
    }

    #[test]
    fn unavailable_graph_backend_reports_service_degradation() {
        let mut backend =
            UnavailableGraphBoostBackend::unreachable("connection refused".to_string());

        let outcome = backend
            .search_graph_boost(GraphBoostRequest {
                scope: SearchScope::project("project-1"),
                seed_paths: vec![PathBuf::from("docs/a.md")],
                limit: 10,
            })
            .expect("unavailable backend degrades");

        assert!(outcome.hits.is_empty());
        assert!(matches!(
            outcome.degradation,
            Some(DegradationKind::ServiceUnavailable {
                service,
                state: ServiceState::Unreachable { message }
            }) if service == GRAPH_SERVICE && message.contains("connection refused")
        ));
    }

    fn document(path: &str, title: Option<&str>) -> GraphBoostDocument {
        GraphBoostDocument {
            path: PathBuf::from(path),
            title: title.map(ToOwned::to_owned),
        }
    }

    fn link(source_path: &str, target_path: &str) -> GraphBoostLink {
        GraphBoostLink {
            source_path: PathBuf::from(source_path),
            target_path: target_path.to_string(),
        }
    }
}
