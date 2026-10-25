//! Spawn-time SRT policy for node runs (plan 2.4).
//!
//! Behavior-free test-writer stub: the signatures below are what the tests in
//! `tests/node_agents.rs` and `tests/fixtures/node_srt/corpus.json` compile against.

use crate::node_commands::Sandbox;
use anyhow::Result;
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};

/// Every input the policy reads. Filesystem probes resolve against `home` and
/// `env`, never the process environment.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PolicyInputs {
    pub provider: String,
    pub sandbox: Sandbox,
    pub source_root: PathBuf,
    pub project_id: String,
    pub session_id: String,
    pub workspace: PathBuf,
    pub home: PathBuf,
    pub gobby_home: PathBuf,
    pub env: BTreeMap<String, String>,
    pub provider_executable: Option<PathBuf>,
    pub daemon_port: u16,
    pub websocket_port: u16,
    pub run_id: String,
    pub prewarm_pre_commit_store: bool,
}

/// The resolved policy for one `srt` run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SrtPolicy {
    /// Sandbox settings serialized with sorted keys and no spaces.
    pub settings: Vec<u8>,
    /// Environment additions the run receives: spawn caches, run root, cargo.
    pub env: BTreeMap<String, String>,
    /// Effective sandbox after the spawn-time grants.
    pub sandbox: Sandbox,
}

/// Resolve the `srt` policy for one run.
///
/// Refuses a grant under a sensitive or credential root with the Python
/// contract's message. Runs the pre-commit prewarm only when
/// `prewarm_pre_commit_store` is set.
pub fn resolve_srt_policy(_inputs: &PolicyInputs) -> Result<SrtPolicy> {
    todo!(
        "plan 2.4: port the spawn-time grants, run paths, compute_sandbox_paths and render_srt_settings"
    )
}

/// The cargo target component that names one checkout's sandbox target.
pub fn checkout_component(_checkout: &Path) -> String {
    todo!("plan 2.4: port sandbox_checkout_cargo_target_dir's checkout component")
}

/// The sixteen-hex digest that names one workspace's gcode runtime home.
pub fn gcode_runtime_digest(_workspace: &Path) -> String {
    todo!("plan 2.4: port gcode_runtime_write_exceptions's workspace digest")
}
