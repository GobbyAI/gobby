use std::collections::BTreeMap;
use std::path::{Component, Path, PathBuf};

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
    let slug_targets = slug_target_map(store);
    let links = store
        .links
        .values()
        .flat_map(|links| links.iter())
        .filter_map(|link| {
            resolve_graph_target(&link.target, &link.path, store, &slug_targets).map(|target| {
                graph::WikiGraphLink {
                    scope: scope.clone(),
                    source_path: link.path.clone(),
                    raw_target: link.target.clone(),
                    target,
                }
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

    let mut mem_graph = graph::MemoryWikiGraph::default();
    mem_graph.replace_facts(graph::WikiGraphFacts {
        documents,
        links,
        sources,
        code_edges: Vec::new(),
    });
    mem_graph
}

fn resolve_graph_target(
    raw_target: &str,
    source_path: &Path,
    store: &store::MemoryWikiStore,
    slug_targets: &BTreeMap<String, PathBuf>,
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
        .replace('\\', "/");
    if normalized.is_empty() {
        return None;
    }

    let lookup = resolve_relative_graph_path(&normalized, source_path);
    let direct = PathBuf::from(&lookup);
    if store.documents.contains_key(&direct) {
        return Some(graph::WikiGraphLinkTarget::Resolved(direct));
    }

    let target_slug = slugify(lookup.strip_suffix(".md").unwrap_or(&lookup));
    if let Some(path) = slug_targets.get(&target_slug) {
        return Some(graph::WikiGraphLinkTarget::Resolved(path.clone()));
    }

    Some(graph::WikiGraphLinkTarget::Unresolved(lookup))
}

fn resolve_relative_graph_path(raw_target: &str, source_path: &Path) -> String {
    let normalized = raw_target.trim_start_matches('/');
    if raw_target.starts_with('/') || !is_path_like_target(normalized) {
        return normalized.to_string();
    }
    let Some(parent) = source_path.parent() else {
        return normalized.to_string();
    };
    normalize_path(parent.join(normalized))
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
            Component::Normal(part) => normalized.push(part),
            Component::RootDir | Component::Prefix(_) => {}
        }
    }
    normalized
}

fn slug_target_map(store: &store::MemoryWikiStore) -> BTreeMap<String, PathBuf> {
    let mut targets = BTreeMap::new();
    for document in store.documents.values() {
        // Deterministic collision behavior: first path in BTreeMap document
        // order wins for each slug, and later title/file slug collisions keep it.
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
    use crate::store::{MemoryWikiStore, WikiDocument, WikiDocumentKind, WikiSource};

    #[test]
    fn graph_uses_distinct_source_document_paths() {
        let mut store = MemoryWikiStore::default();
        store.documents.insert(
            PathBuf::from("knowledge/topics/rust.md"),
            WikiDocument {
                path: PathBuf::from("knowledge/topics/rust.md"),
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
                document_path: PathBuf::from("knowledge/topics/rust.md"),
                kind: WikiDocumentKind::SourceNote,
                content_hash: "hash".to_string(),
            },
        );

        let graph = memory_graph_from_store(&store, &search::SearchScope::topic("rust"));
        let source = &graph.graph_facts_for_tests().sources[0];

        assert_eq!(source.source_path, PathBuf::from("raw/source.md"));
        assert_eq!(
            source.document_path,
            PathBuf::from("knowledge/topics/rust.md")
        );
    }

    #[test]
    fn graph_rejects_url_like_external_targets() {
        let store = MemoryWikiStore::default();
        let slug_targets = slug_target_map(&store);
        let source = Path::new("knowledge/topics/source.md");

        assert!(
            resolve_graph_target("//cdn.example.test/page", source, &store, &slug_targets)
                .is_none()
        );
        assert!(
            resolve_graph_target(r"\\server\share\page", source, &store, &slug_targets).is_none()
        );
        assert!(resolve_graph_target("custom://example", source, &store, &slug_targets).is_none());
    }

    #[test]
    fn graph_resolves_slug_targets_from_precomputed_map() {
        let mut store = MemoryWikiStore::default();
        store.documents.insert(
            PathBuf::from("knowledge/topics/rust-async.md"),
            WikiDocument {
                path: PathBuf::from("knowledge/topics/rust-async.md"),
                kind: WikiDocumentKind::Topic,
                title: Some("Rust Async".to_string()),
                content_hash: "hash".to_string(),
                body: "# Rust Async".to_string(),
            },
        );
        let slug_targets = slug_target_map(&store);

        assert_eq!(
            resolve_graph_target(
                "Rust Async",
                Path::new("knowledge/topics/source.md"),
                &store,
                &slug_targets
            ),
            Some(graph::WikiGraphLinkTarget::Resolved(PathBuf::from(
                "knowledge/topics/rust-async.md"
            )))
        );
    }

    #[test]
    fn graph_resolves_relative_targets_from_source_document_directory() {
        let mut store = MemoryWikiStore::default();
        for path in [
            "knowledge/topics/nested/source.md",
            "knowledge/topics/nested/bar.md",
            "knowledge/topics/concepts/foo.md",
        ] {
            store.documents.insert(
                PathBuf::from(path),
                WikiDocument {
                    path: PathBuf::from(path),
                    kind: WikiDocumentKind::Topic,
                    title: None,
                    content_hash: "hash".to_string(),
                    body: String::new(),
                },
            );
        }
        let slug_targets = slug_target_map(&store);
        let source = Path::new("knowledge/topics/nested/source.md");

        assert_eq!(
            resolve_graph_target("bar.md", source, &store, &slug_targets),
            Some(graph::WikiGraphLinkTarget::Resolved(PathBuf::from(
                "knowledge/topics/nested/bar.md"
            )))
        );
        assert_eq!(
            resolve_graph_target("../concepts/foo.md", source, &store, &slug_targets),
            Some(graph::WikiGraphLinkTarget::Resolved(PathBuf::from(
                "knowledge/topics/concepts/foo.md"
            )))
        );
    }
}
