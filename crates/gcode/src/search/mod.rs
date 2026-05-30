//! Search modules.
//!
//! TODO: Graph boost is intentionally exposed but not wired into the top-level
//! CLI search path yet; ranking currently combines FTS and semantic sources.

pub mod fts;
pub mod graph_boost;
pub mod rrf;
pub mod semantic;
