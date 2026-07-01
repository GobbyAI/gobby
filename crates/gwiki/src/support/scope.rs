use std::path::Path;

use crate::{
    ScopeIdentity, ScopeKind, ScopeSelection, WikiError, indexer, scope as wiki_scope, search,
    store,
};

use super::config;

pub(crate) const DEFAULT_PROJECT_ID: &str = "current";

pub(crate) fn indexed_store_for_selection(
    selection: &ScopeSelection,
) -> Result<
    (
        wiki_scope::ResolvedScope,
        ScopeIdentity,
        search::SearchScope,
        store::MemoryWikiStore,
    ),
    WikiError,
> {
    let resolved = resolve_selection_context(selection)?;
    let index_options = config::local_index_options()?;
    let mut store = store::MemoryWikiStore::default();
    if resolved.scope.root().is_dir() {
        indexer::index_vault_with_options(resolved.scope.root(), &mut store, index_options)?;
    }

    Ok((
        resolved.scope,
        resolved.output_scope,
        resolved.search_scope,
        store,
    ))
}

pub(crate) struct ResolvedSelectionContext {
    pub(crate) scope: wiki_scope::ResolvedScope,
    pub(crate) output_scope: ScopeIdentity,
    pub(crate) search_scope: search::SearchScope,
}

pub(crate) fn resolve_selection_context(
    selection: &ScopeSelection,
) -> Result<ResolvedSelectionContext, WikiError> {
    let scope = resolve_command_scope(selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let search_scope = search_scope_for_resolved(&scope);
    Ok(ResolvedSelectionContext {
        scope,
        output_scope,
        search_scope,
    })
}

/// Convert a resolved vault scope into the search/store scope used by the
/// in-memory index. Topic scopes take precedence over project scopes because a
/// topic vault may still live inside a project checkout.
pub(crate) fn search_scope_for_resolved(scope: &wiki_scope::ResolvedScope) -> search::SearchScope {
    match topic_project_precedence(scope.topic_name(), scope.project_id()) {
        ScopePrecedence::Topic(topic) => search::SearchScope::topic(topic),
        ScopePrecedence::Project(project_id) => search::SearchScope::project(project_id),
        ScopePrecedence::DefaultProject => search::SearchScope::project(DEFAULT_PROJECT_ID),
    }
}

pub(crate) fn store_scope_for_search(scope: &search::SearchScope) -> store::WikiStoreScope {
    match scope {
        search::SearchScope::Global => {
            panic!("global search scope cannot be represented as a scoped wiki store")
        }
        search::SearchScope::Project { project_id } => store::WikiStoreScope::project(project_id),
        search::SearchScope::Topic { topic } => store::WikiStoreScope::topic(topic),
    }
}

pub(crate) fn resolve_command_scope(
    selection: &ScopeSelection,
) -> Result<wiki_scope::ResolvedScope, WikiError> {
    let cwd = std::env::current_dir().map_err(|error| WikiError::Io {
        action: "read current directory",
        path: None,
        source: error,
    })?;
    wiki_scope::resolve(selection, &cwd)
}

pub(crate) fn resolved_scope_identity(scope: &wiki_scope::ResolvedScope) -> ScopeIdentity {
    match topic_project_precedence(scope.topic_name(), scope.project_id()) {
        ScopePrecedence::Topic(topic) => ScopeIdentity::topic(topic),
        ScopePrecedence::Project(project_id) => ScopeIdentity::project(project_id),
        ScopePrecedence::DefaultProject => ScopeIdentity::project(DEFAULT_PROJECT_ID),
    }
}

pub(crate) fn scope_includes_page(scope: &ScopeIdentity, path: &Path) -> bool {
    match scope.kind {
        ScopeKind::Topic => path.starts_with(Path::new("knowledge").join("topics")),
        ScopeKind::Project | ScopeKind::Global => true,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ScopePrecedence<'a> {
    Topic(&'a str),
    Project(&'a str),
    DefaultProject,
}

fn topic_project_precedence<'a>(
    topic_name: Option<&'a str>,
    project_id: Option<&'a str>,
) -> ScopePrecedence<'a> {
    if let Some(topic) = topic_name {
        return ScopePrecedence::Topic(topic);
    }
    if let Some(project_id) = project_id {
        return ScopePrecedence::Project(project_id);
    }
    ScopePrecedence::DefaultProject
}
