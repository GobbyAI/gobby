use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

use gobby_core::degradation::{DegradationKind, ServiceState};

use crate::graph::MemoryWikiGraph;
use crate::search::{
    SearchError, SearchHitKind, SearchProvenance, SearchScope, SearchSource, WikiSearchResult,
    bm25::is_keyword_searchable_path,
};
use crate::support::text::slugify;

const GRAPH_SOURCE_KIND: &str = "graph";
const GRAPH_SERVICE: &str = "gwiki_graph";

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

pub struct PostgresGraphBoostBackend {
    database_url: String,
}

impl PostgresGraphBoostBackend {
    pub fn new(database_url: impl Into<String>) -> Self {
        Self {
            database_url: database_url.into(),
        }
    }
}

impl GraphBoostBackend for PostgresGraphBoostBackend {
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

        let mut conn = match gobby_core::postgres::connect_readonly(&self.database_url) {
            Ok(conn) => conn,
            Err(error) => return Ok(degraded(error.to_string())),
        };
        let scope_kind = request.scope.scope_kind().to_string();
        let scope_id = request.scope.scope_value().to_string();

        let documents = match conn.query(
            "SELECT path, title
             FROM gwiki_documents
             WHERE scope_kind = $1 AND scope_id = $2",
            &[&scope_kind, &scope_id],
        ) {
            Ok(rows) => rows
                .into_iter()
                .map(|row| GraphBoostDocument {
                    path: PathBuf::from(row.get::<_, String>("path")),
                    title: row.get::<_, Option<String>>("title"),
                })
                .collect::<Vec<_>>(),
            Err(error) => return Ok(degraded(error.to_string())),
        };

        let links = match conn.query(
            "SELECT path, target_path
             FROM gwiki_links
             WHERE scope_kind = $1 AND scope_id = $2",
            &[&scope_kind, &scope_id],
        ) {
            Ok(rows) => rows
                .into_iter()
                .map(|row| GraphBoostLink {
                    source_path: PathBuf::from(row.get::<_, String>("path")),
                    target_path: row.get::<_, String>("target_path"),
                })
                .collect::<Vec<_>>(),
            Err(error) => return Ok(degraded(error.to_string())),
        };

        let ranked_paths =
            rank_link_neighborhood(&documents, &links, &request.seed_paths, request.limit);
        Ok(GraphBoostOutcome {
            hits: graph_boost_hits(request.scope, ranked_paths, request.limit),
            degradation: None,
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
            let Some(target_path) =
                resolve_graph_target(&link.target_path, &document_paths, &slug_targets)
            else {
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

fn degraded(message: String) -> GraphBoostOutcome {
    GraphBoostOutcome {
        hits: Vec::new(),
        degradation: Some(DegradationKind::ServiceUnavailable {
            service: GRAPH_SERVICE.to_string(),
            state: ServiceState::Unreachable { message },
        }),
    }
}

fn resolve_graph_target(
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

    let target_slug = slugify(normalized.strip_suffix(".md").unwrap_or(&normalized));
    slug_targets.get(&target_slug).cloned()
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
            document("seed.md", Some("Seed")),
            document("outbound.md", Some("Outbound")),
            document("backlink.md", Some("Backlink")),
        ];
        let links = vec![
            link("seed.md", "outbound.md"),
            link("backlink.md", "Seed"),
            link("outbound.md", "https://example.com"),
        ];

        let ranked = rank_link_neighborhood(&documents, &links, &[PathBuf::from("seed.md")], 10);

        assert_eq!(ranked.len(), 2);
        assert_eq!(ranked[0], (PathBuf::from("outbound.md"), 1.0));
        assert_eq!(ranked[1], (PathBuf::from("backlink.md"), 0.8));
    }

    #[test]
    fn graph_boost_hits_marks_graph_source() {
        let hits = graph_boost_hits(
            SearchScope::topic("docs"),
            vec![(PathBuf::from("wiki/topics/linked.md"), 1.0)],
            10,
        );

        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].sources, vec![SearchSource::Graph]);
        assert_eq!(
            hits[0].provenance.document_path,
            PathBuf::from("wiki/topics/linked.md")
        );
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
