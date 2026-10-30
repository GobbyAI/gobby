//! Provider argv for a node launch: the port of `build_cli_command` and the per-provider
//! additions of the Python `prepare_*_spawn` plans.
use super::sandbox::runtime::SandboxLaunch;
use crate::node_commands::SpawnPayload;
use anyhow::Result;
use std::path::Path;

/// The provider's own argv, before the sandbox wrapper: `payload.provider` first.
pub fn build_command(
    _payload: &SpawnPayload,
    _cwd: &Path,
    _gobby_home: &Path,
    _sandbox_launch: &SandboxLaunch,
) -> Result<Vec<String>> {
    todo!("2.6 provider command")
}
