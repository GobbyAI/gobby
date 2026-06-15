use std::collections::{BTreeMap, BTreeSet};
use std::path::{Component, Path, PathBuf};

use postgres::Client;

use crate::WikiError;
use crate::graph::{
    WikiGraphDocument, WikiGraphFacts, WikiGraphLink, WikiGraphLinkTarget, WikiGraphSource,
};
use crate::search::SearchScope;
use crate::support::text::slugify;

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

pub(super) fn resolve_graph_target(
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

pub(super) fn slug_target_map(documents: &[WikiGraphDocument]) -> BTreeMap<String, PathBuf> {
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

fn is_external_target(target: &str) -> bool {
    let lower = target.to_ascii_lowercase();
    lower.starts_with("http://")
        || lower.starts_with("https://")
        || lower.starts_with("mailto:")
        || lower.starts_with("//")
        || target.starts_with(r"\\")
        || lower.contains("://")
}
