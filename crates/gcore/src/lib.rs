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
pub mod provisioning;

// Always available - lightweight foundation modules.
pub mod ai_context;
pub mod ai_types;
pub mod config;
pub mod context;
pub mod degradation;
pub mod local_backend;
pub mod setup;

/// Return Gobby home, respecting `GOBBY_HOME` when set.
pub fn gobby_home() -> anyhow::Result<std::path::PathBuf> {
    if let Some(home) = std::env::var_os("GOBBY_HOME") {
        return Ok(std::path::PathBuf::from(home));
    }
    dirs::home_dir()
        .map(|home| home.join(".gobby"))
        .ok_or_else(|| anyhow::anyhow!("cannot determine home directory"))
}

// Feature-gated modules.
#[cfg(feature = "ai")]
pub mod ai;

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

#[cfg(test)]
pub(crate) mod test_http;
