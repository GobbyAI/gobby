//! Repairs a prepared or reused workspace so its provider can run in it.

use anyhow::Result;
use std::{path::Path, time::Duration};

/// Node-local inputs that repair reads from the environment in production.
/// Tests inject them so nothing touches the real home or the real `uv`.
pub struct RepairEnv<'a> {
    /// Gobby home (`~/.gobby`); `ghook` and `gmcp` resolve under `bin/`.
    pub home: &'a Path,
    /// Droid hook root: `GOBBY_HOOKS_DIR`, or `<home>/hooks` when unset.
    pub hooks_dir: &'a Path,
    /// The user's `~/.claude.json`, patched for the claude provider.
    pub claude_json: &'a Path,
    /// `uv` for the environment seed; `None` skips the seed.
    pub uv: Option<&'a Path>,
    /// Upper bound on the `uv sync` seed.
    pub seed_timeout: Duration,
}

/// Runs the five repair steps for `provider` in `path`, copying hooks from
/// `source_root`. Warnings and skipped steps never fail the call.
pub async fn repair(
    source_root: &Path,
    path: &Path,
    provider: &str,
    env: &RepairEnv<'_>,
) -> Result<()> {
    let _ = (source_root, path, provider, env);
    todo!("repair: five provider steps are not implemented yet")
}
