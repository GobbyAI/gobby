//! The single SRT launch chokepoint (plan 2.5, ROADMAP decision 6).
//!
//! Behavior-free stubs. The signatures and the `SandboxLaunch` fields are pinned by
//! `crates/gdaemon/tests/node_agents.rs` for task #24374; every body is `todo!()`
//! until the port lands.

use super::PolicyInputs;
use crate::node_commands::Sandbox;
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};

/// Managed SRT release the Rust side pins. Must equal `SRT_RELEASE.version` in Python.
pub const SRT_VERSION: &str = "";

/// A refusal carrying the lockout text the Python oracle reports.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SandboxError {
    message: String,
}

impl std::fmt::Display for SandboxError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for SandboxError {}

/// A verified managed install: its root, the node that runs it, and its runner.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SrtInstallation {
    pub root: PathBuf,
    pub node: PathBuf,
    pub runner: PathBuf,
}

/// A launch the chokepoint prepared for one run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SandboxLaunch {
    pub node: PathBuf,
    pub runner: PathBuf,
    pub settings: PathBuf,
    pub violations: PathBuf,
    /// Provider arguments the sandbox adds: codex only, `[]` for every other provider.
    pub provider_args: Vec<String>,
    /// The complete run environment.
    pub env: BTreeMap<String, String>,
    pub read_paths: Vec<PathBuf>,
    pub write_paths: Vec<PathBuf>,
    pub deny_paths: Vec<PathBuf>,
}

impl SandboxLaunch {
    /// Wraps `argv` as `[node, runner, --settings, ..., --, <provider>, ...]`.
    pub fn wrap(&self, _argv: &[String]) -> Vec<String> {
        todo!()
    }
}

/// Refuses any sandbox that is not `enabled` with backend `srt`.
pub fn require_managed_srt(_sandbox: &Sandbox) -> Result<(), SandboxError> {
    todo!()
}

/// Verifies the managed install under `gobby_home`.
pub fn verify_srt_installation(_gobby_home: &Path) -> Result<SrtInstallation, SandboxError> {
    todo!()
}

/// Checks every file the install manifest names, and refuses drift from it.
pub fn verify_srt_content(
    _root: &Path,
    _manifest: &BTreeMap<String, String>,
) -> Result<(), SandboxError> {
    todo!()
}

/// Verifies the install, then prepares the launch.
pub fn prepare_launch(
    _inputs: &PolicyInputs,
    _prompt_file: &Path,
) -> Result<SandboxLaunch, SandboxError> {
    todo!()
}

/// Prepares the launch against an already verified installation.
pub fn prepare_launch_with(
    _inputs: &PolicyInputs,
    _prompt_file: &Path,
    _installation: &SrtInstallation,
) -> Result<SandboxLaunch, SandboxError> {
    todo!()
}
