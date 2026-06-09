use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Component, Path, PathBuf};

use gobby_core::config::FalkorConfig;
use gobby_core::degradation::DegradationKind;
use gobby_core::falkor::{GraphClient, Row};
use postgres::Client;
use serde_json::Value;

use crate::WikiError;
use crate::graph::{
    GraphStatement, WikiGraphCodeEdge, WikiGraphDocument, WikiGraphFacts, WikiGraphLink,
    WikiGraphLinkTarget, WikiGraphSource, graph_write_statements,
};
use crate::search::{SearchError, SearchScope};
use crate::support::config::SharedCodeGraphLimits;
use crate::support::text::slugify;

pub const FALKORDB_GRAPH_NAME: &str = "gobby_wiki";
pub(crate) const SHARED_CODE_GRAPH_TRUNCATED_SOURCE: &str = "shared_code_graph_truncated";
const CODE_FALKORDB_GRAPH_NAME: &str = "gobby_code";
const CODE_GRAPH_PROVENANCE: &str = "shared_code_graph";
const CODE_CALL_EDGE_TRUNCATION_COMPONENT: &str = "code_call_edges";
const CODE_IMPORT_EDGE_TRUNCATION_COMPONENT: &str = "code_import_edges";
const CODE_TOTAL_EDGE_TRUNCATION_COMPONENT: &str = "code_edges";
const MAX_TOTAL_CODE_EDGES: usize = 1000;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct SharedCodeGraphTruncation {
    pub(crate) components: Vec<String>,
}

impl SharedCodeGraphTruncation {
    fn from_components(components: BTreeSet<String>) -> Self {
        Self {
            components: components.into_iter().collect(),
        }
    }

    pub(crate) fn is_truncated(&self) -> bool {
        !self.components.is_empty()
    }
}

#[derive(Clone, Debug, Default)]
pub(crate) struct SharedCodeGraphEdges {
    pub(crate) edges: Vec<WikiGraphCodeEdge>,
    pub(crate) truncation: SharedCodeGraphTruncation,
}

struct LimitedCodeGraphEdges {
    edges: Vec<WikiGraphCodeEdge>,
    truncated: bool,
}

pub(crate) struct GraphBoostData {
    pub documents: Vec<crate::search::graph_boost::GraphBoostDocument>,
    pub links: Vec<crate::search::graph_boost::GraphBoostLink>,
    pub degradation: Option<DegradationKind>,
}

pub(crate) fn sync_scope_from_postgres(
    conn: &mut Client,
    scope: &SearchScope,
    config: &FalkorConfig,
) -> Result<(), WikiError> {
    let facts = load_wiki_graph_facts(conn, scope)?;
    let mut client = GraphClient::from_config(config, FALKORDB_GRAPH_NAME).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to FalkorDB for gwiki graph sync: {error}"),
        }
    })?;
    clear_scope(&mut client, scope).map_err(graph_sync_error)?;
    for statement in graph_write_statements(&facts) {
        execute_statement(&mut client, statement).map_err(graph_sync_error)?;
    }
    Ok(())
}

pub(crate) fn load_graph_boost_data(
    client: &mut GraphClient,
    scope: &SearchScope,
    document_limit: i64,
    link_limit: i64,
) -> Result<GraphBoostData, SearchError> {
    let documents = query_documents(client, scope, document_limit)?;
    let links = query_links(client, scope, link_limit)?;
    let mut capped = Vec::new();
    if documents.capped {
        capped.push(format!("documents>{document_limit}"));
    }
    if links.capped {
        capped.push(format!("links>{link_limit}"));
    }
    let degradation = partial_graph_degradation(&capped);
    if let Some(degradation) = &degradation {
        log::warn!("loaded partial FalkorDB wiki graph boost data: {degradation:?}");
    }
    Ok(GraphBoostData {
        documents: documents.items,
        links: links.items,
        degradation,
    })
}

pub(crate) fn load_code_graph_edges(
    config: &FalkorConfig,
    project_id: &str,
    documents: &[WikiGraphDocument],
    limits: SharedCodeGraphLimits,
) -> anyhow::Result<SharedCodeGraphEdges> {
    let mut client = GraphClient::from_config(config, CODE_FALKORDB_GRAPH_NAME)?;
    let code_documents = documents
        .iter()
        .filter_map(|document| {
            code_doc_source_path(&document.path)
                .map(|file_path| (document.scope.clone(), document.path.clone(), file_path))
        })
        .collect::<Vec<_>>();
    let mut edges = Vec::new();
    let mut truncated_components = BTreeSet::new();
    let mut remaining_edges = MAX_TOTAL_CODE_EDGES;
    for (scope, document_path, file_path) in code_documents {
        let Some(call_limit) = remaining_code_edge_limit(limits.call_edge_limit, remaining_edges)
        else {
            truncated_components.insert(truncation_component(
                CODE_TOTAL_EDGE_TRUNCATION_COMPONENT,
                MAX_TOTAL_CODE_EDGES,
            ));
            break;
        };
        let call_edges = code_call_edges(
            &mut client,
            project_id,
            &scope,
            &document_path,
            &file_path,
            call_limit,
        )?;
        if call_edges.truncated {
            record_code_edge_truncation(
                &mut truncated_components,
                CODE_CALL_EDGE_TRUNCATION_COMPONENT,
                limits.call_edge_limit,
                call_limit,
            );
        }
        remaining_edges = remaining_edges.saturating_sub(call_edges.edges.len());
        edges.extend(call_edges.edges);
        if remaining_edges == 0 {
            truncated_components.insert(truncation_component(
                CODE_TOTAL_EDGE_TRUNCATION_COMPONENT,
                MAX_TOTAL_CODE_EDGES,
            ));
            break;
        }

        let Some(import_limit) =
            remaining_code_edge_limit(limits.import_edge_limit, remaining_edges)
        else {
            truncated_components.insert(truncation_component(
                CODE_TOTAL_EDGE_TRUNCATION_COMPONENT,
                MAX_TOTAL_CODE_EDGES,
            ));
            break;
        };
        let import_edges = code_import_edges(
            &mut client,
            project_id,
            &scope,
            &document_path,
            &file_path,
            import_limit,
        )?;
        if import_edges.truncated {
            record_code_edge_truncation(
                &mut truncated_components,
                CODE_IMPORT_EDGE_TRUNCATION_COMPONENT,
                limits.import_edge_limit,
                import_limit,
            );
        }
        remaining_edges = remaining_edges.saturating_sub(import_edges.edges.len());
        edges.extend(import_edges.edges);
        if remaining_edges == 0 {
            truncated_components.insert(truncation_component(
                CODE_TOTAL_EDGE_TRUNCATION_COMPONENT,
                MAX_TOTAL_CODE_EDGES,
            ));
            break;
        }
    }
    Ok(SharedCodeGraphEdges {
        edges,
        truncation: SharedCodeGraphTruncation::from_components(truncated_components),
    })
}

fn code_call_edges(
    client: &mut GraphClient,
    project_id: &str,
    scope: &SearchScope,
    document_path: &Path,
    file_path: &str,
    limit: usize,
) -> anyhow::Result<LimitedCodeGraphEdges> {
    let query = code_call_edges_query();
    let mut rows = client.query(
        query,
        Some(code_edge_query_params(project_id, file_path, limit)?),
    )?;
    let truncated = truncate_to_limit(&mut rows, limit);
    Ok(LimitedCodeGraphEdges {
        edges: rows
            .into_iter()
            .map(|row| {
                let source_file = optional_row_string(&row, "source_file_path")
                    .unwrap_or_else(|| file_path.to_string());
                let source_name = optional_row_string(&row, "source_name")
                    .unwrap_or_else(|| "unknown".to_string());
                let target_file = optional_row_string(&row, "target_file_path")
                    .unwrap_or_else(|| "external".to_string());
                let target_name = optional_row_string(&row, "target_name")
                    .unwrap_or_else(|| "unknown".to_string());
                let incoming = target_file == file_path && source_file != file_path;
                WikiGraphCodeEdge {
                    scope: scope.clone(),
                    document_path: document_path.to_path_buf(),
                    source: code_endpoint(&source_file, &source_name),
                    target: code_endpoint(&target_file, &target_name),
                    kind: if incoming { "callers" } else { "calls" }.to_string(),
                    direction: if incoming { "incoming" } else { "outgoing" }.to_string(),
                    line: optional_row_usize(&row, "line"),
                    provenance: CODE_GRAPH_PROVENANCE.to_string(),
                }
            })
            .collect(),
        truncated,
    })
}

fn code_call_edges_query() -> &'static str {
    "\
        MATCH (source:CodeSymbol {project: $project})-[r:CALLS]->(target {project: $project}) \
        WHERE source.file_path = $path OR (target:CodeSymbol AND target.file_path = $path) \
        RETURN source.file_path AS source_file_path, source.name AS source_name, \
               target.file_path AS target_file_path, target.name AS target_name, r.line AS line \
        LIMIT toInteger($limit)"
}

fn code_import_edges(
    client: &mut GraphClient,
    project_id: &str,
    scope: &SearchScope,
    document_path: &Path,
    file_path: &str,
    limit: usize,
) -> anyhow::Result<LimitedCodeGraphEdges> {
    let query = code_import_edges_query();
    let mut rows = client.query(
        query,
        Some(code_edge_query_params(project_id, file_path, limit)?),
    )?;
    let truncated = truncate_to_limit(&mut rows, limit);
    Ok(LimitedCodeGraphEdges {
        edges: rows
            .into_iter()
            .map(|row| {
                let source_file = optional_row_string(&row, "source_file_path")
                    .unwrap_or_else(|| file_path.to_string());
                let target_name = optional_row_string(&row, "target_name")
                    .unwrap_or_else(|| "unknown".to_string());
                WikiGraphCodeEdge {
                    scope: scope.clone(),
                    document_path: document_path.to_path_buf(),
                    source: source_file,
                    target: target_name,
                    kind: "imports".to_string(),
                    direction: "outgoing".to_string(),
                    line: None,
                    provenance: CODE_GRAPH_PROVENANCE.to_string(),
                }
            })
            .collect(),
        truncated,
    })
}

fn code_import_edges_query() -> &'static str {
    "\
        MATCH (file:CodeFile {path: $path, project: $project})-[r:IMPORTS]->(module:CodeModule {project: $project}) \
        RETURN file.path AS source_file_path, module.name AS target_name \
        LIMIT toInteger($limit)"
}

fn code_edge_query_params(
    project_id: &str,
    file_path: &str,
    limit: usize,
) -> anyhow::Result<HashMap<String, String>> {
    Ok(HashMap::from([
        ("project".to_string(), project_id.to_string()),
        ("path".to_string(), file_path.to_string()),
        ("limit".to_string(), sentinel_limit(limit)?.to_string()),
    ]))
}

fn sentinel_limit(limit: usize) -> anyhow::Result<usize> {
    limit
        .checked_add(1)
        .ok_or_else(|| anyhow::anyhow!("shared code graph edge limit is too large: {limit}"))
}

fn truncate_to_limit<T>(rows: &mut Vec<T>, limit: usize) -> bool {
    let truncated = rows.len() > limit;
    if truncated {
        rows.truncate(limit);
    }
    truncated
}

fn remaining_code_edge_limit(configured_limit: usize, remaining_edges: usize) -> Option<usize> {
    (remaining_edges > 0).then_some(configured_limit.min(remaining_edges))
}

fn record_code_edge_truncation(
    components: &mut BTreeSet<String>,
    component: &str,
    configured_limit: usize,
    query_limit: usize,
) {
    let (component, limit) = if query_limit < configured_limit {
        (CODE_TOTAL_EDGE_TRUNCATION_COMPONENT, MAX_TOTAL_CODE_EDGES)
    } else {
        (component, configured_limit)
    };
    components.insert(truncation_component(component, limit));
}

fn truncation_component(component: &str, limit: usize) -> String {
    format!("{component}>{limit}")
}

fn clear_scope(client: &mut GraphClient, scope: &SearchScope) -> anyhow::Result<()> {
    client.query(
        "MATCH (node)
         WHERE (node:WikiDoc OR node:WikiSource OR node:WikiTarget)
           AND node.scope_kind = $scope_kind
           AND node.scope_id = $scope_id
         DETACH DELETE node",
        Some(scope_params(scope)),
    )?;
    Ok(())
}

fn execute_statement(client: &mut GraphClient, statement: GraphStatement) -> anyhow::Result<()> {
    client.query(&statement.cypher, None)?;
    Ok(())
}

fn query_documents(
    client: &mut GraphClient,
    scope: &SearchScope,
    limit: i64,
) -> Result<LimitedQuery<crate::search::graph_boost::GraphBoostDocument>, SearchError> {
    let rows = query_limited(
        client,
        scope,
        "MATCH (doc:WikiDoc {scope_kind: $scope_kind, scope_id: $scope_id})
         RETURN doc.path AS path, doc.title AS title
         ORDER BY path",
        "document",
        limit,
    )?;
    let items = rows
        .items
        .into_iter()
        .map(|row| {
            let path = row_string(&row, "path")?;
            Ok(crate::search::graph_boost::GraphBoostDocument {
                path: PathBuf::from(path),
                title: optional_row_string(&row, "title"),
            })
        })
        .collect::<Result<Vec<_>, SearchError>>()?;
    Ok(LimitedQuery {
        items,
        capped: rows.capped,
    })
}

fn query_links(
    client: &mut GraphClient,
    scope: &SearchScope,
    limit: i64,
) -> Result<LimitedQuery<crate::search::graph_boost::GraphBoostLink>, SearchError> {
    let rows = query_limited(
        client,
        scope,
        "MATCH (source:WikiDoc {scope_kind: $scope_kind, scope_id: $scope_id})
             -[:WIKI_LINKS_TO]->
             (target:WikiDoc {scope_kind: $scope_kind, scope_id: $scope_id})
         RETURN source.path AS path, target.path AS target_path
         ORDER BY path, target_path",
        "link",
        limit,
    )?;
    let items = rows
        .items
        .into_iter()
        .map(|row| {
            let source_path = row_string(&row, "path")?;
            let target_path = row_string(&row, "target_path")?;
            Ok(crate::search::graph_boost::GraphBoostLink {
                source_path: PathBuf::from(source_path),
                target_path,
            })
        })
        .collect::<Result<Vec<_>, SearchError>>()?;
    Ok(LimitedQuery {
        items,
        capped: rows.capped,
    })
}

struct LimitedQuery<T> {
    items: Vec<T>,
    capped: bool,
}

fn query_limited(
    client: &mut GraphClient,
    scope: &SearchScope,
    base_query: &str,
    component: &'static str,
    limit: i64,
) -> Result<LimitedQuery<Row>, SearchError> {
    let limit = limit.max(0);
    let fetch_limit = usize::try_from(limit)
        .map_err(|_| SearchError::Backend(format!("invalid graph {component} limit {limit}")))?
        .saturating_add(1);
    let query = format!("{base_query}\nLIMIT {fetch_limit}");
    let mut rows = client
        .query(&query, Some(scope_params(scope)))
        .map_err(|error| {
            SearchError::Backend(format!("query FalkorDB wiki {component}s: {error}"))
        })?;
    let capped = rows.len() == fetch_limit && fetch_limit > 0;
    if let Ok(limit) = usize::try_from(limit)
        && rows.len() > limit
    {
        // Query fetches one sentinel row to detect truncation; drop it before
        // callers build graph facts so the result never exceeds the contract.
        rows.truncate(limit);
    }
    Ok(LimitedQuery {
        items: rows,
        capped,
    })
}

fn partial_graph_degradation(capped: &[String]) -> Option<DegradationKind> {
    if capped.is_empty() {
        return None;
    }
    Some(DegradationKind::PartialData {
        component: "gwiki_graph".to_string(),
        message: format!(
            "FalkorDB wiki graph exceeded configured caps ({}); graph boost used capped data",
            capped.join(", ")
        ),
    })
}

pub(crate) fn load_wiki_graph_facts(
    conn: &mut Client,
    scope: &SearchScope,
) -> Result<WikiGraphFacts, WikiError> {
    let scope_kind = scope.scope_kind().to_string();
    let scope_id = scope.scope_value().to_string();
    let document_rows = conn
        .query(
            "SELECT path, title
             FROM gwiki_documents
             WHERE scope_kind = $1 AND scope_id = $2
             ORDER BY path",
            &[&scope_kind, &scope_id],
        )
        .map_err(|error| WikiError::Config {
            detail: format!("failed to load gwiki documents for FalkorDB sync: {error}"),
        })?;
    let documents = document_rows
        .into_iter()
        .map(|row| WikiGraphDocument {
            scope: scope.clone(),
            path: PathBuf::from(row.get::<_, String>("path")),
            title: row.get::<_, Option<String>>("title"),
        })
        .collect::<Vec<_>>();

    let document_paths = documents
        .iter()
        .map(|document| document.path.clone())
        .collect::<BTreeSet<_>>();
    let slug_targets = slug_target_map(&documents);

    let link_rows = conn
        .query(
            "SELECT path, target_path
             FROM gwiki_links
             WHERE scope_kind = $1 AND scope_id = $2
             ORDER BY path, target_path",
            &[&scope_kind, &scope_id],
        )
        .map_err(|error| WikiError::Config {
            detail: format!("failed to load gwiki links for FalkorDB sync: {error}"),
        })?;
    let links = link_rows
        .into_iter()
        .filter_map(|row| {
            let source_path = PathBuf::from(row.get::<_, String>("path"));
            let raw_target = row.get::<_, String>("target_path");
            resolve_graph_target(&raw_target, &source_path, &document_paths, &slug_targets).map(
                |target| WikiGraphLink {
                    scope: scope.clone(),
                    source_path,
                    raw_target,
                    target,
                },
            )
        })
        .collect::<Vec<_>>();

    let source_rows = conn
        .query(
            "SELECT path, document_path
             FROM gwiki_sources
             WHERE scope_kind = $1 AND scope_id = $2
             ORDER BY path, document_path",
            &[&scope_kind, &scope_id],
        )
        .map_err(|error| WikiError::Config {
            detail: format!("failed to load gwiki sources for FalkorDB sync: {error}"),
        })?;
    let sources = source_rows
        .into_iter()
        .map(|row| WikiGraphSource {
            scope: scope.clone(),
            source_path: PathBuf::from(row.get::<_, String>("path")),
            document_path: PathBuf::from(row.get::<_, String>("document_path")),
        })
        .collect::<Vec<_>>();

    Ok(WikiGraphFacts {
        documents,
        links,
        sources,
        code_edges: Vec::new(),
    })
}

fn resolve_graph_target(
    raw_target: &str,
    source_path: &Path,
    document_paths: &BTreeSet<PathBuf>,
    slug_targets: &BTreeMap<String, PathBuf>,
) -> Option<WikiGraphLinkTarget> {
    let trimmed = raw_target.trim();
    if is_external_target(trimmed) {
        return None;
    }
    let normalized = trimmed
        .split('#')
        .next()
        .unwrap_or_default()
        .trim()
        .replace('\\', "/");
    if normalized.is_empty() {
        return None;
    }

    let lookup = resolve_relative_graph_path(&normalized, source_path);
    let direct = PathBuf::from(&lookup);
    if document_paths.contains(&direct) {
        return Some(WikiGraphLinkTarget::Resolved(direct));
    }

    let target_slug = slugify(lookup.strip_suffix(".md").unwrap_or(&lookup));
    if let Some(path) = slug_targets.get(&target_slug) {
        return Some(WikiGraphLinkTarget::Resolved(path.clone()));
    }

    Some(WikiGraphLinkTarget::Unresolved(lookup))
}

fn slug_target_map(documents: &[WikiGraphDocument]) -> BTreeMap<String, PathBuf> {
    documents
        .iter()
        .filter_map(|document| {
            let title = document.title.as_deref()?;
            Some((slugify(title), document.path.clone()))
        })
        .collect()
}

fn resolve_relative_graph_path(raw_target: &str, source_path: &Path) -> String {
    let normalized = raw_target.trim_start_matches('/');
    if raw_target.starts_with('/') || !is_path_like_target(normalized) {
        return normalized.to_string();
    }
    let raw_path = Path::new(normalized);
    let candidate = source_path
        .parent()
        .map(|parent| parent.join(raw_path))
        .unwrap_or_else(|| raw_path.to_path_buf());
    normalize_path(candidate)
        .to_string_lossy()
        .replace('\\', "/")
}

fn is_path_like_target(target: &str) -> bool {
    target.contains('/') || target.starts_with('.') || target.ends_with(".md")
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

fn code_doc_source_path(path: &Path) -> Option<String> {
    normalize_graph_path(path)
        .strip_prefix("code/files/")
        .and_then(|path| path.strip_suffix(".md"))
        .map(str::to_string)
}

fn code_endpoint(file_path: &str, symbol: &str) -> String {
    if symbol.is_empty() {
        file_path.to_string()
    } else {
        format!("{file_path}:{symbol}")
    }
}

fn normalize_graph_path(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

fn is_external_target(target: &str) -> bool {
    let lower = target.to_ascii_lowercase();
    lower.starts_with("http://")
        || lower.starts_with("https://")
        || lower.starts_with("mailto:")
        || lower.starts_with("//")
        || target.starts_with(r"\\")
        || lower.contains("://")
}

fn scope_params(scope: &SearchScope) -> HashMap<String, String> {
    HashMap::from([
        ("scope_kind".to_string(), scope.scope_kind().to_string()),
        ("scope_id".to_string(), scope.scope_value().to_string()),
    ])
}

fn row_string(row: &Row, key: &'static str) -> Result<String, SearchError> {
    optional_row_string(row, key)
        .ok_or_else(|| SearchError::Backend(format!("FalkorDB wiki graph row missing `{key}`")))
}

fn optional_row_string(row: &Row, key: &'static str) -> Option<String> {
    row.get(key)
        .and_then(Value::as_str)
        .map(ToString::to_string)
}

fn optional_row_usize(row: &Row, key: &'static str) -> Option<usize> {
    row.get(key)
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok())
}

#[cfg(test)]
fn cypher_string_literal(value: &str) -> String {
    format!("'{}'", escape_string_contents(value))
}

#[cfg(test)]
fn escape_string_contents(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '\\' => escaped.push_str(r"\\"),
            '\'' => escaped.push_str(r"\'"),
            '"' => escaped.push_str("\\\""),
            '\n' => escaped.push_str(r"\n"),
            '\r' => escaped.push_str(r"\r"),
            '\t' => escaped.push_str(r"\t"),
            '\u{0008}' => escaped.push_str(r"\b"),
            '\u{000C}' => escaped.push_str(r"\f"),
            ch if ch.is_control() => escaped.push_str(&format!(r"\u{:04X}", ch as u32)),
            ch => escaped.push(ch),
        }
    }
    escaped
}

fn graph_sync_error(error: anyhow::Error) -> WikiError {
    WikiError::Config {
        detail: format!("failed to sync gwiki graph to FalkorDB: {error}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::{
        self, MENTIONS_TARGET_REL, SUPPORTS_REL, WIKI_DOC_LABEL, WIKI_LINKS_TO_REL,
        WIKI_SOURCE_LABEL, WIKI_TARGET_LABEL,
    };

    #[test]
    fn falkordb_graph_name_is_wiki_owned() {
        assert_eq!(FALKORDB_GRAPH_NAME, "gobby_wiki");
        assert_ne!(FALKORDB_GRAPH_NAME, "gobby_code");
    }

    #[test]
    fn graph_resolution_keeps_unresolved_targets_and_skips_external() {
        let scope = SearchScope::topic("rust");
        let documents = vec![WikiGraphDocument {
            scope: scope.clone(),
            path: PathBuf::from("knowledge/topics/rust.md"),
            title: Some("Rust Async".to_string()),
        }];
        let document_paths = documents
            .iter()
            .map(|document| document.path.clone())
            .collect::<BTreeSet<_>>();
        let slug_targets = slug_target_map(&documents);
        assert_eq!(
            resolve_graph_target(
                "Rust Async",
                Path::new("knowledge/topics/source.md"),
                &document_paths,
                &slug_targets
            ),
            Some(graph::WikiGraphLinkTarget::Resolved(PathBuf::from(
                "knowledge/topics/rust.md"
            )))
        );
        assert_eq!(
            resolve_graph_target(
                "Missing Page",
                Path::new("knowledge/topics/source.md"),
                &document_paths,
                &slug_targets
            ),
            Some(graph::WikiGraphLinkTarget::Unresolved(
                "Missing Page".to_string()
            ))
        );
        assert!(
            resolve_graph_target(
                "https://example.test",
                Path::new("knowledge/topics/source.md"),
                &document_paths,
                &slug_targets
            )
            .is_none()
        );
    }

    #[test]
    fn graph_scope_params_are_raw_parameter_values() {
        let params = scope_params(&SearchScope::topic("rust'async"));

        assert_eq!(params.get("scope_kind").map(String::as_str), Some("topic"));
        assert_eq!(
            params.get("scope_id").map(String::as_str),
            Some("rust'async")
        );
    }

    #[test]
    fn ask_unified_graph_code_doc_source_path_maps_to_code_file() {
        assert_eq!(
            code_doc_source_path(Path::new("code/files/crates/gwiki/src/lib.rs.md")),
            Some("crates/gwiki/src/lib.rs".to_string())
        );
        assert_eq!(code_doc_source_path(Path::new("wiki/notes.md")), None);
    }

    #[test]
    fn cypher_string_literal_escapes_like_gcode() {
        assert_eq!(
            cypher_string_literal("a\"b\\c'd\n\r\t\u{0008}\u{000C}\u{001F}"),
            "'a\\\"b\\\\c\\'d\\n\\r\\t\\b\\f\\u001F'"
        );
    }

    #[test]
    fn partial_graph_degradation_reports_capped_components() {
        let degradation =
            partial_graph_degradation(&["documents>10".to_string(), "links>20".to_string()])
                .expect("partial data degradation");

        let DegradationKind::PartialData { component, message } = degradation else {
            panic!("expected partial data degradation");
        };
        assert_eq!(component, "gwiki_graph");
        assert!(message.contains("documents>10"));
        assert!(message.contains("links>20"));
    }

    #[test]
    fn code_edge_query_params_use_sentinel_limit_and_parameterized_queries() {
        let call_query = code_call_edges_query();
        let import_query = code_import_edges_query();

        assert!(call_query.contains("LIMIT toInteger($limit)"));
        assert!(import_query.contains("LIMIT toInteger($limit)"));
        assert!(!call_query.contains("LIMIT 200"));
        assert!(!import_query.contains("LIMIT 200"));

        let params = code_edge_query_params("project-1", "src/lib.rs", 7).expect("params");

        assert_eq!(params.get("limit").map(String::as_str), Some("8"));
    }

    #[test]
    fn truncation_components_name_capped_call_and_import_queries() {
        assert_eq!(
            truncation_component(CODE_CALL_EDGE_TRUNCATION_COMPONENT, 7),
            "code_call_edges>7"
        );
        assert_eq!(
            truncation_component(CODE_IMPORT_EDGE_TRUNCATION_COMPONENT, 9),
            "code_import_edges>9"
        );
    }

    #[test]
    fn code_edge_query_limit_respects_total_remaining_cap() {
        assert_eq!(
            remaining_code_edge_limit(200, MAX_TOTAL_CODE_EDGES),
            Some(200)
        );
        assert_eq!(remaining_code_edge_limit(200, 17), Some(17));
        assert_eq!(remaining_code_edge_limit(200, 0), None);

        let mut components = BTreeSet::new();
        record_code_edge_truncation(
            &mut components,
            CODE_CALL_EDGE_TRUNCATION_COMPONENT,
            200,
            17,
        );
        assert!(components.contains(&truncation_component(
            CODE_TOTAL_EDGE_TRUNCATION_COMPONENT,
            MAX_TOTAL_CODE_EDGES
        )));
    }

    #[test]
    fn truncate_to_limit_handles_sentinel_rows_and_zero_limit() {
        let mut rows = vec![1, 2, 3];
        assert!(truncate_to_limit(&mut rows, 2));
        assert_eq!(rows, vec![1, 2]);

        let mut rows = vec![1];
        assert!(truncate_to_limit(&mut rows, 0));
        assert!(rows.is_empty());

        let mut rows = vec![1, 2];
        assert!(!truncate_to_limit(&mut rows, 2));
        assert_eq!(rows, vec![1, 2]);
    }

    #[test]
    fn graph_write_uses_wiki_labels_and_relationships() {
        let facts = WikiGraphFacts {
            documents: vec![
                WikiGraphDocument {
                    scope: SearchScope::topic("rust"),
                    path: PathBuf::from("wiki/a.md"),
                    title: Some("A".to_string()),
                },
                WikiGraphDocument {
                    scope: SearchScope::topic("rust"),
                    path: PathBuf::from("wiki/b.md"),
                    title: Some("B".to_string()),
                },
            ],
            links: vec![
                WikiGraphLink {
                    scope: SearchScope::topic("rust"),
                    source_path: PathBuf::from("wiki/a.md"),
                    raw_target: "wiki/b.md".to_string(),
                    target: WikiGraphLinkTarget::Resolved(PathBuf::from("wiki/b.md")),
                },
                WikiGraphLink {
                    scope: SearchScope::topic("rust"),
                    source_path: PathBuf::from("wiki/a.md"),
                    raw_target: "Missing Page".to_string(),
                    target: WikiGraphLinkTarget::Unresolved("Missing Page".to_string()),
                },
            ],
            sources: vec![WikiGraphSource {
                scope: SearchScope::topic("rust"),
                source_path: PathBuf::from("raw/a.md"),
                document_path: PathBuf::from("wiki/a.md"),
            }],
            code_edges: Vec::new(),
        };
        let joined = graph_write_statements(&facts)
            .into_iter()
            .map(|statement| statement.cypher)
            .collect::<Vec<_>>()
            .join("\n");
        for token in [
            WIKI_DOC_LABEL,
            WIKI_SOURCE_LABEL,
            WIKI_TARGET_LABEL,
            WIKI_LINKS_TO_REL,
            MENTIONS_TARGET_REL,
            SUPPORTS_REL,
        ] {
            assert!(joined.contains(token), "{token} missing from {joined}");
        }
        assert!(!joined.contains("CodeSymbol"));
        assert!(!joined.contains("gobby_code"));
    }
}
