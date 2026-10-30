//! Provider launch, exits and host reconciliation for node-local agent runs.
use super::{NodeAgents, sandbox::runtime::SrtInstallation};
use crate::node_commands::{ExitReason, SpawnPayload};
use std::{collections::BTreeMap, path::{Path, PathBuf}};

/// The node-level inputs a launch takes besides the payload.
#[derive(Debug, Clone)]
pub struct LaunchContext {
    /// The verified SRT install the chokepoint wraps with.
    pub installation: SrtInstallation,
    /// The node's own front door, which becomes the agent's `GOBBY_DAEMON_URL`.
    pub front_door_url: String,
    /// The user's home: provider trust stores and the sandbox home.
    pub home: PathBuf,
    /// The `PATH` the provider and `agy` binaries are found on.
    pub path_env: String,
    /// The node's ambient environment, which the native env policy filters.
    pub ambient: BTreeMap<String, String>,
    /// 2.4's cache variables for this run.
    pub cache_env: BTreeMap<String, String>,
    pub daemon_port: u16,
    pub websocket_port: u16,
    /// `sys.platform` spelling of the host: `darwin` or `linux`.
    pub platform: String,
}

/// Why a launch did not start a child.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LaunchFailure {
    pub reason: ExitReason,
    pub detail: String,
}

/// Prepares, spawns and commits one run's provider terminal and records it as launched.
pub async fn launch_with(
    _agents: &NodeAgents,
    _context: &LaunchContext,
    _run_id: &str,
    _payload: &SpawnPayload,
    _cwd: &Path,
) -> Result<(), LaunchFailure> {
    todo!("2.6 launch")
}

/// Subscribes to host events and commits an exit for each launched run that ends.
pub async fn watch_exits(_agents: &NodeAgents) -> anyhow::Result<()> {
    todo!("2.6 exit watcher")
}

/// Settles launched runs against the live host's inventory.
pub async fn reconcile(_agents: &NodeAgents) -> anyhow::Result<()> {
    todo!("2.6 reconcile")
}
