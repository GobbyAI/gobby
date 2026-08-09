//! Typed, owned CodeWiki facts exported by gcode.
//!
//! This is the only datastore boundary consumed by the wiki-owned CodeWiki
//! engine. It is also the seed surface for #17678; renderers built on
//! `gobby_core::code_facts::FactsBundle` consume that shared contract instead.
//!
//! # Dependency inventory
//!
//! The ownership move started with
//! `gcode grep 'crate::' crates/gcode/src/commands/codewiki`. Every hit is in
//! one of these buckets:
//!
//! - `db`, `visibility`, `models::Symbol`, `search::fts`, `commands::grep`,
//!   `graph::{code_graph,typed_query}`, and `commands::scope` are datastore
//!   consumers. They map to the scoped-file, symbol, search, grep, graph, and
//!   leading-chunk families in this module. The grouped
//!   `use crate::{db, visibility}` form in `tool_executor.rs` is included.
//! - `config::Context` in `run`, `graph`, compare/purge, AI generation/routing,
//!   and the tool executor splits at the boundary: this facade resolves and
//!   keeps a private context for facts, while project identity, output format,
//!   quiet/verbose behavior, and AI/daemon routing live in gwiki's runtime
//!   carrier. No context or service configuration crosses this API.
//! - `output`, `index::hasher`, storage-path normalization, language detection,
//!   and `index::security` are output or filesystem concerns. They move with
//!   the engine; filesystem-only tool operations remain there.
//! - `commands::codewiki::*` imports are engine-internal references and move as
//!   a unit. Coupling remains an engine-derived projection over typed call and
//!   import facts.
//!
//! The command wrapper was inventoried separately. [`ensure_project_fresh`]
//! is the sole non-query admission helper: it runs gcode's existing
//! project-scope freshness path, returns an owned status, and emits no warning.
//! The generation-path caller owns the quiet-dependent busy diagnostic.

use std::path::Path;
use std::sync::Arc;

use anyhow::Context as _;
use postgres::Client;

use crate::config::Context;

mod graph;
mod scope;
mod search;
mod symbols;
mod text;

pub use crate::freshness::FreshnessStatus;
pub use graph::{GraphAvailability, GraphEdge, GraphEdgeKind, GraphNodeFact, GraphOutcome};
pub use scope::{FileFact, FileId, ScopeSelector};
pub use search::{SearchHit, SearchQuery};
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
}

impl CodewikiFacts {
    /// Resolve the indexed project rooted at `project_root`.
    pub fn open(project_root: &Path) -> anyhow::Result<Self> {
        let project = project_root
            .to_str()
            .context("CodeWiki project root is not valid UTF-8")?;
        Ok(Self::from_context(Context::resolve(Some(project), true)?))
    }

    fn from_context(context: Context) -> Self {
        Self {
            context: Arc::new(context),
        }
    }

    fn context(&self) -> &Context {
        &self.context
    }

    fn read_connection(&self) -> anyhow::Result<Client> {
        crate::db::connect_readonly(&self.context.database_url)
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
