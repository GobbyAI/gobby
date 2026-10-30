//! Provider environment for a node launch: the port of the Python `prepare_*_spawn`
//! environment layers and `apply_native_env_policy`.
use super::sandbox::runtime::SandboxLaunch;
use crate::node_commands::SpawnPayload;
use anyhow::Result;
use std::{collections::BTreeMap, path::Path};

/// What the native host spawns with: the argv prefix that unsets denied ambient keys, and
/// the environment.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProviderEnv {
    pub argv_prefix: Vec<String>,
    pub env: BTreeMap<String, String>,
}

/// Layers the identity, front door, prompt file, cache, `extra_env`, provider endpoint and
/// sandbox variables, then applies the native env policy over `ambient`.
pub fn build_env(
    _payload: &SpawnPayload,
    _gobby_home: &Path,
    _front_door_url: &str,
    _prompt_file: &Path,
    _cache_env: &BTreeMap<String, String>,
    _sandbox_launch: &SandboxLaunch,
    _ambient: &BTreeMap<String, String>,
) -> Result<ProviderEnv> {
    todo!("2.6 provider environment")
}
