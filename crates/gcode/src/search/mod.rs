//! Search modules.
//!
//! Top-level search combines FTS, semantic vectors, and graph boost using
//! Reciprocal Rank Fusion. Semantic and graph infrastructure are part of the
//! supported full stack; hybrid callers may degrade to fewer sources when a
//! configured service is unavailable at query time.

pub mod fts;
pub mod graph_boost;
pub mod rrf;
