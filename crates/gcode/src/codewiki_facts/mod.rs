//! Typed, owned code-index facts exported by gcode.
//!
//! Structural graph views consume this datastore boundary for scoped files,
//! symbols, search, grep, graph queries, and leading content chunks. Renderers
//! built on `gobby_core::code_facts::FactsBundle` consume that shared contract.
//!
//! # Ownership
//!
//! This facade keeps its `config::Context` and read connection private; no
//! service configuration crosses the public facts API. Callers own output
//! formatting and diagnostics. Coupling graphs are derived from the typed
//! call and import facts exposed here.
//!
//! [`ensure_project_fresh`] is the sole non-query admission helper: it runs gcode's existing
//! project-scope freshness path, returns an owned status, and emits no warning.
//! The caller owns the quiet-dependent busy diagnostic.

use std::ops::{Deref, DerefMut};
use std::path::Path;
use std::sync::{Arc, Mutex, MutexGuard};

use anyhow::Context as _;
use postgres::Client;

use crate::config::Context;

mod communities;
mod graph;
mod graph_query;
mod scope;
mod search;
mod symbols;
mod text;

pub use crate::freshness::FreshnessStatus;
pub use communities::{CommunityFact, ProjectCommunities};
pub use graph::{
    GraphAvailability, GraphBounds, GraphDirection, GraphEdge, GraphEdgeKind, GraphNodeFact,
    GraphOutcome, GraphScopeMode, MAX_DECLARED_EDGE_LIMIT, ScopedGraph,
};
pub use graph_query::PublicEdge;
pub use scope::{FileFact, FileId, ScopeSelector};
pub use search::{ContentFact, SearchHit, SearchQuery};
pub use symbols::SymbolFact;
pub use text::{
    GrepContextLineFact, GrepHit, GrepOutcome, GrepQuery, GrepSpanFact, LeadingChunkFact,
};

/// Cheap-clone handle to gcode-owned CodeWiki facts.
///
/// The handle stores only immutable resolution context. Every datastore method
/// opens and owns a fresh read-only connection for that call.
#[derive(Clone)]
pub struct CodewikiFacts {
    context: Arc<Context>,
    read_connection: Arc<Mutex<Option<Client>>>,
}

struct ReadConnection<'a> {
    guard: MutexGuard<'a, Option<Client>>,
}

impl Deref for ReadConnection<'_> {
    type Target = Client;

    fn deref(&self) -> &Self::Target {
        // `read_connection` initializes the option before constructing this guard.
        self.guard.as_ref().expect("read connection initialized")
    }
}

impl DerefMut for ReadConnection<'_> {
    fn deref_mut(&mut self) -> &mut Self::Target {
        // `read_connection` initializes the option before constructing this guard.
        self.guard.as_mut().expect("read connection initialized")
    }
}

impl CodewikiFacts {
    /// Resolve the indexed project rooted at `project_root`.
    pub fn open(project_root: &Path) -> anyhow::Result<Self> {
        let project = project_root
            .to_str()
            .context("CodeWiki project root is not valid UTF-8")?;
        Ok(Self::from_context(Context::resolve(Some(project), true)?))
    }

    /// Stable project identity resolved by the datastore facade.
    pub fn project_id(&self) -> &str {
        &self.context.project_id
    }

    pub(crate) fn from_context(context: Context) -> Self {
        Self {
            context: Arc::new(context),
            read_connection: Arc::new(Mutex::new(None)),
        }
    }

    fn context(&self) -> &Context {
        &self.context
    }

    fn read_connection(&self) -> anyhow::Result<ReadConnection<'_>> {
        let mut guard = self
            .read_connection
            .lock()
            .map_err(|_| anyhow::anyhow!("CodeWiki facts read connection lock poisoned"))?;
        if guard.as_ref().is_none_or(Client::is_closed) {
            *guard = Some(crate::db::connect_readonly(&self.context.database_url)?);
        }
        Ok(ReadConnection { guard })
    }
}

/// Run gcode's project freshness admission without producing diagnostics.
pub fn ensure_project_fresh(
    project_root: &Path,
    disabled: bool,
) -> anyhow::Result<FreshnessStatus> {
    if disabled {
        return Ok(FreshnessStatus::Checked);
    }
    let facts = CodewikiFacts::open(project_root)?;
    crate::freshness::ensure_fresh(facts.context(), crate::freshness::FreshnessScope::Project)
}

#[cfg(test)]
mod tests;
