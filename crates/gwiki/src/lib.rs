#[cfg(feature = "ai")]
pub(crate) mod ai;
mod api;
mod error;
mod runner;
pub(crate) mod support;
#[cfg(test)]
pub(crate) mod test_http;

pub(crate) mod audit;
pub(crate) mod benchmark;
pub(crate) mod citations;
pub(crate) mod code_graph;
pub(crate) mod collect;
pub(crate) mod commands;
pub(crate) mod compile;
pub mod contract;
pub(crate) mod credibility;
pub(crate) mod daemon;
pub mod document;
pub mod explainer;
pub mod exports;
pub(crate) mod falkor_graph;
pub(crate) mod frontmatter;
pub(crate) mod graph;
pub(crate) mod health;
pub(crate) mod indexer;
pub(crate) mod ingest;
pub(crate) mod librarian;
pub(crate) mod links;
pub(crate) mod lint;
pub(crate) mod log;
pub(crate) mod markdown;
pub mod media;
pub mod models;
pub mod output;
pub(crate) mod paths;
pub(crate) mod provenance;
pub(crate) mod registry;
pub(crate) mod schema;
pub(crate) mod scope;
pub(crate) mod search;
pub mod session;
pub(crate) mod setup;
pub mod sources;
pub(crate) mod store;
pub mod synthesis;
pub(crate) mod transcribe;
pub(crate) mod vault;
pub(crate) mod vector;
pub(crate) mod video;
pub(crate) mod vision;

pub use api::{
    BenchmarkOptions, Command, CommandOutcome, CommandResult, IngestFileOptions, ReadTarget,
    ReviewReportOptions, ScopeIdentity, ScopeKind, ScopeSelection, SetupOptions,
    SyncSessionsOptions,
};
pub use error::WikiError;
pub use runner::run;
