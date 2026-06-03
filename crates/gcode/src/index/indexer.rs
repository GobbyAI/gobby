//! Full and incremental indexing orchestrator.
//!
//! Writes files, symbols, imports, calls, unresolved targets, and content chunks
//! to the PostgreSQL hub. External sync (Qdrant vectors, FalkorDB graph) is
//! delegated through projection sync status and handled outside this module.

mod file;
mod freshness_probe;
mod lifecycle;
mod overlay;
mod pipeline;
mod sink;
mod types;
mod util;

pub use freshness_probe::project_changed_since;
pub use lifecycle::invalidate;
pub use pipeline::index_files;
pub use types::{
    IndexDegradation, IndexDurations, IndexOutcome, IndexRequest, UnsupportedFileType,
};

#[cfg(test)]
mod tests;
