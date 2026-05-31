//! Search modules.
//!
//! Top-level search combines FTS, optional semantic vectors, and optional graph
//! boost using Reciprocal Rank Fusion.

pub mod fts;
pub mod graph_boost;
pub mod rrf;
pub mod semantic;
