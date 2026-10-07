//! Full and incremental indexing orchestrator.
//!
//! Writes files, symbols, imports, calls, unresolved targets, and content chunks
//! to the PostgreSQL hub. External sync (Qdrant vectors, FalkorDB graph) is
//! delegated through projection sync status and handled outside this module.

mod file;
mod freshness_probe;
mod lifecycle;
mod local_imports;
mod overlay;
mod pipeline;
mod sink;
mod timing;
mod types;
mod util;

pub use freshness_probe::project_changed_since;
pub use lifecycle::invalidate;
#[cfg(all(test, gcode_postgres_tests))]
pub(crate) use lifecycle::refresh_communities;
pub(crate) use local_imports::{
    LocalImportRepair, resolve_project_local_import_calls, resolve_project_local_import_inheritance,
};
pub use pipeline::index_files;
pub(crate) use timing::IndexTimings;
pub use types::{
    CommunityRefreshReport, IndexDegradation, IndexDurations, IndexOptions, IndexOutcome,
    IndexProgressSink, IndexRequest, UnsupportedFileType,
};

#[cfg(test)]
mod stale_cleanup_tests;
#[cfg(test)]
mod tests;
