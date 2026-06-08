#[cfg(feature = "ai")]
#[allow(dead_code)]
pub(crate) mod ai;
mod api;
mod error;
mod runner;
pub(crate) mod support;
#[cfg(test)]
pub(crate) mod test_http;

// TODO(#357): Narrow these broad library-build allowances as gwiki CLI-only
// modules are split from reusable public APIs.
#[allow(dead_code)]
pub(crate) mod audit;
#[allow(dead_code)]
pub(crate) mod benchmark;
#[allow(dead_code)]
pub(crate) mod citations;
#[allow(dead_code)]
pub(crate) mod code_graph;
#[allow(dead_code)]
pub(crate) mod collect;
#[allow(dead_code)]
pub(crate) mod commands;
#[allow(dead_code)]
pub(crate) mod compile;
pub mod contract;
#[allow(dead_code)]
pub(crate) mod credibility;
#[allow(dead_code)]
pub(crate) mod daemon;
pub mod document;
#[allow(dead_code)]
pub(crate) mod events;
pub mod exports;
#[allow(dead_code)]
pub(crate) mod falkor_graph;
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
pub(crate) mod librarian;
#[allow(dead_code)]
pub(crate) mod links;
#[allow(dead_code)]
pub(crate) mod lint;
#[allow(dead_code)]
pub(crate) mod log;
#[allow(dead_code)]
pub(crate) mod markdown;
pub mod media;
pub mod models;
pub mod output;
#[allow(dead_code)]
pub(crate) mod provenance;
#[allow(dead_code)]
pub(crate) mod registry;
pub mod research;
pub(crate) mod research_loop;
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
pub(crate) mod vector;
#[allow(dead_code)]
pub(crate) mod video;
#[allow(dead_code)]
pub(crate) mod vision;

pub use api::{
    Command, CommandOutcome, CommandResult, IngestFileOptions, ReadTarget, ReviewReportOptions,
    ScopeIdentity, ScopeKind, ScopeSelection, SetupOptions,
};
pub use error::WikiError;
pub use runner::{resolve_research_scope, run};
