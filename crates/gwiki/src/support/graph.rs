use std::path::PathBuf;

use crate::{graph, search, store};

use super::text::slugify;

pub(crate) fn memory_graph_from_store(
    store: &store::MemoryWikiStore,
    scope: &search::SearchScope,
) -> graph::MemoryWikiGraph {
    let documents = store
        .documents
        .values()
        .map(|document| graph::WikiGraphDocument {
            scope: scope.clone(),
            path: document.path.clone(),
            title: document.title.clone(),
        })
        .collect::<Vec<_>>();
    let links = store
        .links
        .values()
        .flat_map(|links| links.iter())
        .filter_map(|link| {
            resolve_graph_target(&link.target, store).map(|target| graph::WikiGraphLink {
                scope: scope.clone(),
                source_path: link.path.clone(),
                raw_target: link.target.clone(),
                target,
            })
        })
        .collect::<Vec<_>>();
    let sources = store
        .sources
        .values()
        .map(|source| graph::WikiGraphSource {
            scope: scope.clone(),
            source_path: source.path.clone(),
            document_path: source.document_path.clone(),
        })
        .collect::<Vec<_>>();

    let mut graph = graph::MemoryWikiGraph::default();
    graph.replace_facts(graph::WikiGraphFacts {
        documents,
        links,
        sources,
    });
    graph
}

fn resolve_graph_target(
    raw_target: &str,
    store: &store::MemoryWikiStore,
) -> Option<graph::WikiGraphLinkTarget> {
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
    if store.documents.contains_key(&direct) {
        return Some(graph::WikiGraphLinkTarget::Resolved(direct));
    }

    let target_slug = slugify(normalized.trim_end_matches(".md"));
    for document in store.documents.values() {
        let file_slug = document
            .path
            .file_stem()
            .and_then(|value| value.to_str())
            .map(slugify);
        let title_slug = document.title.as_deref().map(slugify);
        if file_slug.as_deref() == Some(target_slug.as_str())
            || title_slug.as_deref() == Some(target_slug.as_str())
        {
            return Some(graph::WikiGraphLinkTarget::Resolved(document.path.clone()));
        }
    }

    Some(graph::WikiGraphLinkTarget::Unresolved(trimmed.to_string()))
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
    use crate::store::{MemoryWikiStore, WikiDocument, WikiDocumentKind, WikiSource};

    #[test]
    fn graph_uses_distinct_source_document_paths() {
        let mut store = MemoryWikiStore::default();
        store.documents.insert(
            PathBuf::from("wiki/topics/rust.md"),
            WikiDocument {
                path: PathBuf::from("wiki/topics/rust.md"),
                kind: WikiDocumentKind::Topic,
                title: Some("Rust".to_string()),
                content_hash: "hash".to_string(),
                body: "# Rust".to_string(),
            },
        );
        store.sources.insert(
            PathBuf::from("raw/source.md"),
            WikiSource {
                path: PathBuf::from("raw/source.md"),
                document_path: PathBuf::from("wiki/topics/rust.md"),
                kind: WikiDocumentKind::SourceNote,
                content_hash: "hash".to_string(),
            },
        );

        let graph = memory_graph_from_store(&store, &search::SearchScope::topic("rust"));
        let source = &graph.graph_facts_for_tests().sources[0];

        assert_eq!(source.source_path, PathBuf::from("raw/source.md"));
        assert_eq!(source.document_path, PathBuf::from("wiki/topics/rust.md"));
    }

    #[test]
    fn graph_rejects_url_like_external_targets() {
        let store = MemoryWikiStore::default();

        assert!(resolve_graph_target("//cdn.example.test/page", &store).is_none());
        assert!(resolve_graph_target(r"\\server\share\page", &store).is_none());
        assert!(resolve_graph_target("custom://example", &store).is_none());
    }
}
