use std::path::Path;

use crate::{
    ScopeIdentity, ScopeKind, ScopeSelection, WikiError, scope as wiki_scope, search, store,
};

pub(crate) const DEFAULT_PROJECT_ID: &str = "current";

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
    if let Some(root) = selection.project_root() {
        crate::support::env::set_active_project_root(Some(root.to_path_buf()));
    } else if let Some(root) = gobby_core::project::find_project_root(&cwd) {
        crate::support::env::set_active_project_root(Some(root));
    }
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
