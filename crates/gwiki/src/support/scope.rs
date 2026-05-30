use crate::error::index_error_to_wiki_error;
use crate::{
    ScopeIdentity, ScopeSelection, WikiError, indexer, scope as wiki_scope, search, session, store,
};

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
    let scope = resolve_command_scope(selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let search_scope = search_scope_for_resolved(&scope);
    let mut store = store::MemoryWikiStore::default();
    if scope.root().is_dir() {
        indexer::index_vault(scope.root(), &mut store).map_err(index_error_to_wiki_error)?;
    }

    Ok((scope, output_scope, search_scope, store))
}

pub(crate) fn search_scope_for_resolved(scope: &wiki_scope::ResolvedScope) -> search::SearchScope {
    if let Some(topic) = scope.topic_name() {
        return search::SearchScope::topic(topic);
    }
    if let Some(project_id) = scope.project_id() {
        return search::SearchScope::project(project_id);
    }
    search::SearchScope::project("current")
}

pub(crate) fn store_scope_for_search(scope: &search::SearchScope) -> store::WikiStoreScope {
    match scope {
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
        source: error.to_string(),
    })?;
    wiki_scope::resolve(selection, &cwd)
}

pub(crate) fn research_scope_identity(scope: &session::ResearchScope) -> ScopeIdentity {
    match scope {
        session::ResearchScope::Project { .. } => ScopeIdentity::project("current"),
        session::ResearchScope::Topic { name, .. } => ScopeIdentity::topic(name.clone()),
    }
}

pub(crate) fn resolved_scope_identity(scope: &wiki_scope::ResolvedScope) -> ScopeIdentity {
    if let Some(topic) = scope.topic_name() {
        return ScopeIdentity::topic(topic);
    }

    if let Some(project_id) = scope.project_id() {
        return ScopeIdentity::project(project_id);
    }

    ScopeIdentity::project("current")
}
