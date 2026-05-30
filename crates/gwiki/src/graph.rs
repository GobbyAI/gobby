use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use crate::search::SearchScope;

pub const WIKI_DOC_LABEL: &str = "WikiDoc";
pub const WIKI_SOURCE_LABEL: &str = "WikiSource";
pub const WIKI_TARGET_LABEL: &str = "WikiTarget";
pub const WIKI_LINKS_TO_REL: &str = "WIKI_LINKS_TO";
pub const MENTIONS_TARGET_REL: &str = "MENTIONS_TARGET";
pub const SUPPORTS_REL: &str = "SUPPORTS";

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

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct WikiGraphFacts {
    pub documents: Vec<WikiGraphDocument>,
    pub links: Vec<WikiGraphLink>,
    pub sources: Vec<WikiGraphSource>,
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
    pub target: String,
    pub mention_count: usize,
    pub source_paths: Vec<PathBuf>,
}

pub fn graph_write_statements(facts: &WikiGraphFacts) -> Vec<GraphStatement> {
    let mut statements = Vec::new();

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
        statements.push(match &link.target {
            WikiGraphLinkTarget::Resolved(target_path) => GraphStatement {
                cypher: format!(
                    "MATCH (source:{} {{{}}}) MERGE (target:{} {{{}}}) MERGE (source)-[:{} {{{}: {}}}]->(target)",
                    label(WIKI_DOC_LABEL),
                    scoped_path_properties(&link.scope, &link.source_path),
                    label(WIKI_DOC_LABEL),
                    scoped_path_properties(&link.scope, target_path),
                    rel(WIKI_LINKS_TO_REL),
                    property("raw_target"),
                    string(&link.raw_target),
                ),
            },
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
        statements.push(GraphStatement {
            cypher: format!(
                "MERGE (source:{} {{{}}}) MERGE (doc:{} {{{}}}) MERGE (source)-[:{}]->(doc)",
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
            source_paths: BTreeSet<PathBuf>,
        }

        let mut by_target = BTreeMap::<String, Accumulator>::new();
        for link in &self.facts.links {
            let WikiGraphLinkTarget::Unresolved(target) = &link.target else {
                continue;
            };
            if &link.scope != scope {
                continue;
            }

            let entry = by_target.entry(target.clone()).or_default();
            entry.count += 1;
            entry.source_paths.insert(link.source_path.clone());
        }

        let mut suggestions = by_target
            .into_iter()
            .map(|(target, entry)| LinkSuggestion {
                scope: scope.clone(),
                target,
                mention_count: entry.count,
                source_paths: entry.source_paths.into_iter().collect(),
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn graph_labels_are_wiki_owned() {
        let facts = WikiGraphFacts {
            documents: vec![
                WikiGraphDocument {
                    scope: SearchScope::project("project-1"),
                    path: "wiki/topics/rust.md".into(),
                    title: Some("Rust".to_string()),
                },
                WikiGraphDocument {
                    scope: SearchScope::project("project-1"),
                    path: "wiki/concepts/ownership.md".into(),
                    title: Some("Ownership".to_string()),
                },
            ],
            links: vec![
                WikiGraphLink {
                    scope: SearchScope::project("project-1"),
                    source_path: "wiki/topics/rust.md".into(),
                    raw_target: "Ownership".to_string(),
                    target: WikiGraphLinkTarget::Resolved("wiki/concepts/ownership.md".into()),
                },
                WikiGraphLink {
                    scope: SearchScope::project("project-1"),
                    source_path: "wiki/topics/rust.md".into(),
                    raw_target: "Borrow checker".to_string(),
                    target: WikiGraphLinkTarget::Unresolved("Borrow checker".to_string()),
                },
            ],
            sources: vec![WikiGraphSource {
                scope: SearchScope::project("project-1"),
                source_path: "raw/INDEX.md".into(),
                document_path: "wiki/topics/rust.md".into(),
            }],
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
    fn backlinks_are_scope_filtered() {
        let mut graph = MemoryWikiGraph::default();
        graph.replace_facts(WikiGraphFacts {
            documents: vec![
                doc(
                    SearchScope::project("project-1"),
                    "wiki/concepts/ownership.md",
                ),
                doc(SearchScope::project("project-1"), "wiki/topics/rust.md"),
                doc(SearchScope::topic("rust"), "wiki/topics/rust.md"),
            ],
            links: vec![
                resolved_link(
                    SearchScope::project("project-1"),
                    "wiki/topics/rust.md",
                    "Ownership",
                    "wiki/concepts/ownership.md",
                ),
                resolved_link(
                    SearchScope::topic("rust"),
                    "wiki/topics/rust.md",
                    "Ownership",
                    "wiki/concepts/ownership.md",
                ),
            ],
            sources: Vec::new(),
        });

        let backlinks = graph.backlinks(
            &SearchScope::project("project-1"),
            "wiki/concepts/ownership.md",
        );

        assert_eq!(backlinks.len(), 1);
        assert_eq!(backlinks[0].scope, SearchScope::project("project-1"));
        assert_eq!(
            backlinks[0].source_path,
            PathBuf::from("wiki/topics/rust.md")
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
                "wiki/topics/rust.md",
            )],
            links: vec![
                unresolved_link(
                    SearchScope::project("project-1"),
                    "wiki/topics/rust.md",
                    "Ownership",
                ),
                unresolved_link(
                    SearchScope::project("project-1"),
                    "wiki/concepts/lifetime.md",
                    "Ownership",
                ),
                unresolved_link(
                    SearchScope::project("project-1"),
                    "wiki/topics/rust.md",
                    "Borrow checker",
                ),
                unresolved_link(
                    SearchScope::topic("rust"),
                    "wiki/topics/rust.md",
                    "Ownership",
                ),
            ],
            sources: Vec::new(),
        });

        let suggestions = graph.link_suggestions(&SearchScope::project("project-1"), 10);

        assert_eq!(markdown, before);
        assert_eq!(suggestions[0].target, "Ownership");
        assert_eq!(suggestions[0].mention_count, 2);
        assert_eq!(suggestions[0].source_paths.len(), 2);
        assert_eq!(suggestions[1].target, "Borrow checker");
        assert_eq!(suggestions[1].mention_count, 1);
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
}
