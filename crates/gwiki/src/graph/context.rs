use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use crate::graph::{WikiGraphCodeEdge, WikiGraphFacts, WikiGraphLinkTarget};
use crate::search::SearchScope;

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct GraphContextOptions {
    pub degraded_sources: Vec<String>,
}

impl GraphContextOptions {
    pub fn available() -> Self {
        Self::default()
    }

    pub fn degraded(degraded_sources: Vec<String>) -> Self {
        Self { degraded_sources }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct GraphContextPack {
    pub command: &'static str,
    pub scope: GraphContextScope,
    pub degradation: GraphContextDegradation,
    pub warnings: Vec<GraphContextWarning>,
    pub neighborhoods: Vec<GraphContextNeighborhood>,
    pub recommendations: Vec<GraphContextRecommendation>,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct GraphContextScope {
    pub kind: String,
    pub id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct GraphContextDegradation {
    pub degraded: bool,
    pub degraded_sources: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct GraphContextWarning {
    pub kind: &'static str,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub path: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct GraphContextNeighborhood {
    pub path: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    pub neighbors: Vec<GraphContextNeighbor>,
    pub doc_links: Vec<GraphContextDocLink>,
    pub citations: Vec<String>,
    pub calls: Vec<GraphContextCodeEdge>,
    pub imports: Vec<GraphContextCodeEdge>,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct GraphContextNeighbor {
    pub path: String,
    pub direction: &'static str,
    pub raw_target: String,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct GraphContextDocLink {
    pub source: String,
    pub target: String,
    pub raw_target: String,
    pub status: &'static str,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct GraphContextCodeEdge {
    pub source: String,
    pub target: String,
    pub kind: String,
    pub direction: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub line: Option<usize>,
    pub provenance: String,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct GraphContextRecommendation {
    pub path: String,
    pub reason: String,
}

pub fn build_context_pack(
    facts: &WikiGraphFacts,
    options: GraphContextOptions,
) -> GraphContextPack {
    let scope = context_scope(facts);
    let document_titles = facts
        .documents
        .iter()
        .map(|document| (document.path.clone(), document.title.clone()))
        .collect::<BTreeMap<_, _>>();
    let citations_by_doc = citations_by_document(facts);
    let mut warnings = degradation_warnings(&options.degraded_sources);
    warnings.extend(stale_link_warnings(facts));
    warnings.extend(audit_warnings(&document_titles, &citations_by_doc));

    let neighborhoods = document_titles
        .iter()
        .map(|(path, title)| GraphContextNeighborhood {
            path: display_path(path),
            title: title.clone(),
            neighbors: neighbors_for_path(facts, path),
            doc_links: doc_links_for_path(facts, path),
            citations: citations_by_doc
                .get(path)
                .map(|citations| citations.iter().cloned().collect())
                .unwrap_or_default(),
            calls: code_calls_for_path(facts, path),
            imports: code_imports_for_path(facts, path),
        })
        .collect::<Vec<_>>();

    GraphContextPack {
        command: "graph-context",
        scope,
        degradation: GraphContextDegradation {
            degraded: !options.degraded_sources.is_empty(),
            degraded_sources: options.degraded_sources,
        },
        warnings,
        recommendations: recommendations(facts, &citations_by_doc),
        neighborhoods,
    }
}

fn context_scope(facts: &WikiGraphFacts) -> GraphContextScope {
    let scope = facts
        .documents
        .first()
        .map(|document| &document.scope)
        .or_else(|| facts.links.first().map(|link| &link.scope))
        .or_else(|| facts.sources.first().map(|source| &source.scope));
    match scope {
        Some(scope) => GraphContextScope {
            kind: scope.scope_kind().to_string(),
            id: scope.scope_value().to_string(),
        },
        None => GraphContextScope {
            kind: SearchScope::project("unknown").scope_kind().to_string(),
            id: "unknown".to_string(),
        },
    }
}

fn citations_by_document(facts: &WikiGraphFacts) -> BTreeMap<PathBuf, BTreeSet<String>> {
    let mut citations = BTreeMap::<PathBuf, BTreeSet<String>>::new();
    for source in &facts.sources {
        citations
            .entry(source.document_path.clone())
            .or_default()
            .insert(display_path(&source.source_path));
    }
    citations
}

fn degradation_warnings(degraded_sources: &[String]) -> Vec<GraphContextWarning> {
    degraded_sources
        .iter()
        .map(|source| GraphContextWarning {
            kind: "degradation",
            message: format!("{source} is unavailable; graph-context returned wiki-only context"),
            path: None,
        })
        .collect()
}

fn stale_link_warnings(facts: &WikiGraphFacts) -> Vec<GraphContextWarning> {
    facts
        .links
        .iter()
        .filter_map(|link| match &link.target {
            WikiGraphLinkTarget::Resolved(_) => None,
            WikiGraphLinkTarget::Unresolved(target) => Some(GraphContextWarning {
                kind: "stale",
                message: format!("unresolved wiki link target {target}"),
                path: Some(display_path(&link.source_path)),
            }),
        })
        .collect()
}

fn audit_warnings(
    document_titles: &BTreeMap<PathBuf, Option<String>>,
    citations_by_doc: &BTreeMap<PathBuf, BTreeSet<String>>,
) -> Vec<GraphContextWarning> {
    document_titles
        .keys()
        .filter(|path| !citations_by_doc.contains_key(*path))
        .map(|path| GraphContextWarning {
            kind: "audit",
            message: "document has no source citation in the wiki index".to_string(),
            path: Some(display_path(path)),
        })
        .collect()
}

fn neighbors_for_path(facts: &WikiGraphFacts, path: &Path) -> Vec<GraphContextNeighbor> {
    let mut neighbors = Vec::new();
    for link in &facts.links {
        if link.source_path == path {
            if let WikiGraphLinkTarget::Resolved(target) = &link.target {
                neighbors.push(GraphContextNeighbor {
                    path: display_path(target),
                    direction: "outgoing",
                    raw_target: link.raw_target.clone(),
                });
            }
        } else if let WikiGraphLinkTarget::Resolved(target) = &link.target
            && target == path
        {
            neighbors.push(GraphContextNeighbor {
                path: display_path(&link.source_path),
                direction: "incoming",
                raw_target: link.raw_target.clone(),
            });
        }
    }
    neighbors.sort_by(|left, right| {
        left.path
            .cmp(&right.path)
            .then_with(|| left.direction.cmp(right.direction))
            .then_with(|| left.raw_target.cmp(&right.raw_target))
    });
    neighbors
}

fn doc_links_for_path(facts: &WikiGraphFacts, path: &Path) -> Vec<GraphContextDocLink> {
    let mut links = facts
        .links
        .iter()
        .filter(|link| link.source_path == path)
        .map(|link| match &link.target {
            WikiGraphLinkTarget::Resolved(target) => GraphContextDocLink {
                source: display_path(&link.source_path),
                target: display_path(target),
                raw_target: link.raw_target.clone(),
                status: "resolved",
            },
            WikiGraphLinkTarget::Unresolved(target) => GraphContextDocLink {
                source: display_path(&link.source_path),
                target: target.clone(),
                raw_target: link.raw_target.clone(),
                status: "unresolved",
            },
        })
        .collect::<Vec<_>>();
    links.extend(facts.links.iter().filter_map(|link| match &link.target {
        WikiGraphLinkTarget::Resolved(target) if link.source_path != path && target == path => {
            Some(GraphContextDocLink {
                source: display_path(&link.source_path),
                target: display_path(path),
                raw_target: link.raw_target.clone(),
                status: "resolved",
            })
        }
        _ => None,
    }));
    links.sort_by(|left, right| {
        left.target
            .cmp(&right.target)
            .then_with(|| left.raw_target.cmp(&right.raw_target))
    });
    links
}

fn code_calls_for_path(facts: &WikiGraphFacts, path: &Path) -> Vec<GraphContextCodeEdge> {
    facts
        .code_edges
        .iter()
        .filter(|edge| edge.document_path == path && edge.kind != "imports")
        .map(graph_context_code_edge)
        .collect()
}

fn code_imports_for_path(facts: &WikiGraphFacts, path: &Path) -> Vec<GraphContextCodeEdge> {
    facts
        .code_edges
        .iter()
        .filter(|edge| edge.document_path == path && edge.kind == "imports")
        .map(graph_context_code_edge)
        .collect()
}

fn graph_context_code_edge(edge: &WikiGraphCodeEdge) -> GraphContextCodeEdge {
    GraphContextCodeEdge {
        source: edge.source.clone(),
        target: edge.target.clone(),
        kind: edge.kind.clone(),
        direction: edge.direction.clone(),
        line: edge.line,
        provenance: edge.provenance.clone(),
    }
}

fn recommendations(
    facts: &WikiGraphFacts,
    citations_by_doc: &BTreeMap<PathBuf, BTreeSet<String>>,
) -> Vec<GraphContextRecommendation> {
    let mut degree = BTreeMap::<PathBuf, usize>::new();
    for link in &facts.links {
        if let WikiGraphLinkTarget::Resolved(target) = &link.target {
            *degree.entry(link.source_path.clone()).or_default() += 1;
            *degree.entry(target.clone()).or_default() += 1;
        }
    }

    let mut ranked = degree.into_iter().collect::<Vec<_>>();
    ranked.sort_by(|left, right| right.1.cmp(&left.1).then_with(|| left.0.cmp(&right.0)));

    let mut seen = BTreeSet::new();
    let mut recommendations = Vec::new();
    for (path, _) in ranked.into_iter().take(5) {
        if seen.insert(path.clone()) {
            recommendations.push(GraphContextRecommendation {
                path: display_path(&path),
                reason: "inspect linked wiki neighborhood".to_string(),
            });
        }
    }

    for source in &facts.links {
        if let WikiGraphLinkTarget::Unresolved(target) = &source.target {
            let path = display_path(&source.source_path);
            if seen.insert(source.source_path.clone()) {
                recommendations.push(GraphContextRecommendation {
                    path,
                    reason: format!("resolve stale wiki link {target}"),
                });
            }
        }
    }

    for document in &facts.documents {
        if !citations_by_doc.contains_key(&document.path) && seen.insert(document.path.clone()) {
            recommendations.push(GraphContextRecommendation {
                path: display_path(&document.path),
                reason: "inspect or add source citation".to_string(),
            });
        }
    }

    recommendations
}

fn display_path(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use crate::graph::{
        WikiGraphCodeEdge, WikiGraphDocument, WikiGraphFacts, WikiGraphLink, WikiGraphLinkTarget,
        WikiGraphSource,
    };
    use crate::search::SearchScope;

    #[test]
    fn graph_context_json_includes_neighborhoods_and_empty_code_edges() {
        let scope = SearchScope::project("project-1");
        let facts = WikiGraphFacts {
            documents: vec![
                doc(scope.clone(), "wiki/a.md", Some("Alpha")),
                doc(scope.clone(), "wiki/b.md", Some("Beta")),
                doc(scope.clone(), "wiki/c.md", None),
            ],
            links: vec![
                resolved_link(scope.clone(), "wiki/a.md", "Beta", "wiki/b.md"),
                resolved_link(scope.clone(), "wiki/b.md", "wiki/c.md", "wiki/c.md"),
                unresolved_link(scope.clone(), "wiki/a.md", "Missing"),
            ],
            sources: vec![source(scope.clone(), "raw/a.md", "wiki/a.md")],
            code_edges: Vec::new(),
        };

        let pack = super::build_context_pack(
            &facts,
            super::GraphContextOptions::degraded(vec!["falkordb_unavailable".to_string()]),
        );
        let json = serde_json::to_value(&pack).expect("serialize pack");

        assert_eq!(json["command"], "graph-context");
        assert_eq!(json["degradation"]["degraded"], true);
        assert_eq!(
            json["degradation"]["degraded_sources"],
            serde_json::json!(["falkordb_unavailable"])
        );
        assert!(
            json["warnings"]
                .as_array()
                .expect("warnings")
                .iter()
                .any(|warning| warning["kind"] == "stale")
        );
        assert!(
            json["warnings"]
                .as_array()
                .expect("warnings")
                .iter()
                .any(|warning| warning["kind"] == "audit")
        );
        assert!(
            json["warnings"]
                .as_array()
                .expect("warnings")
                .iter()
                .any(|warning| warning["kind"] == "degradation")
        );

        let neighborhoods = json["neighborhoods"].as_array().expect("neighborhoods");
        let alpha = neighborhoods
            .iter()
            .find(|node| node["path"] == "wiki/a.md")
            .expect("alpha neighborhood");
        assert_eq!(alpha["calls"], serde_json::json!([]));
        assert_eq!(alpha["imports"], serde_json::json!([]));
        assert_eq!(alpha["citations"], serde_json::json!(["raw/a.md"]));
        assert!(
            alpha["neighbors"]
                .as_array()
                .expect("neighbors")
                .iter()
                .any(|neighbor| neighbor["path"] == "wiki/b.md"
                    && neighbor["direction"] == "outgoing"
                    && neighbor["raw_target"] == "Beta")
        );
        assert!(
            alpha["doc_links"]
                .as_array()
                .expect("doc links")
                .iter()
                .any(|link| link["target"] == "wiki/b.md" && link["status"] == "resolved")
        );
        assert!(
            alpha["doc_links"]
                .as_array()
                .expect("doc links")
                .iter()
                .any(|link| link["target"] == "Missing" && link["status"] == "unresolved")
        );

        assert!(
            json["recommendations"]
                .as_array()
                .expect("recommendations")
                .iter()
                .any(|recommendation| recommendation["path"] == "wiki/b.md")
        );
    }

    #[test]
    fn ask_unified_graph_context_merges_wiki_and_code_edges() {
        let scope = SearchScope::project("project-1");
        let facts = WikiGraphFacts {
            documents: vec![
                doc(scope.clone(), "wiki/architecture.md", Some("Architecture")),
                doc(
                    scope.clone(),
                    "code/files/src/handler.rs.md",
                    Some("src/handler.rs"),
                ),
            ],
            links: vec![resolved_link(
                scope.clone(),
                "wiki/architecture.md",
                "Handler",
                "code/files/src/handler.rs.md",
            )],
            sources: vec![source(
                scope.clone(),
                "src/handler.rs",
                "code/files/src/handler.rs.md",
            )],
            code_edges: vec![
                code_edge(
                    scope.clone(),
                    "code/files/src/handler.rs.md",
                    "src/handler.rs:handle",
                    "src/router.rs:route",
                    "calls",
                    "outgoing",
                    Some(42),
                ),
                code_edge(
                    scope.clone(),
                    "code/files/src/handler.rs.md",
                    "src/main.rs:main",
                    "src/handler.rs:handle",
                    "callers",
                    "incoming",
                    Some(7),
                ),
                code_edge(
                    scope.clone(),
                    "code/files/src/handler.rs.md",
                    "src/handler.rs",
                    "crate::router",
                    "imports",
                    "outgoing",
                    None,
                ),
            ],
        };

        let pack = super::build_context_pack(&facts, super::GraphContextOptions::available());
        let json = serde_json::to_value(&pack).expect("serialize pack");
        let neighborhoods = json["neighborhoods"].as_array().expect("neighborhoods");
        let code = neighborhoods
            .iter()
            .find(|node| node["path"] == "code/files/src/handler.rs.md")
            .expect("code neighborhood");

        assert!(
            code["calls"]
                .as_array()
                .expect("calls")
                .iter()
                .any(|edge| edge["kind"] == "calls"
                    && edge["direction"] == "outgoing"
                    && edge["line"] == 42
                    && edge["provenance"] == "shared_code_graph")
        );
        assert!(
            code["calls"]
                .as_array()
                .expect("calls")
                .iter()
                .any(|edge| edge["kind"] == "callers" && edge["direction"] == "incoming")
        );
        assert!(
            code["imports"]
                .as_array()
                .expect("imports")
                .iter()
                .any(|edge| edge["target"] == "crate::router"
                    && edge["provenance"] == "shared_code_graph")
        );
        assert!(
            code["doc_links"]
                .as_array()
                .expect("doc links")
                .iter()
                .any(|link| link["source"] == "wiki/architecture.md"
                    && link["target"] == "code/files/src/handler.rs.md")
        );
    }

    fn doc(scope: SearchScope, path: &str, title: Option<&str>) -> WikiGraphDocument {
        WikiGraphDocument {
            scope,
            path: PathBuf::from(path),
            title: title.map(str::to_string),
        }
    }

    fn source(scope: SearchScope, source_path: &str, document_path: &str) -> WikiGraphSource {
        WikiGraphSource {
            scope,
            source_path: PathBuf::from(source_path),
            document_path: PathBuf::from(document_path),
        }
    }

    fn resolved_link(
        scope: SearchScope,
        source_path: &str,
        raw_target: &str,
        target_path: &str,
    ) -> WikiGraphLink {
        WikiGraphLink {
            scope,
            source_path: PathBuf::from(source_path),
            raw_target: raw_target.to_string(),
            target: WikiGraphLinkTarget::Resolved(PathBuf::from(target_path)),
        }
    }

    fn unresolved_link(scope: SearchScope, source_path: &str, raw_target: &str) -> WikiGraphLink {
        WikiGraphLink {
            scope,
            source_path: PathBuf::from(source_path),
            raw_target: raw_target.to_string(),
            target: WikiGraphLinkTarget::Unresolved(raw_target.to_string()),
        }
    }

    fn code_edge(
        scope: SearchScope,
        document_path: &str,
        source: &str,
        target: &str,
        kind: &str,
        direction: &str,
        line: Option<usize>,
    ) -> WikiGraphCodeEdge {
        WikiGraphCodeEdge {
            scope,
            document_path: PathBuf::from(document_path),
            source: source.to_string(),
            target: target.to_string(),
            kind: kind.to_string(),
            direction: direction.to_string(),
            line,
            provenance: "shared_code_graph".to_string(),
        }
    }
}
