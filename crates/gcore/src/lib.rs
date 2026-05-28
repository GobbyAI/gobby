//! Shared primitives for Gobby CLI tools.
//!
//! The baseline crate stays dependency-light for consumers that only need
//! project discovery, bootstrap config, daemon URLs, and shared foundation
//! vocabulary. Datastore and indexing adapters sit behind Cargo feature gates
//! so small binaries do not inherit services they never call.

// Always available - existing modules.
pub mod bootstrap;
pub mod daemon_url;
pub mod project;

// Always available - lightweight foundation modules.
pub mod config;
pub mod context;
pub mod degradation;
pub mod setup;

// Feature-gated modules.
#[cfg(feature = "postgres")]
pub mod postgres;

#[cfg(feature = "falkor")]
pub mod falkor;

#[cfg(feature = "qdrant")]
pub mod qdrant;

#[cfg(feature = "indexing")]
pub mod indexing;

#[cfg(feature = "search")]
pub mod search;
