use std::collections::{BTreeMap, BTreeSet};
use std::path::{Component, Path, PathBuf};

use gobby_core::degradation::{DegradationKind, ServiceState};

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
    scope: SearchScope,
    documents: Vec<GraphBoostDocument>,
    links: Vec<GraphBoostLink>,
    degradation: Option<DegradationKind>,
}

impl PostgresGraphBoostBackend {
    pub fn new(conn: &mut postgres::Client, scope: SearchScope) -> Self {
        Self::new_with_config(conn, scope, GraphBoostConfig::default())
    }

    pub fn new_with_config(
        conn: &mut postgres::Client,
        scope: SearchScope,
        config: GraphBoostConfig,
    ) -> Self {
        match load_graph_boost_data(conn, &scope, config) {
            Ok(data) => Self {
                scope,
                documents: data.documents,
                links: data.links,
                degradation: data.degradation,
            },
            Err(error) => Self {
                scope,
                documents: Vec::new(),
                links: Vec::new(),
                degradation: Some(graph_degradation(error.to_string())),
            },
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
        if let Some(degradation) = self.degradation.clone()
            && self.documents.is_empty()
            && self.links.is_empty()
        {
            return Ok(GraphBoostOutcome {
                hits: Vec::new(),
                degradation: Some(degradation),
            });
        }
        if request.scope != self.scope {
            return Ok(GraphBoostOutcome {
                hits: Vec::new(),
                degradation: Some(graph_degradation(format!(
                    "graph boost backend loaded for {}:{}, requested {}:{}",
                    self.scope.scope_kind(),
                    self.scope.scope_value(),
                    request.scope.scope_kind(),
                    request.scope.scope_value()
                ))),
            });
        }

        let ranked_paths = rank_link_neighborhood(
            &self.documents,
            &self.links,
            &request.seed_paths,
            request.limit,
        );
        Ok(GraphBoostOutcome {
            hits: graph_boost_hits(request.scope, ranked_paths, request.limit),
            degradation: self.degradation.clone(),
        })
    }
}

struct GraphBoostData {
    documents: Vec<GraphBoostDocument>,
    links: Vec<GraphBoostLink>,
    degradation: Option<DegradationKind>,
}

fn load_graph_boost_data(
    conn: &mut postgres::Client,
    scope: &SearchScope,
    config: GraphBoostConfig,
) -> Result<GraphBoostData, SearchError> {
    let scope_kind = scope.scope_kind().to_string();
    let scope_id = scope.scope_value().to_string();
    let document_query_limit = config.document_query_limit.max(0);
    let link_query_limit = config.link_query_limit.max(0);

    let document_count = count_graph_boost_rows(
        conn,
        "gwiki_documents",
        &scope_kind,
        &scope_id,
        "count graph boost documents",
    )?;
    if let Some(error) = graph_boost_cap_error("document", document_count, document_query_limit) {
        return Err(error);
    }
    let link_count = count_graph_boost_rows(
        conn,
        "gwiki_links",
        &scope_kind,
        &scope_id,
        "count graph boost links",
    )?;
    if let Some(error) = graph_boost_cap_error("link", link_count, link_query_limit) {
        return Err(error);
    }

    let document_rows = conn
        .query(
            "SELECT path, title
             FROM gwiki_documents
             WHERE scope_kind = $1 AND scope_id = $2
             ORDER BY path
             LIMIT $3",
            &[&scope_kind, &scope_id, &document_query_limit],
        )
        .map_err(|error| SearchError::Backend(format!("load graph boost documents: {error}")))?;
    let documents = document_rows
        .into_iter()
        .map(|row| GraphBoostDocument {
            path: PathBuf::from(row.get::<_, String>("path")),
            title: row.get::<_, Option<String>>("title"),
        })
        .collect::<Vec<_>>();

    let link_rows = conn
        .query(
            "SELECT path, target_path
             FROM gwiki_links
             WHERE scope_kind = $1 AND scope_id = $2
             ORDER BY path, target_path
             LIMIT $3",
            &[&scope_kind, &scope_id, &link_query_limit],
        )
        .map_err(|error| SearchError::Backend(format!("load graph boost links: {error}")))?;
    let links = link_rows
        .into_iter()
        .map(|row| GraphBoostLink {
            source_path: PathBuf::from(row.get::<_, String>("path")),
            target_path: row.get::<_, String>("target_path"),
        })
        .collect::<Vec<_>>();

    Ok(GraphBoostData {
        documents,
        links,
        degradation: None,
    })
}

fn graph_boost_cap_error(component: &'static str, count: i64, limit: i64) -> Option<SearchError> {
    if count <= limit {
        return None;
    }
    Some(SearchError::Backend(format!(
        "graph boost {component} count {count} exceeds limit {limit}; refusing partial graph boost data"
    )))
}

fn count_graph_boost_rows(
    conn: &mut postgres::Client,
    table: &'static str,
    scope_kind: &str,
    scope_id: &str,
    action: &'static str,
) -> Result<i64, SearchError> {
    let sql = format!(
        "SELECT COUNT(*)::BIGINT AS count
         FROM {table}
         WHERE scope_kind = $1 AND scope_id = $2"
    );
    conn.query_one(&sql, &[&scope_kind, &scope_id])
        .and_then(|row| row.try_get("count"))
        .map_err(|error| SearchError::Backend(format!("{action}: {error}")))
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
            document("wiki/topics/seed.md", Some("Seed")),
            document("wiki/topics/outbound.md", Some("Outbound")),
            document("wiki/topics/backlink.md", Some("Backlink")),
        ];
        let links = vec![
            link("wiki/topics/seed.md", "wiki/topics/outbound.md"),
            link("wiki/topics/backlink.md", "Seed"),
            link("wiki/topics/outbound.md", "https://example.com"),
        ];

        let ranked = rank_link_neighborhood(
            &documents,
            &links,
            &[PathBuf::from("wiki/topics/seed.md")],
            10,
        );

        assert_eq!(ranked.len(), 2);
        assert_eq!(ranked[0], (PathBuf::from("wiki/topics/outbound.md"), 1.0));
        assert_eq!(ranked[1], (PathBuf::from("wiki/topics/backlink.md"), 0.8));
    }

    #[test]
    fn rank_link_neighborhood_filters_non_searchable_before_truncating() {
        let documents = vec![
            document("wiki/topics/seed.md", Some("Seed")),
            document("meta/high.md", Some("High")),
            document("wiki/topics/low.md", Some("Low")),
        ];
        let links = vec![
            link("wiki/topics/seed.md", "meta/high.md"),
            link("wiki/topics/low.md", "Seed"),
        ];

        let ranked = rank_link_neighborhood(
            &documents,
            &links,
            &[PathBuf::from("wiki/topics/seed.md")],
            1,
        );

        assert_eq!(ranked, vec![(PathBuf::from("wiki/topics/low.md"), 0.8)]);
    }

    #[test]
    fn rank_link_neighborhood_resolves_targets_relative_to_source() {
        let documents = vec![
            document("wiki/topics/seed.md", Some("Seed")),
            document("wiki/topics/outbound.md", Some("Outbound")),
        ];
        let links = vec![link("wiki/topics/seed.md", "outbound.md")];

        let ranked = rank_link_neighborhood(
            &documents,
            &links,
            &[PathBuf::from("wiki/topics/seed.md")],
            10,
        );

        assert_eq!(
            ranked,
            vec![(PathBuf::from("wiki/topics/outbound.md"), 1.0)]
        );
    }

    #[test]
    fn graph_boost_cap_error_reports_capped_rows() {
        assert!(graph_boost_cap_error("document", 10, 10).is_none());
        assert!(matches!(
            graph_boost_cap_error("document", 11, 10),
            Some(SearchError::Backend(message)) if message.contains("document count 11")
        ));
        assert!(graph_boost_cap_error("link", 11, 10).is_some());
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
