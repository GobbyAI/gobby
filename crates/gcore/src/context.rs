//! Shared runtime context boundary.
//!
//! Consumer crates keep their CLI flags and domain state locally. This module
//! owns the public location for cross-crate project, daemon, and service context
//! types as the Rust foundation expands.
