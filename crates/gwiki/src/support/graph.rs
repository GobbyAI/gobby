use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use gobby_core::vault::lint::{link_lookup_keys, page_targets};

use crate::frontmatter::{WikiFrontmatter, parse_frontmatter};
use crate::graph;
use crate::links::{LinkKind, canonical_target_key};
#[cfg(test)]
use crate::{search, store};

use super::text::slugify;

#[cfg(test)]
pub(crate) fn memory_graph_from_store(
    store: &store::FakeWikiStore,
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
    let targets = graph_target_maps(store.documents.values().map(|document| {
        (
            &document.path,
            document.title.as_deref(),
            document.body.as_str(),
        )
    }));
    let links = store
        .links
        .values()
        .flat_map(|links| links.iter())
        .filter_map(|link| {
            resolve_graph_target(&link.target, &link.path, &targets).map(|target| {
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

/// Lookup maps for graph link resolution. `document_targets` holds the lint
/// target set ([`page_targets`]: relative path, stem, title, aliases) plus the
/// exact page-file path key; `slug_targets` is the slugified stem/title
/// fallback for prose-style mentions.
pub(crate) struct GraphTargetMaps {
    document_targets: BTreeMap<String, PathBuf>,
    slug_targets: BTreeMap<String, PathBuf>,
}

/// Build resolution maps from `(path, stored_title, body)` document rows.
/// Frontmatter is parsed for the title and aliases lint matches against;
/// malformed frontmatter never fails a graph build — lint reports it.
pub(crate) fn graph_target_maps<'a, I>(documents: I) -> GraphTargetMaps
where
    I: IntoIterator<Item = (&'a PathBuf, Option<&'a str>, &'a str)>,
{
    let mut document_targets = BTreeMap::<String, PathBuf>::new();
    let mut slug_targets = BTreeMap::<String, PathBuf>::new();
    for (path, stored_title, body) in documents {
        let metadata = parse_frontmatter(body)
            .map(|parsed| parsed.metadata)
            .unwrap_or_else(|_| WikiFrontmatter::empty());
        let title = metadata.title.as_deref().or(stored_title);

        // Deterministic collision behavior: first path in iteration order
        // wins for each key, and later stem/title/alias collisions keep it.
        for key in page_targets(path, title, &metadata.aliases) {
            document_targets.entry(key).or_insert_with(|| path.clone());
        }
        document_targets
            .entry(canonical_target_key(&path.to_string_lossy()))
            .or_insert_with(|| path.clone());

        if let Some(file_slug) = path
            .file_stem()
            .and_then(|value| value.to_str())
            .map(slugify)
        {
            slug_targets
                .entry(file_slug)
                .or_insert_with(|| path.clone());
        }
        if let Some(title_slug) = title.map(slugify) {
            slug_targets
                .entry(title_slug)
                .or_insert_with(|| path.clone());
        }
    }
    GraphTargetMaps {
        document_targets,
        slug_targets,
    }
}

/// Resolve a stored link target against the vault page set using lint's
/// candidate rule ([`link_lookup_keys`]): vault-root-relative first, then
/// ancestor-directory joins. Stored links do not persist their kind; the
/// wikilink rule's ancestor candidates are a superset of the markdown rule's
/// single-parent candidate, so unknown kinds resolve at least everything lint
/// resolves (#17638).
pub(crate) fn resolve_graph_target(
    raw_target: &str,
    source_path: &Path,
    targets: &GraphTargetMaps,
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

    for key in link_lookup_keys(source_path, LinkKind::Wikilink, &normalized) {
        if let Some(path) = targets.document_targets.get(&key) {
            return Some(graph::WikiGraphLinkTarget::Resolved(path.clone()));
        }
    }

    let target_slug = slugify(normalized.strip_suffix(".md").unwrap_or(&normalized));
    if let Some(path) = targets.slug_targets.get(&target_slug) {
        return Some(graph::WikiGraphLinkTarget::Resolved(path.clone()));
    }

    Some(graph::WikiGraphLinkTarget::Unresolved(normalized))
}

fn is_external_target(target: &str) -> bool {
    let lower = target.to_ascii_lowercase();
    target.is_empty()
        || lower.contains("://")
        || lower.starts_with("//")
        || target.starts_with(r"\\")
        || lower.starts_with("mailto:")
        || lower.starts_with("tel:")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::{
        FakeWikiStore, WikiDocument, WikiDocumentKind, WikiLink as StoreWikiLink, WikiSource,
    };

    #[test]
    fn graph_uses_distinct_source_document_paths() {
        let mut store = FakeWikiStore::default();
        store.documents.insert(
            PathBuf::from("knowledge/topics/rust.md"),
            WikiDocument {
                path: PathBuf::from("knowledge/topics/rust.md"),
                kind: WikiDocumentKind::Topic,
                title: Some("Rust".to_string()),
                content_hash: "hash".to_string(),
                frontmatter: serde_json::json!({}),
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

    fn store_target_maps(store: &FakeWikiStore) -> GraphTargetMaps {
        graph_target_maps(store.documents.values().map(|document| {
            (
                &document.path,
                document.title.as_deref(),
                document.body.as_str(),
            )
        }))
    }

    fn insert_document(store: &mut FakeWikiStore, path: &str, title: Option<&str>, body: &str) {
        store.documents.insert(
            PathBuf::from(path),
            WikiDocument {
                path: PathBuf::from(path),
                kind: WikiDocumentKind::Topic,
                title: title.map(str::to_string),
                content_hash: "hash".to_string(),
                frontmatter: serde_json::json!({}),
                body: body.to_string(),
            },
        );
    }

    #[test]
    fn graph_rejects_url_like_external_targets() {
        let store = FakeWikiStore::default();
        let targets = store_target_maps(&store);
        let source = Path::new("knowledge/topics/source.md");

        assert!(resolve_graph_target("//cdn.example.test/page", source, &targets).is_none());
        assert!(resolve_graph_target(r"\\server\share\page", source, &targets).is_none());
        assert!(resolve_graph_target("custom://example", source, &targets).is_none());
    }

    #[test]
    fn graph_resolves_slug_targets_from_precomputed_map() {
        let mut store = FakeWikiStore::default();
        for path in [
            "knowledge/topics/rust-async.md",
            "knowledge/topics/rust_async.md",
        ] {
            insert_document(
                &mut store,
                path,
                path.ends_with("rust-async.md").then_some("Rust Async"),
                "# Rust Async",
            );
        }
        let targets = store_target_maps(&store);

        assert_eq!(
            resolve_graph_target(
                "Rust Async",
                Path::new("knowledge/topics/source.md"),
                &targets
            ),
            Some(graph::WikiGraphLinkTarget::Resolved(PathBuf::from(
                "knowledge/topics/rust-async.md"
            )))
        );
        // Both stems slugify to rust-async, so the slug fallback alone would
        // misresolve this case variant; the folded path lookup must win.
        assert_eq!(
            resolve_graph_target(
                "Rust_Async.md",
                Path::new("knowledge/topics/source.md"),
                &targets
            ),
            Some(graph::WikiGraphLinkTarget::Resolved(PathBuf::from(
                "knowledge/topics/rust_async.md"
            ))),
            "document paths resolve case-insensitively"
        );
    }

    #[test]
    fn graph_resolves_relative_targets_from_source_document_directory() {
        let mut store = FakeWikiStore::default();
        for path in [
            "knowledge/topics/nested/source.md",
            "knowledge/topics/nested/bar.md",
            "knowledge/topics/concepts/foo.md",
        ] {
            insert_document(&mut store, path, None, "");
        }
        let targets = store_target_maps(&store);
        let source = Path::new("knowledge/topics/nested/source.md");

        assert_eq!(
            resolve_graph_target("bar.md", source, &targets),
            Some(graph::WikiGraphLinkTarget::Resolved(PathBuf::from(
                "knowledge/topics/nested/bar.md"
            )))
        );
        assert_eq!(
            resolve_graph_target("../concepts/foo.md", source, &targets),
            Some(graph::WikiGraphLinkTarget::Resolved(PathBuf::from(
                "knowledge/topics/concepts/foo.md"
            )))
        );
    }

    #[test]
    fn vault_root_relative_wikilinks_resolve_without_joining_source_directory() {
        // #17638 repro: a concept page links a digest by vault-root-relative
        // path. The old resolver joined the concept page's directory onto the
        // target, mangling it into knowledge/concepts/knowledge/sources/...
        // and surfacing a bogus link suggestion for a resolvable link.
        let digest = "knowledge/sources/src-5966419ee2f6bb38-session-019e4155.md";
        let mut store = FakeWikiStore::default();
        insert_document(&mut store, "knowledge/concepts/gcode.md", Some("gcode"), "");
        insert_document(&mut store, digest, Some("Session digest"), "");
        store.links.insert(
            PathBuf::from("knowledge/concepts/gcode.md"),
            vec![StoreWikiLink {
                path: PathBuf::from("knowledge/concepts/gcode.md"),
                target: "knowledge/sources/src-5966419ee2f6bb38-session-019e4155".to_string(),
                alias: Some("digest".to_string()),
                byte_start: 0,
                byte_end: 10,
            }],
        );

        let scope = search::SearchScope::project("project-1");
        let graph = memory_graph_from_store(&store, &scope);

        assert_eq!(
            graph.graph_facts_for_tests().links[0].target,
            graph::WikiGraphLinkTarget::Resolved(PathBuf::from(digest))
        );
        assert!(
            graph.link_suggestions(&scope, 10).is_empty(),
            "resolvable vault-root-relative links must not surface as suggestions"
        );
    }

    #[test]
    fn absolute_filesystem_targets_never_surface_as_suggestions() {
        // #17649 repro: session digests carry markdown links to scratchpad
        // files. An absolute filesystem path can never be a vault page, so it
        // must not become a page-creation suggestion; the link fact itself
        // stays unresolved so lint parity holds.
        let digest = "knowledge/sources/src-5966419ee2f6bb38-session-019e4155.md";
        let scratchpad = "/private/tmp/claude-501/scratchpad/note-orchid.md";
        let mut store = FakeWikiStore::default();
        insert_document(&mut store, digest, Some("Session digest"), "");
        store.links.insert(
            PathBuf::from(digest),
            vec![StoreWikiLink {
                path: PathBuf::from(digest),
                target: scratchpad.to_string(),
                alias: Some("note".to_string()),
                byte_start: 0,
                byte_end: 10,
            }],
        );

        let scope = search::SearchScope::project("project-1");
        let graph = memory_graph_from_store(&store, &scope);

        assert_eq!(
            graph.graph_facts_for_tests().links[0].target,
            graph::WikiGraphLinkTarget::Unresolved(scratchpad.to_string()),
            "the link fact must survive for lint parity"
        );
        assert!(
            graph.link_suggestions(&scope, 10).is_empty(),
            "absolute filesystem targets must not surface as page-creation suggestions"
        );
    }

    #[test]
    fn unresolved_targets_keep_their_written_form() {
        let mut store = FakeWikiStore::default();
        insert_document(&mut store, "knowledge/concepts/gcode.md", Some("gcode"), "");
        let targets = store_target_maps(&store);

        assert_eq!(
            resolve_graph_target(
                "knowledge/sources/missing-digest",
                Path::new("knowledge/concepts/gcode.md"),
                &targets
            ),
            Some(graph::WikiGraphLinkTarget::Unresolved(
                "knowledge/sources/missing-digest".to_string()
            )),
            "unresolved suggestions must not be mangled by directory joins"
        );
    }

    #[test]
    fn frontmatter_aliases_resolve_like_lint() {
        let mut store = FakeWikiStore::default();
        insert_document(
            &mut store,
            "knowledge/concepts/build-home.md",
            Some("Build Home"),
            "---\naliases:\n  - Build Home Dashboard\n---\n# Build Home\n",
        );
        let targets = store_target_maps(&store);

        assert_eq!(
            resolve_graph_target(
                "Build Home Dashboard",
                Path::new("knowledge/topics/source.md"),
                &targets
            ),
            Some(graph::WikiGraphLinkTarget::Resolved(PathBuf::from(
                "knowledge/concepts/build-home.md"
            ))),
            "alias targets lint resolves must not surface as suggestions"
        );
    }
}
