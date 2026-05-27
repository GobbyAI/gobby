//! Shared primitives for Gobby CLI tools.
//!
//! Small, dependency-light helpers that multiple Gobby binaries (`gcode`,
//! `gsqz`, `gloc`, `ghook`) share: project-root walk-up, project-id reading,
//! bootstrap config resolution, daemon URL construction.

pub mod bootstrap;
pub mod daemon_url;
#[cfg(feature = "indexing")]
pub mod indexing;
pub mod project;
