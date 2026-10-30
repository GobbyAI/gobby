//! Provider trust seeds: the port of `pre_approve_directory` for claude and codex.
use anyhow::Result;
use std::path::Path;

/// Marks `workspace` and its real path trusted in the provider's store under `home`.
/// Droid, grok and agy have no store.
pub fn seed_trust(_provider: &str, _home: &Path, _workspace: &Path) -> Result<()> {
    todo!("2.6 trust seed")
}
