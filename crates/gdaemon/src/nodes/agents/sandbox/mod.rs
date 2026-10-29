//! Spawn-time SRT policy for node runs (plan 2.4). Ports the Python oracle's
//! spawn-time grants, run paths, `compute_sandbox_paths`, and `render_srt_settings`.
//! Each submodule owns one step; `resolve_srt_policy` runs them in oracle order.

mod caches;
mod grok_home;
mod monitor;
mod paths;
mod prewarm;
mod ps_shim;
pub mod runtime;
mod settings;
mod tables;
mod verify;
mod workspace_scope;

use crate::node_commands::{Sandbox, SandboxBackend};
use anyhow::{Result, bail};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    io,
    os::unix::ffi::OsStrExt,
    path::{Path, PathBuf},
    process::{Child, ExitStatus},
    thread,
    time::{Duration, Instant},
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
    /// `sys.platform` spelling of the host: `darwin` or `linux`.
    pub platform: String,
    /// Trusted host-resolved Darwin temp/cache roots; absent means query confstr.
    pub darwin_user_dirs: Option<[PathBuf; 2]>,
    /// The seat's definition, its session sequence number, and its epic sequence number
    /// when it shares an epic folder. They name the workspace folders the spawn grants
    /// (#25148); an absent value grants nothing for that folder.
    pub definition: Option<String>,
    pub session_seq: Option<u64>,
    pub epic_seq: Option<u64>,
    /// Authority resolved from the stored definition by the hub at launch/resume.
    pub read_all_agent_workspaces: bool,
    /// Existing same-definition predecessors selected by the hub's persistence,
    /// sequence and live-holder checks. Refresh at every launch/resume.
    pub predecessor_workspace_folders: Vec<PathBuf>,
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
pub fn resolve_srt_policy(inputs: &PolicyInputs) -> Result<SrtPolicy> {
    if !inputs.sandbox.enabled || !matches!(inputs.sandbox.backend, SandboxBackend::Srt) {
        bail!("resolve_srt_policy requires an enabled srt sandbox");
    }
    let compiled = compile_policy(inputs, &[], &[])?;
    let settings =
        settings::canonical_json(&settings::render(&compiled.resolved, &inputs.home)).into_bytes();
    let additions = compiled
        .env
        .into_iter()
        .filter(|(key, value)| inputs.env.get(key) != Some(value))
        .collect();
    Ok(SrtPolicy {
        settings,
        env: additions,
        sandbox: compiled.sandbox,
    })
}

/// One run's compiled policy, before its settings are rendered.
pub(super) struct CompiledPolicy {
    resolved: paths::ResolvedPaths,
    sandbox: Sandbox,
    env: BTreeMap<String, String>,
}

/// The compile step both launch paths share. `home_writes` and `home_denies` are a
/// private provider home's grants, added before the sandbox paths are computed.
pub(super) fn compile_policy(
    inputs: &PolicyInputs,
    home_writes: &[String],
    home_denies: &[String],
) -> Result<CompiledPolicy> {
    let mut env = inputs.env.clone();
    let project = caches::project_write_paths(&inputs.source_root)?;
    let mut config = inputs.sandbox.clone();
    caches::grant_declared_nextest(&mut config, &inputs.source_root, &project);
    let workspace_scope = caches::grant_workspace_folders(&mut config, inputs)?;
    env.insert(
        "CARGO_TARGET_DIR".to_string(),
        caches::agent_cargo_target_dir(inputs),
    );
    let spawned = caches::spawn_grants(config, &project, &mut env, inputs);
    let layout = caches::run_layout(inputs, &env)?;
    if inputs.prewarm_pre_commit_store {
        prewarm::prewarm(
            &inputs.workspace,
            &layout.cache.join("xdg-cache-home").join("pre-commit"),
            &paths::managed_execution_root(&inputs.gobby_home),
            &env,
            &inputs.home,
        );
    }

    let mut effective = spawned;
    let run_grants = layout
        .writable()
        .into_iter()
        .map(|path| paths::text(&path))
        .chain([
            paths::text(&layout.cargo_home()),
            paths::text(&layout.cargo_target),
        ]);
    effective.extra_write_paths = paths::dedupe(
        effective
            .extra_write_paths
            .into_iter()
            .chain(run_grants)
            .chain(home_writes.iter().cloned()),
    );
    effective.extra_deny_write_paths = paths::dedupe(
        effective
            .extra_deny_write_paths
            .into_iter()
            .chain(home_denies.iter().cloned()),
    );

    let mut policy_env = env;
    policy_env.extend(layout.environment(&inputs.provider));
    let resolved = paths::compute(&effective, inputs, &policy_env, &workspace_scope)?;
    Ok(CompiledPolicy {
        resolved,
        sandbox: effective,
        env: policy_env,
    })
}

/// The cargo target component that names one checkout's sandbox target: the
/// checkout's name plus a digest of its resolved path.
pub fn checkout_component(checkout: &Path) -> String {
    let canonical = paths::resolve_nonstrict(checkout);
    let name = canonical
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    format!(
        "{}-{}",
        caches::safe_component(&name, 64, "checkout"),
        hash16(canonical.as_os_str().as_bytes())
    )
}

/// The sixteen-hex digest that names one workspace's gcode runtime home.
pub fn gcode_runtime_digest(workspace: &Path) -> String {
    hash16(paths::resolve_nonstrict(workspace).as_os_str().as_bytes())
}

/// The first sixteen lowercase hex digits of `sha256(bytes)`.
fn hash16(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .take(8)
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// Waits for `child` up to `timeout`. On expiry the child is killed and reaped, and
/// `None` comes back.
fn wait_bounded(child: &mut Child, timeout: Duration) -> io::Result<Option<ExitStatus>> {
    let started = Instant::now();
    loop {
        if let Some(status) = child.try_wait()? {
            return Ok(Some(status));
        }
        if started.elapsed() >= timeout {
            let _ = child.kill();
            let _ = child.wait();
            return Ok(None);
        }
        thread::sleep(Duration::from_millis(20));
    }
}
