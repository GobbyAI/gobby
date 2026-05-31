mod api;
mod error;
mod runner;
pub(crate) mod support;

// TODO(#357): Narrow these broad library-build allowances as gwiki CLI-only
// modules are split from reusable public APIs.
#[allow(dead_code)]
pub(crate) mod audit;
#[allow(dead_code)]
pub(crate) mod citations;
#[allow(dead_code)]
pub(crate) mod collect;
#[allow(dead_code)]
pub(crate) mod commands;
#[allow(dead_code)]
pub(crate) mod compile;
#[allow(dead_code)]
pub(crate) mod credibility;
#[allow(dead_code)]
pub(crate) mod daemon;
#[allow(dead_code)]
pub(crate) mod events;
pub mod exports;
#[allow(dead_code)]
pub(crate) mod frontmatter;
#[allow(dead_code)]
pub(crate) mod graph;
#[allow(dead_code)]
pub(crate) mod health;
#[allow(dead_code)]
pub(crate) mod indexer;
#[allow(dead_code)]
pub(crate) mod ingest;
#[allow(dead_code)]
pub(crate) mod links;
#[allow(dead_code)]
pub(crate) mod lint;
#[allow(dead_code)]
pub(crate) mod log;
#[allow(dead_code)]
pub(crate) mod markdown;
pub mod models;
pub mod output;
#[allow(dead_code)]
pub(crate) mod provenance;
#[allow(dead_code)]
pub(crate) mod registry;
pub mod research;
#[allow(dead_code)]
pub(crate) mod schema;
#[allow(dead_code)]
pub(crate) mod scope;
#[allow(dead_code)]
pub(crate) mod search;
pub mod session;
#[allow(dead_code)]
pub(crate) mod setup;
pub mod sources;
#[allow(dead_code)]
pub(crate) mod store;
pub mod synthesis;
#[allow(dead_code)]
pub(crate) mod transcribe;
#[allow(dead_code)]
pub(crate) mod vault;
#[allow(dead_code)]
pub(crate) mod video;
#[allow(dead_code)]
pub(crate) mod vision;

pub use api::{Command, CommandOutcome, CommandResult, ScopeIdentity, ScopeKind, ScopeSelection};
pub use error::WikiError;
pub use runner::{resolve_research_scope, run};
