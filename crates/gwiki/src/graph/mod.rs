use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use crate::links::canonical_target_key;
use crate::search::SearchScope;
use uuid::Uuid;

pub mod analytics;
pub mod context;
mod export;

pub use export::render_graph_report;

pub const WIKI_DOC_LABEL: &str = "WikiDoc";
pub const WIKI_SOURCE_LABEL: &str = "WikiSource";
pub const WIKI_TARGET_LABEL: &str = "WikiTarget";
pub const WIKI_LINKS_TO_REL: &str = "WIKI_LINKS_TO";
pub const MENTIONS_TARGET_REL: &str = "MENTIONS_TARGET";
pub const SUPPORTS_REL: &str = "SUPPORTS";
pub const BACKWARD_LINK_WEIGHT: f64 = 0.8;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WikiGraphDocument {
    pub scope: SearchScope,
    pub path: PathBuf,
    pub title: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WikiGraphSource {
    pub scope: SearchScope,
    pub source_path: PathBuf,
    pub document_path: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WikiGraphLinkTarget {
    Resolved(PathBuf),
    Unresolved(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WikiGraphLink {
    pub scope: SearchScope,
    pub source_path: PathBuf,
    pub raw_target: String,
    pub target: WikiGraphLinkTarget,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WikiGraphCodeEdge {
    pub scope: SearchScope,
    pub document_path: PathBuf,
    pub source: String,
    pub target: String,
    pub kind: String,
    pub direction: String,
    pub line: Option<usize>,
    pub provenance: String,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct WikiGraphFacts {
    pub documents: Vec<WikiGraphDocument>,
    pub links: Vec<WikiGraphLink>,
    pub sources: Vec<WikiGraphSource>,
    pub code_edges: Vec<WikiGraphCodeEdge>,
}

/// Scope filter applied to graph facts before export.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, clap::ValueEnum)]
pub enum GraphInclude {
    /// Knowledge pages (`knowledge/`, `recaps/`, root pages) without code edges.
    Knowledge,
    /// `code/**` documents plus code edges.
    Code,
    /// Every fact.
    #[default]
    All,
}

impl WikiGraphFacts {
    /// Retain only the facts selected by `include`. Analytics downstream see
    /// the filtered set because `export_graph` recomputes them from `self`.
    pub fn retain_include(&mut self, include: GraphInclude) {
        let retain: fn(&Path) -> bool = match include {
            GraphInclude::All => return,
            GraphInclude::Knowledge => is_knowledge_document_path,
            GraphInclude::Code => is_code_document_path,
        };
        self.documents.retain(|document| retain(&document.path));
        self.links.retain(|link| retain(&link.source_path));
        self.sources.retain(|source| retain(&source.document_path));
        if matches!(include, GraphInclude::Knowledge) {
            self.code_edges.clear();
        }
    }
}

fn is_knowledge_document_path(path: &Path) -> bool {
    let graph_path = graph_path(path);
    graph_path.starts_with("knowledge/")
        || graph_path.starts_with("recaps/")
        || !graph_path.contains('/')
}

fn is_code_document_path(path: &Path) -> bool {
    graph_path(path).starts_with("code/")
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct GraphExportOptions {
    pub degraded_sources: Vec<String>,
}

impl GraphExportOptions {
    pub fn available() -> Self {
        Self::default()
    }

    pub fn degraded(degraded_sources: Vec<String>) -> Self {
        Self { degraded_sources }
    }
}

#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct GraphExport {
    pub command: &'static str,
    pub degraded: bool,
    pub degraded_sources: Vec<String>,
    pub analytics: analytics::GraphExportAnalytics,
    pub nodes: Vec<GraphExportNode>,
    pub edges: GraphExportEdges,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct GraphExportNode {
    pub id: String,
    pub kind: &'static str,
    pub scope_kind: String,
    pub scope_id: String,
    pub path: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, serde::Serialize)]
pub struct GraphExportEdges {
    pub links: Vec<GraphExportEdge>,
    pub imports: Vec<GraphExportEdge>,
    pub calls: Vec<GraphExportEdge>,
    pub callers: Vec<GraphExportEdge>,
    pub trust: Vec<GraphExportEdge>,
    pub audit: Vec<GraphExportEdge>,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct GraphExportEdge {
    pub source: String,
    pub target: String,
    pub kind: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub raw_target: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GraphStatement {
    pub cypher: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WikiBacklink {
    pub scope: SearchScope,
    pub source_path: PathBuf,
    pub target_path: PathBuf,
    pub raw_target: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LinkSuggestion {
    pub scope: SearchScope,
    /// Most frequent raw spelling in the case-folded cluster (ties break
    /// lexicographically).
    pub target: String,
    /// Total mentions across every case variant in the cluster.
    pub mention_count: usize,
    pub source_paths: Vec<PathBuf>,
    /// Distinct raw spellings in the cluster, most frequent first (ties
    /// break lexicographically). Always contains `target`.
    pub variants: Vec<String>,
}

#[cfg(test)]
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RelatedPathOptions {
    pub backward_link_weight: f64,
}

#[cfg(test)]
impl Default for RelatedPathOptions {
    fn default() -> Self {
        Self {
            backward_link_weight: BACKWARD_LINK_WEIGHT,
        }
    }
}

/// Case-insensitive document lookup map from [`canonical_target_key`] of a
/// document path to the actual path. On case-colliding paths the first path
/// in iteration order wins.
pub fn document_target_map<'a, I>(paths: I) -> BTreeMap<String, PathBuf>
where
    I: IntoIterator<Item = &'a PathBuf>,
{
    let mut targets = BTreeMap::new();
    for path in paths {
        targets
            .entry(canonical_target_key(&path.to_string_lossy()))
            .or_insert_with(|| path.clone());
    }
    targets
}

pub fn graph_write_statements(facts: &WikiGraphFacts) -> Vec<GraphStatement> {
    let mut statements = Vec::new();
    let documents = facts
        .documents
        .iter()
        .map(|document| (document.scope.clone(), document.path.clone()))
        .collect::<BTreeSet<_>>();

    for document in &facts.documents {
        statements.push(GraphStatement {
            cypher: format!(
                "MERGE (doc:{} {{{}}}){}",
                label(WIKI_DOC_LABEL),
                scoped_path_properties(&document.scope, &document.path),
                document
                    .title
                    .as_deref()
                    .map(|title| format!(" SET doc.{} = {}", property("title"), string(title)))
                    .unwrap_or_default()
            ),
        });
    }

    for link in &facts.links {
        if !documents.contains(&(link.scope.clone(), link.source_path.clone())) {
            continue;
        }
        statements.push(match &link.target {
            WikiGraphLinkTarget::Resolved(target_path) => {
                if !documents.contains(&(link.scope.clone(), target_path.clone())) {
                    continue;
                }
                GraphStatement {
                    cypher: format!(
                        "MATCH (source:{} {{{}}}) MATCH (target:{} {{{}}}) MERGE (source)-[:{} {{{}: {}}}]->(target)",
                        label(WIKI_DOC_LABEL),
                        scoped_path_properties(&link.scope, &link.source_path),
                        label(WIKI_DOC_LABEL),
                        scoped_path_properties(&link.scope, target_path),
                        rel(WIKI_LINKS_TO_REL),
                        property("raw_target"),
                        string(&link.raw_target),
                    ),
                }
            }
            WikiGraphLinkTarget::Unresolved(target) => GraphStatement {
                cypher: format!(
                    "MATCH (source:{} {{{}}}) MERGE (target:{} {{{}, {}: {}}}) MERGE (source)-[:{} {{{}: {}}}]->(target)",
                    label(WIKI_DOC_LABEL),
                    scoped_path_properties(&link.scope, &link.source_path),
                    label(WIKI_TARGET_LABEL),
                    scope_properties(&link.scope),
                    property("target"),
                    string(target),
                    rel(MENTIONS_TARGET_REL),
                    property("raw_target"),
                    string(&link.raw_target),
                ),
            },
        });
    }

    for source in &facts.sources {
        if !documents.contains(&(source.scope.clone(), source.document_path.clone())) {
            continue;
        }
        statements.push(GraphStatement {
            cypher: format!(
                // Cypher requires WITH to reintroduce MATCH after an updating
                // clause such as MERGE.
                "MERGE (source:{} {{{}}}) WITH source MATCH (doc:{} {{{}}}) MERGE (source)-[:{}]->(doc)",
                label(WIKI_SOURCE_LABEL),
                scoped_path_properties(&source.scope, &source.source_path),
                label(WIKI_DOC_LABEL),
                scoped_path_properties(&source.scope, &source.document_path),
                rel(SUPPORTS_REL),
            ),
        });
    }

    statements
}

#[derive(Debug, Default)]
pub struct MemoryWikiGraph {
    facts: WikiGraphFacts,
}

impl MemoryWikiGraph {
    pub fn replace_facts(&mut self, facts: WikiGraphFacts) {
        self.facts = facts;
    }

    #[cfg(test)]
    pub fn document_titles(&self, scope: &SearchScope) -> BTreeMap<PathBuf, String> {
        self.facts
            .documents
            .iter()
            .filter(|document| &document.scope == scope)
            .filter_map(|document| {
                document
                    .title
                    .as_ref()
                    .map(|title| (document.path.clone(), title.clone()))
            })
            .collect()
    }

    #[cfg(test)]
    pub(crate) fn graph_facts_for_tests(&self) -> &WikiGraphFacts {
        &self.facts
    }

    pub fn backlinks(
        &self,
        scope: &SearchScope,
        target_path: impl Into<PathBuf>,
    ) -> Vec<WikiBacklink> {
        let target_path = target_path.into();
        let documents = self.document_keys();
        let mut backlinks = self
            .facts
            .links
            .iter()
            .filter_map(|link| {
                let WikiGraphLinkTarget::Resolved(resolved_path) = &link.target else {
                    return None;
                };
                if &link.scope != scope || resolved_path != &target_path {
                    return None;
                }
                if !documents.contains(&(scope.clone(), link.source_path.clone()))
                    || !documents.contains(&(scope.clone(), target_path.clone()))
                {
                    return None;
                }

                Some(WikiBacklink {
                    scope: scope.clone(),
                    source_path: link.source_path.clone(),
                    target_path: target_path.clone(),
                    raw_target: link.raw_target.clone(),
                })
            })
            .collect::<Vec<_>>();
        backlinks.sort_by(|a, b| a.source_path.cmp(&b.source_path));
        backlinks
    }

    pub fn link_suggestions(&self, scope: &SearchScope, limit: usize) -> Vec<LinkSuggestion> {
        if limit == 0 {
            return Vec::new();
        }

        #[derive(Default)]
        struct Accumulator {
            count: usize,
            variant_counts: BTreeMap<String, usize>,
            source_paths: BTreeSet<PathBuf>,
        }

        // Cluster unresolved targets case-insensitively so [[gcode]] and
        // [[Gcode]] surface as one suggestion instead of split mentions.
        let mut by_target = BTreeMap::<String, Accumulator>::new();
        for link in &self.facts.links {
            let WikiGraphLinkTarget::Unresolved(target) = &link.target else {
                continue;
            };
            if &link.scope != scope {
                continue;
            }
            // An absolute filesystem path can never be a vault page, so it
            // is pure noise as a page-creation candidate; lint keeps
            // reporting the link as broken (#17649).
            if Path::new(target).is_absolute() {
                continue;
            }

            let entry = by_target.entry(canonical_target_key(target)).or_default();
            entry.count += 1;
            *entry.variant_counts.entry(target.clone()).or_default() += 1;
            entry.source_paths.insert(link.source_path.clone());
        }

        let mut suggestions = by_target
            .into_values()
            .map(|entry| {
                let mut variants = entry.variant_counts.into_iter().collect::<Vec<_>>();
                variants.sort_by(|(left_target, left_count), (right_target, right_count)| {
                    right_count
                        .cmp(left_count)
                        .then_with(|| left_target.cmp(right_target))
                });
                let variants = variants
                    .into_iter()
                    .map(|(variant, _)| variant)
                    .collect::<Vec<_>>();
                LinkSuggestion {
                    scope: scope.clone(),
                    target: variants[0].clone(),
                    mention_count: entry.count,
                    source_paths: entry.source_paths.into_iter().collect(),
                    variants,
                }
            })
            .collect::<Vec<_>>();

        suggestions.sort_by(|a, b| {
            b.mention_count
                .cmp(&a.mention_count)
                .then_with(|| a.target.cmp(&b.target))
        });
        suggestions.truncate(limit);
        suggestions
    }

    #[cfg(test)]
    pub fn related_paths(
        &self,
        scope: &SearchScope,
        seed_paths: &[PathBuf],
        limit: usize,
    ) -> Vec<(PathBuf, f64)> {
        self.related_paths_with_options(scope, seed_paths, limit, RelatedPathOptions::default())
    }

    #[cfg(test)]
    pub fn related_paths_with_options(
        &self,
        scope: &SearchScope,
        seed_paths: &[PathBuf],
        limit: usize,
        options: RelatedPathOptions,
    ) -> Vec<(PathBuf, f64)> {
        if seed_paths.is_empty() || limit == 0 {
            return Vec::new();
        }

        let documents = self.document_keys();
        let seed_set = seed_paths.iter().cloned().collect::<BTreeSet<_>>();
        let mut scores = BTreeMap::<PathBuf, f64>::new();
        let resolved_links = self
            .facts
            .links
            .iter()
            .filter_map(|link| {
                if &link.scope != scope
                    || !documents.contains(&(scope.clone(), link.source_path.clone()))
                {
                    return None;
                }
                let WikiGraphLinkTarget::Resolved(target_path) = &link.target else {
                    return None;
                };
                if !documents.contains(&(scope.clone(), target_path.clone())) {
                    return None;
                }
                Some((link, target_path))
            })
            .collect::<Vec<_>>();
        let mut outdegrees = BTreeMap::<PathBuf, usize>::new();
        for (link, _) in &resolved_links {
            *outdegrees.entry(link.source_path.clone()).or_default() += 1;
        }
        for (rank, seed_path) in seed_paths.iter().enumerate() {
            if !documents.contains(&(scope.clone(), seed_path.clone())) {
                continue;
            }
            let seed_score = 1.0 / (rank + 1) as f64;
            for &(link, target_path) in &resolved_links {
                let candidate = if &link.source_path == seed_path {
                    Some((target_path, seed_score))
                } else if target_path == seed_path {
                    let outdegree = outdegrees.get(&link.source_path).copied().unwrap_or(1) as f64;
                    Some((
                        &link.source_path,
                        seed_score * options.backward_link_weight / outdegree,
                    ))
                } else {
                    None
                };
                let Some((path, score)) = candidate else {
                    continue;
                };
                if !score.is_finite() {
                    continue;
                }
                if seed_set.contains(path) {
                    continue;
                }
                *scores.entry(path.clone()).or_default() += score;
            }
        }

        let mut ranked = scores.into_iter().collect::<Vec<_>>();
        ranked.sort_by(|(left_path, left_score), (right_path, right_score)| {
            right_score
                .total_cmp(left_score)
                .then_with(|| left_path.cmp(right_path))
        });
        ranked.truncate(limit);
        ranked
    }

    fn document_keys(&self) -> BTreeSet<(SearchScope, PathBuf)> {
        self.facts
            .documents
            .iter()
            .map(|document| (document.scope.clone(), document.path.clone()))
            .collect()
    }
}

fn label(value: &str) -> String {
    gobby_core::falkor::escape_label(value)
}

fn rel(value: &str) -> String {
    gobby_core::falkor::escape_rel_type(value)
}

fn property(value: &str) -> String {
    gobby_core::falkor::escape_property(value)
}

fn string(value: &str) -> String {
    gobby_core::falkor::escape_string(value)
}

fn scope_properties(scope: &SearchScope) -> String {
    format!(
        "{}: {}, {}: {}",
        property("scope_kind"),
        string(scope.scope_kind()),
        property("scope_id"),
        string(scope.scope_value()),
    )
}

fn scoped_path_properties(scope: &SearchScope, path: &Path) -> String {
    format!(
        "{}, {}: {}",
        scope_properties(scope),
        property("path"),
        string(&graph_path(path)),
    )
}

fn graph_path(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

fn document_node(document: &WikiGraphDocument) -> GraphExportNode {
    GraphExportNode {
        id: document_id(&document.scope, &document.path),
        kind: document_kind(&document.path),
        scope_kind: document.scope.scope_kind().to_string(),
        scope_id: document.scope.scope_value().to_string(),
        path: graph_path(&document.path),
        title: document.title.clone(),
    }
}

fn source_node(source: &WikiGraphSource) -> GraphExportNode {
    GraphExportNode {
        id: source_node_id(&source.scope, &source.source_path),
        kind: "source",
        scope_kind: source.scope.scope_kind().to_string(),
        scope_id: source.scope.scope_value().to_string(),
        path: graph_path(&source.source_path),
        title: None,
    }
}

fn citation_node(source: &WikiGraphSource) -> GraphExportNode {
    GraphExportNode {
        id: citation_node_id(&source.scope, &source.source_path, &source.document_path),
        kind: "citation",
        scope_kind: source.scope.scope_kind().to_string(),
        scope_id: source.scope.scope_value().to_string(),
        path: graph_path(&source.source_path),
        title: None,
    }
}

fn unresolved_target_node(scope: &SearchScope, target: &str) -> GraphExportNode {
    GraphExportNode {
        id: unresolved_target_id(scope, target),
        kind: "unresolved_target",
        scope_kind: scope.scope_kind().to_string(),
        scope_id: scope.scope_value().to_string(),
        path: target.to_string(),
        title: Some(target.to_string()),
    }
}

fn document_id(scope: &SearchScope, path: &Path) -> String {
    scoped_id(scope, "document", &graph_path(path))
}

fn source_node_id(scope: &SearchScope, path: &Path) -> String {
    scoped_id(scope, "source", &graph_path(path))
}

fn citation_node_id(scope: &SearchScope, source_path: &Path, document_path: &Path) -> String {
    scoped_id(
        scope,
        "citation",
        &[graph_path(source_path), graph_path(document_path)].join("\0"),
    )
}

fn unresolved_target_id(scope: &SearchScope, target: &str) -> String {
    scoped_id(scope, "unresolved", target)
}

fn code_endpoint_id(scope: &SearchScope, endpoint: &str) -> String {
    scoped_id(scope, "code", endpoint)
}

fn scoped_id(scope: &SearchScope, kind: &str, value: &str) -> String {
    let key = format!(
        "kind={kind}\0scope_kind={}\0scope_value={}\0value={value}",
        scope.scope_kind(),
        scope.scope_value()
    );
    let stable = Uuid::new_v5(&Uuid::NAMESPACE_URL, key.as_bytes());
    let readable = readable_id_prefix(value);
    format!("{kind}-{readable}-{stable}")
}

fn readable_id_prefix(value: &str) -> String {
    let mut prefix = value
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() {
                character.to_ascii_lowercase()
            } else {
                '-'
            }
        })
        .collect::<String>();
    while prefix.contains("--") {
        prefix = prefix.replace("--", "-");
    }
    let prefix = prefix.trim_matches('-');
    if prefix.is_empty() {
        "id".to_string()
    } else {
        prefix.chars().take(48).collect()
    }
}

fn document_kind(path: &Path) -> &'static str {
    let graph_path = graph_path(path);
    if graph_path.starts_with("knowledge/") {
        "wiki_page"
    } else if graph_path.starts_with("code/") || is_code_path(path) {
        "code"
    } else {
        "document"
    }
}

fn is_code_path(path: &Path) -> bool {
    matches!(
        path.extension().and_then(|extension| extension.to_str()),
        Some(
            "c" | "cc"
                | "cpp"
                | "cs"
                | "go"
                | "h"
                | "hpp"
                | "java"
                | "js"
                | "jsx"
                | "kt"
                | "php"
                | "py"
                | "rb"
                | "rs"
                | "scala"
                | "sh"
                | "sql"
                | "swift"
                | "ts"
                | "tsx"
        )
    )
}

fn mermaid_node_id(id: &str) -> String {
    id.chars()
        .map(|ch| if ch.is_ascii_alphanumeric() { ch } else { '_' })
        .collect()
}

fn mermaid_label(node: &GraphExportNode) -> String {
    gobby_core::vault::mermaid::escape_label(node.title.as_deref().unwrap_or(&node.path))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn graph_labels_are_wiki_owned() {
        let facts = WikiGraphFacts {
            documents: vec![
                WikiGraphDocument {
                    scope: SearchScope::project("project-1"),
                    path: "knowledge/topics/rust.md".into(),
                    title: Some("Rust".to_string()),
                },
                WikiGraphDocument {
                    scope: SearchScope::project("project-1"),
                    path: "knowledge/concepts/ownership.md".into(),
                    title: Some("Ownership".to_string()),
                },
            ],
            links: vec![
                WikiGraphLink {
                    scope: SearchScope::project("project-1"),
                    source_path: "knowledge/topics/rust.md".into(),
                    raw_target: "Ownership".to_string(),
                    target: WikiGraphLinkTarget::Resolved("knowledge/concepts/ownership.md".into()),
                },
                WikiGraphLink {
                    scope: SearchScope::project("project-1"),
                    source_path: "knowledge/topics/rust.md".into(),
                    raw_target: "Borrow checker".to_string(),
                    target: WikiGraphLinkTarget::Unresolved("Borrow checker".to_string()),
                },
            ],
            sources: vec![WikiGraphSource {
                scope: SearchScope::project("project-1"),
                source_path: "raw/INDEX.md".into(),
                document_path: "knowledge/topics/rust.md".into(),
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
            assert!(joined.contains(token), "expected wiki graph token {token}");
        }

        for forbidden in [
            "CodeSymbol",
            "CodeFile",
            "Symbol",
            "CALLS",
            "IMPORTS",
            "DEFINES",
        ] {
            assert!(
                !joined.contains(forbidden),
                "{forbidden} must not leak into wiki graph"
            );
        }
    }

    #[test]
    fn graph_write_skips_relationships_to_missing_documents() {
        let scope = SearchScope::project("project-1");
        let facts = WikiGraphFacts {
            documents: vec![doc(scope.clone(), "wiki/source.md")],
            links: vec![
                resolved_link(
                    scope.clone(),
                    "wiki/source.md",
                    "Missing",
                    "wiki/missing.md",
                ),
                unresolved_link(scope.clone(), "wiki/source.md", "Missing"),
                unresolved_link(scope.clone(), "wiki/ghost.md", "Ghost"),
            ],
            sources: vec![WikiGraphSource {
                scope: scope.clone(),
                source_path: "raw/missing.md".into(),
                document_path: "wiki/missing.md".into(),
            }],
            code_edges: Vec::new(),
        };

        let statements = graph_write_statements(&facts);
        let joined = statements
            .iter()
            .map(|statement| statement.cypher.as_str())
            .collect::<Vec<_>>()
            .join("\n");

        assert_eq!(joined.matches(WIKI_LINKS_TO_REL).count(), 0);
        assert_eq!(joined.matches(SUPPORTS_REL).count(), 0);
        assert_eq!(joined.matches(MENTIONS_TARGET_REL).count(), 1);
        assert!(joined.contains(WIKI_TARGET_LABEL));
    }

    #[test]
    fn scoped_graph_ids_hash_structured_values() {
        let scope = SearchScope::project("project:1");
        let id = scoped_id(&scope, "document", "knowledge/topics/a:b.md");

        assert!(id.starts_with("document-knowledge-topics-a-b-md-"));
        assert_eq!(id, scoped_id(&scope, "document", "knowledge/topics/a:b.md"));
        assert_ne!(id, "document:project:project:1:knowledge/topics/a:b.md");
    }

    #[test]
    fn backlinks_are_scope_filtered() {
        let mut graph = MemoryWikiGraph::default();
        graph.replace_facts(WikiGraphFacts {
            documents: vec![
                doc(
                    SearchScope::project("project-1"),
                    "knowledge/concepts/ownership.md",
                ),
                doc(
                    SearchScope::project("project-1"),
                    "knowledge/topics/rust.md",
                ),
                doc(SearchScope::topic("rust"), "knowledge/topics/rust.md"),
            ],
            links: vec![
                resolved_link(
                    SearchScope::project("project-1"),
                    "knowledge/topics/rust.md",
                    "Ownership",
                    "knowledge/concepts/ownership.md",
                ),
                resolved_link(
                    SearchScope::topic("rust"),
                    "knowledge/topics/rust.md",
                    "Ownership",
                    "knowledge/concepts/ownership.md",
                ),
            ],
            sources: Vec::new(),
            code_edges: Vec::new(),
        });

        let backlinks = graph.backlinks(
            &SearchScope::project("project-1"),
            "knowledge/concepts/ownership.md",
        );

        assert_eq!(backlinks.len(), 1);
        assert_eq!(backlinks[0].scope, SearchScope::project("project-1"));
        assert_eq!(
            backlinks[0].source_path,
            PathBuf::from("knowledge/topics/rust.md")
        );
    }

    #[test]
    fn link_suggest_is_read_only() {
        let markdown = "# Rust\n\nSee [[Ownership]] and [[Borrow checker]].\n";
        let before = markdown.to_string();
        let mut graph = MemoryWikiGraph::default();
        graph.replace_facts(WikiGraphFacts {
            documents: vec![doc(
                SearchScope::project("project-1"),
                "knowledge/topics/rust.md",
            )],
            links: vec![
                unresolved_link(
                    SearchScope::project("project-1"),
                    "knowledge/topics/rust.md",
                    "Ownership",
                ),
                unresolved_link(
                    SearchScope::project("project-1"),
                    "knowledge/concepts/lifetime.md",
                    "Ownership",
                ),
                unresolved_link(
                    SearchScope::project("project-1"),
                    "knowledge/topics/rust.md",
                    "Borrow checker",
                ),
                unresolved_link(
                    SearchScope::topic("rust"),
                    "knowledge/topics/rust.md",
                    "Ownership",
                ),
            ],
            sources: Vec::new(),
            code_edges: Vec::new(),
        });

        let suggestions = graph.link_suggestions(&SearchScope::project("project-1"), 10);

        assert_eq!(markdown, before);
        assert_eq!(suggestions[0].target, "Ownership");
        assert_eq!(suggestions[0].mention_count, 2);
        assert_eq!(suggestions[0].source_paths.len(), 2);
        assert_eq!(suggestions[0].variants, vec!["Ownership".to_string()]);
        assert_eq!(suggestions[1].target, "Borrow checker");
        assert_eq!(suggestions[1].mention_count, 1);
    }

    #[test]
    fn link_suggestions_cluster_case_variants() {
        let scope = SearchScope::project("project-1");
        let mut graph = MemoryWikiGraph::default();
        graph.replace_facts(WikiGraphFacts {
            documents: vec![doc(scope.clone(), "knowledge/topics/rust.md")],
            links: vec![
                unresolved_link(scope.clone(), "knowledge/topics/a.md", "gcode"),
                unresolved_link(scope.clone(), "knowledge/topics/b.md", "gcode"),
                unresolved_link(scope.clone(), "knowledge/topics/c.md", "Gcode"),
                unresolved_link(scope.clone(), "knowledge/topics/c.md", "GCODE"),
                unresolved_link(scope.clone(), "knowledge/topics/d.md", "Other"),
            ],
            sources: Vec::new(),
            code_edges: Vec::new(),
        });

        let suggestions = graph.link_suggestions(&scope, 10);

        assert_eq!(suggestions.len(), 2);
        // The cluster keeps the most frequent raw spelling as display target
        // and sums mentions across every case variant.
        assert_eq!(suggestions[0].target, "gcode");
        assert_eq!(suggestions[0].mention_count, 4);
        assert_eq!(suggestions[0].source_paths.len(), 3);
        assert_eq!(
            suggestions[0].variants,
            vec![
                "gcode".to_string(),
                "GCODE".to_string(),
                "Gcode".to_string(),
            ]
        );
        assert_eq!(suggestions[1].target, "Other");
        assert_eq!(suggestions[1].variants, vec!["Other".to_string()]);
    }

    #[test]
    fn related_paths_support_weight_options_and_skip_non_finite_scores() {
        let mut graph = MemoryWikiGraph::default();
        let scope = SearchScope::project("project-1");
        graph.replace_facts(WikiGraphFacts {
            documents: vec![
                doc(scope.clone(), "wiki/a.md"),
                doc(scope.clone(), "wiki/b.md"),
                doc(scope.clone(), "wiki/c.md"),
            ],
            links: vec![
                resolved_link(scope.clone(), "wiki/a.md", "B", "wiki/b.md"),
                resolved_link(scope.clone(), "wiki/c.md", "A", "wiki/a.md"),
            ],
            sources: Vec::new(),
            code_edges: Vec::new(),
        });

        let ranked = graph.related_paths_with_options(
            &scope,
            &[PathBuf::from("wiki/a.md")],
            10,
            RelatedPathOptions {
                backward_link_weight: 0.5,
            },
        );
        assert_eq!(
            ranked,
            vec![
                (PathBuf::from("wiki/b.md"), 1.0),
                (PathBuf::from("wiki/c.md"), 0.5),
            ]
        );

        let non_finite = graph.related_paths_with_options(
            &scope,
            &[PathBuf::from("wiki/a.md")],
            10,
            RelatedPathOptions {
                backward_link_weight: f64::NAN,
            },
        );
        assert_eq!(non_finite, vec![(PathBuf::from("wiki/b.md"), 1.0)]);
    }

    fn doc(scope: SearchScope, path: &str) -> WikiGraphDocument {
        WikiGraphDocument {
            scope,
            path: path.into(),
            title: None,
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
            source_path: source_path.into(),
            raw_target: raw_target.to_string(),
            target: WikiGraphLinkTarget::Resolved(target_path.into()),
        }
    }

    fn unresolved_link(scope: SearchScope, source_path: &str, target: &str) -> WikiGraphLink {
        WikiGraphLink {
            scope,
            source_path: source_path.into(),
            raw_target: target.to_string(),
            target: WikiGraphLinkTarget::Unresolved(target.to_string()),
        }
    }

    fn code_edge(scope: SearchScope, document_path: &str) -> WikiGraphCodeEdge {
        WikiGraphCodeEdge {
            scope,
            document_path: document_path.into(),
            source: "crates/gwiki/src/main.rs".to_string(),
            target: "crates/gwiki/src/api.rs".to_string(),
            kind: "imports".to_string(),
            direction: "out".to_string(),
            line: Some(1),
            provenance: "code-index".to_string(),
        }
    }

    fn mixed_facts(scope: SearchScope) -> WikiGraphFacts {
        WikiGraphFacts {
            documents: vec![
                doc(scope.clone(), "knowledge/concepts/ownership.md"),
                doc(scope.clone(), "recaps/2026-07-09.md"),
                doc(scope.clone(), "Home.md"),
                doc(scope.clone(), "code/crates/gwiki/src/main.rs.md"),
            ],
            links: vec![
                resolved_link(
                    scope.clone(),
                    "knowledge/concepts/ownership.md",
                    "Home",
                    "Home.md",
                ),
                unresolved_link(
                    scope.clone(),
                    "knowledge/concepts/ownership.md",
                    "Borrowing",
                ),
                resolved_link(
                    scope.clone(),
                    "code/crates/gwiki/src/main.rs.md",
                    "Ownership",
                    "knowledge/concepts/ownership.md",
                ),
            ],
            sources: vec![
                WikiGraphSource {
                    scope: scope.clone(),
                    source_path: "raw/sources/example.md".into(),
                    document_path: "knowledge/concepts/ownership.md".into(),
                },
                WikiGraphSource {
                    scope: scope.clone(),
                    source_path: "raw/sources/code.md".into(),
                    document_path: "code/crates/gwiki/src/main.rs.md".into(),
                },
            ],
            code_edges: vec![code_edge(scope, "code/crates/gwiki/src/main.rs.md")],
        }
    }

    #[test]
    fn retain_include_knowledge_drops_code_edges() {
        let scope = SearchScope::project("project-1");
        let mut facts = mixed_facts(scope);

        facts.retain_include(GraphInclude::Knowledge);

        let retained = facts
            .documents
            .iter()
            .map(|document| graph_path(&document.path))
            .collect::<Vec<_>>();
        assert_eq!(
            retained,
            vec![
                "knowledge/concepts/ownership.md",
                "recaps/2026-07-09.md",
                "Home.md"
            ]
        );
        assert_eq!(facts.links.len(), 2);
        assert!(
            facts
                .links
                .iter()
                .all(|link| graph_path(&link.source_path).starts_with("knowledge/"))
        );
        assert!(facts.links.iter().any(|link| matches!(
            &link.target,
            WikiGraphLinkTarget::Unresolved(target) if target == "Borrowing"
        )));
        assert_eq!(facts.sources.len(), 1);
        assert!(facts.code_edges.is_empty());

        // Analytics recompute on the filtered facts through the normal export path.
        let export = facts
            .export_graph(GraphExportOptions::available())
            .expect("filtered export");
        assert!(export.edges.imports.is_empty());
        assert!(
            export
                .nodes
                .iter()
                .all(|node| !node.path.starts_with("code/"))
        );
    }

    #[test]
    fn retain_include_code_retains_code_documents_and_edges() {
        let scope = SearchScope::project("project-1");
        let mut facts = mixed_facts(scope);

        facts.retain_include(GraphInclude::Code);

        let retained = facts
            .documents
            .iter()
            .map(|document| graph_path(&document.path))
            .collect::<Vec<_>>();
        assert_eq!(retained, vec!["code/crates/gwiki/src/main.rs.md"]);
        assert_eq!(facts.links.len(), 1);
        assert_eq!(facts.sources.len(), 1);
        assert_eq!(facts.code_edges.len(), 1);
    }

    #[test]
    fn retain_include_all_is_noop() {
        let scope = SearchScope::project("project-1");
        let mut facts = mixed_facts(scope);
        let original = facts.clone();

        facts.retain_include(GraphInclude::All);

        assert_eq!(facts, original);
    }
}
