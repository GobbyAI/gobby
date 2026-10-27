//! The single SRT launch chokepoint (plan 2.5, ROADMAP decision 6).
//!
//! Every managed SRT launch goes through `prepare_launch_with`: the refusals run
//! first, then the run directories, the private provider home, the compiled policy,
//! the shared monitor, the policy files, and the preflight. `SandboxLaunch::wrap` is
//! the only place a provider argv gets wrapped.

pub use super::verify::{verify_srt_content, verify_srt_installation};
use super::{PolicyInputs, caches, compile_policy, grok_home, monitor, paths, ps_shim, settings};
use crate::node_commands::{Sandbox, SandboxBackend};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::{self, DirBuilder},
    io::{self, Read, Write},
    os::fd::AsRawFd,
    os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    process::{ChildStderr, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

/// Managed SRT release the Rust side pins. Equals `SRT_RELEASE.version` in Python.
pub const SRT_VERSION: &str = super::verify::SRT_RELEASE.version;

const SRT_SETTINGS_RELATIVE_PATH: &str = "assets/settings.json";
const SRT_VIOLATIONS_RELATIVE_PATH: &str = "logs/violations.jsonl";
const PREFLIGHT_TIMEOUT: Duration = Duration::from_secs(90);

/// A refusal carrying the lockout text the Python oracle reports.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SandboxError {
    message: String,
}

impl SandboxError {
    pub(super) fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
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
    /// SHA-256 of the canonical effective policy bytes written to settings.
    pub policy_hash: String,
    pub violations: PathBuf,
    /// The resolved provider executable that replaces the argv's first element.
    pub provider_executable: PathBuf,
    /// Provider arguments the sandbox adds: codex only, `[]` for every other provider.
    pub provider_args: Vec<String>,
    /// The complete run environment.
    pub env: BTreeMap<String, String>,
    pub read_paths: Vec<PathBuf>,
    pub write_paths: Vec<PathBuf>,
    pub deny_paths: Vec<PathBuf>,
}

impl SandboxLaunch {
    /// Wraps `argv` as `[node, runner, --settings, ..., --, <provider>, ...]`. An
    /// empty argv yields only the wrapper prefix.
    pub fn wrap(&self, argv: &[String]) -> Vec<String> {
        let mut command = vec![
            self.node.display().to_string(),
            self.runner.display().to_string(),
            "--settings".to_string(),
            self.settings.display().to_string(),
            "--violations".to_string(),
            self.violations.display().to_string(),
            "--".to_string(),
        ];
        if let Some((_, rest)) = argv.split_first() {
            command.push(self.provider_executable.display().to_string());
            command.extend(rest.iter().cloned());
        }
        command
    }
}

/// Refuses any sandbox that is not `enabled` with backend `srt`.
pub fn require_managed_srt(sandbox: &Sandbox) -> Result<(), SandboxError> {
    if sandbox.enabled && sandbox.backend == SandboxBackend::Srt {
        return Ok(());
    }
    let backend = match sandbox.backend {
        SandboxBackend::Srt => "srt",
        SandboxBackend::ProviderNative => "provider-native",
    };
    let enabled = if sandbox.enabled { "True" } else { "False" };
    Err(SandboxError::new(format!(
        "spawn_agent requires managed SRT (enabled={enabled}, backend={backend})"
    )))
}

/// Verifies the managed install under `inputs.gobby_home`, then prepares the launch.
pub fn prepare_launch(
    inputs: &PolicyInputs,
    prompt_file: &Path,
) -> Result<SandboxLaunch, SandboxError> {
    require_managed_srt(&inputs.sandbox)?;
    let installation = verify_srt_installation(&inputs.gobby_home)?;
    prepare_launch_with(inputs, prompt_file, &installation)
}

/// Prepares the launch against an already verified installation.
pub fn prepare_launch_with(
    inputs: &PolicyInputs,
    prompt_file: &Path,
    installation: &SrtInstallation,
) -> Result<SandboxLaunch, SandboxError> {
    require_managed_srt(&inputs.sandbox)?;
    if inputs.provider == "grok" && inputs.platform == "linux" {
        return Err(SandboxError::new(
            "Linux SRT cannot safely persist Grok auth refresh: atomic temporary-file \
             grants and replacement of the auth bind mount are unsupported",
        ));
    }
    if inputs.sandbox.allow_network {
        return Err(SandboxError::new(
            "SRT does not accept unrestricted network access; configure explicit \
             allowed_domains or enable a scoped Git/package capability",
        ));
    }
    let provider_executable = inputs.provider_executable.as_deref().ok_or_else(|| {
        SandboxError::new(format!("{} executable not found in PATH", inputs.provider))
    })?;
    let provider_executable = fs::canonicalize(provider_executable).map_err(|error| {
        SandboxError::new(format!(
            "Failed to resolve {} executable {}: {error}",
            inputs.provider,
            provider_executable.display()
        ))
    })?;

    let layout = caches::run_layout(inputs, &inputs.env).map_err(lift)?;
    let assets = layout.root.join("assets");
    create_run_directories(&layout, &assets)?;

    let mut run_env = BTreeMap::new();
    let mut home_writes = Vec::new();
    let mut home_denies = Vec::new();
    if inputs.provider == "grok" {
        let home = grok_home::prepare(inputs, &layout.cache, &assets).map_err(lift)?;
        run_env.insert("GROK_HOME".to_string(), paths::text(&home.home));
        run_env.insert(
            "GROK_AUTH_PATH".to_string(),
            paths::text(&home.home.join("auth.json")),
        );
        home_writes = home.runtime_write_paths;
        home_denies = home.protected_write_paths;
    }
    if prompt_file.is_file() {
        let copied = assets.join("prompt.md");
        fs::copy(prompt_file, &copied)
            .and_then(|_| fs::set_permissions(&copied, fs::Permissions::from_mode(0o600)))
            .map_err(|error| SandboxError::new(format!("prompt copy failed: {error}")))?;
        run_env.insert("GOBBY_PROMPT_FILE".to_string(), paths::text(&copied));
    }

    let mut run_inputs = inputs.clone();
    run_inputs.env.extend(run_env);
    let compiled = compile_policy(&run_inputs, &home_writes, &home_denies).map_err(lift)?;
    caches::recheck_cache_grants(&layout, &compiled.resolved.write_paths).map_err(lift)?;
    let gobby = inputs.gobby_home.as_path();
    let home = inputs.home.as_path();
    let managed_root = paths::resolve_nonstrict(&paths::managed_execution_root(gobby));
    let own_root = paths::resolve_nonstrict(&layout.root);
    for allowed in compiled
        .resolved
        .read_paths
        .iter()
        .chain(&compiled.resolved.write_paths)
    {
        let allowed_root = paths::resolve_nonstrict(Path::new(&paths::subtree_prefix(allowed)));
        let is_glob = allowed.contains(['*', '?', '[', '{']);
        if managed_root.starts_with(&allowed_root)
            || (allowed_root.starts_with(&managed_root)
                && (is_glob || !allowed_root.starts_with(&own_root)))
        {
            return Err(SandboxError::new(
                "Sandbox grants cannot expose another managed execution",
            ));
        }
    }

    let mut resolved = compiled.resolved;
    // Resolved like every compiled grant, as Python resolves them before rendering.
    resolved
        .read_paths
        .push(paths::text(&paths::resolve_nonstrict(&assets)));
    resolved
        .read_paths
        .push(paths::text(&paths::resolve_nonstrict(&installation.root)));
    let shim_dir = ps_shim::droid_shim_dir(gobby, &inputs.platform);
    if let Some(dir) = &shim_dir {
        resolved
            .read_paths
            .push(paths::text(&paths::resolve_nonstrict(dir)));
    }
    paths::assert_sensitive_path_contract(
        &[
            resolved.read_paths.as_slice(),
            resolved.write_paths.as_slice(),
        ],
        gobby,
        home,
        &run_inputs.env,
    )
    .map_err(lift)?;

    let mut env = compiled.env;
    if let Some(dir) = &shim_dir {
        let existing = env.get("PATH").cloned().unwrap_or_default();
        let joined = if existing.is_empty() {
            paths::text(dir)
        } else {
            format!("{}:{existing}", paths::text(dir))
        };
        env.insert("PATH".to_string(), joined);
    }
    let run_key = layout
        .root
        .file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .into_owned();
    if inputs.platform == "darwin" {
        let socket = monitor::ensure(
            gobby,
            &installation.node,
            &installation.runner,
            &env,
            &run_key,
        )
        .map_err(|error| {
            SandboxError::new(format!("Shared SRT violation monitor unavailable: {error}"))
        })?;
        env.insert("GOBBY_SRT_MONITOR_SOCKET".to_string(), paths::text(&socket));
        env.insert("GOBBY_SRT_RUN_KEY".to_string(), run_key);
    }

    let settings_bytes = settings::canonical_json(&settings::render(&resolved, home)).into_bytes();
    let settings_path = layout.root.join(SRT_SETTINGS_RELATIVE_PATH);
    let violations_path = layout.root.join(SRT_VIOLATIONS_RELATIVE_PATH);
    write_private_file(&settings_path, &settings_bytes)?;
    write_private_file(&violations_path, b"")?;

    let mux_tmpdir = if inputs.platform == "linux" {
        layout.tmp.clone()
    } else {
        monitor::mux_tmpdir(gobby, &env).map_err(lift)?
    };
    env.insert("GOBBY_SRT_TMPDIR".to_string(), paths::text(&mux_tmpdir));

    let launch = SandboxLaunch {
        policy_hash: format!("{:x}", Sha256::digest(&settings_bytes)),
        node: installation.node.clone(),
        runner: installation.runner.clone(),
        settings: settings_path,
        violations: violations_path,
        provider_executable,
        // A snapshot can restore a stale PATH after this run's shell startup.
        provider_args: if inputs.provider == "codex" {
            vec![
                "-c".to_string(),
                "features.shell_snapshot=false".to_string(),
            ]
        } else {
            Vec::new()
        },
        env,
        read_paths: resolved.read_paths.iter().map(PathBuf::from).collect(),
        write_paths: resolved.write_paths.iter().map(PathBuf::from).collect(),
        deny_paths: resolved
            .deny_read_paths
            .iter()
            .chain(&resolved.deny_write_paths)
            .map(PathBuf::from)
            .collect(),
    };
    preflight(&launch, &inputs.workspace)?;
    Ok(launch)
}

/// Creates the run's directories, each private to the daemon user.
fn create_run_directories(layout: &caches::RunLayout, assets: &Path) -> Result<(), SandboxError> {
    let cargo_home = layout.cargo_home();
    let dirs = [
        layout.root.as_path(),
        assets,
        layout.tmp.as_path(),
        layout.hooks.as_path(),
        layout.logs.as_path(),
        layout.cache.as_path(),
        cargo_home.as_path(),
        layout.cargo_target.as_path(),
    ];
    for dir in dirs {
        DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(dir)
            .map_err(|error| SandboxError::new(format!("create {}: {error}", dir.display())))?;
    }
    Ok(())
}

/// `_preflight_srt`: the runner's `--preflight` must exit 0 within the bound.
fn preflight(launch: &SandboxLaunch, workspace: &Path) -> Result<(), SandboxError> {
    if !launch.node.is_absolute() {
        return Err(SandboxError::new(
            "Node.js executable must have an absolute PATH spelling",
        ));
    }
    let mut command = launch.wrap(&[]);
    command.pop();
    command.push("--preflight".to_string());
    run_preflight(&command, workspace, &launch.env, PREFLIGHT_TIMEOUT)
}

/// Stderr kept from a preflight run: enough for a diagnostic line, never unbounded.
const PREFLIGHT_DETAIL_LIMIT: usize = 64 * 1024;

/// Makes a child's stderr nonblocking, so a read returns at once when nothing is buffered.
fn set_nonblocking(pipe: &ChildStderr) -> io::Result<()> {
    // SAFETY: the descriptor belongs to `pipe`, which stays alive for this call.
    // F_GETFL takes no third argument; F_SETFL keeps the existing flags.
    let fd = pipe.as_raw_fd();
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// Moves the bytes buffered on `stderr` into `detail`. It never blocks and never waits
/// for EOF, so a descendant that inherited the pipe cannot hold the launch.
fn drain_buffered(stderr: &mut Option<ChildStderr>, detail: &mut Vec<u8>) {
    let Some(pipe) = stderr.as_mut() else {
        return;
    };
    let mut bytes = [0; 4096];
    while detail.len() < PREFLIGHT_DETAIL_LIMIT {
        match pipe.read(&mut bytes) {
            Ok(count) if count > 0 => detail.extend_from_slice(&bytes[..count]),
            Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
            // EOF, or WouldBlock when nothing more is buffered right now.
            _ => break,
        }
    }
    detail.truncate(PREFLIGHT_DETAIL_LIMIT);
}

/// Runs a preflight command within `timeout`, refusing a nonzero exit.
fn run_preflight(
    command: &[String],
    workspace: &Path,
    env: &BTreeMap<String, String>,
    timeout: Duration,
) -> Result<(), SandboxError> {
    let mut child = Command::new(&command[0])
        .args(&command[1..])
        .current_dir(workspace)
        .envs(env)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|_| SandboxError::new("managed SRT preflight could not start"))?;
    let mut stderr = child.stderr.take();
    if stderr.as_ref().map_or(Ok(()), set_nonblocking).is_err() {
        let _ = child.kill();
        let _ = child.wait();
        return Err(SandboxError::new("managed SRT preflight execution failed"));
    }
    let mut detail = Vec::new();
    let started = Instant::now();
    let status = loop {
        drain_buffered(&mut stderr, &mut detail);
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if started.elapsed() < timeout => {
                thread::sleep(Duration::from_millis(20));
            }
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(SandboxError::new(format!(
                    "managed SRT preflight timed out after {:.1}s (bound {}s)",
                    started.elapsed().as_secs_f64(),
                    timeout.as_secs_f64()
                )));
            }
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(SandboxError::new("managed SRT preflight execution failed"));
            }
        }
    };
    drain_buffered(&mut stderr, &mut detail);
    if status.success() {
        return Ok(());
    }
    let detail = String::from_utf8_lossy(&detail);
    let detail = detail.trim();
    Err(SandboxError::new(format!(
        "managed SRT preflight failed: {}",
        if detail.is_empty() {
            "unknown error"
        } else {
            detail
        }
    )))
}

/// `_write_private_file`: an exclusive 0600 temporary file, fsynced, then renamed.
fn write_private_file(path: &Path, content: &[u8]) -> Result<(), SandboxError> {
    let name = path
        .file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .into_owned();
    let temp = path.with_file_name(format!(".{name}.{}.tmp", std::process::id()));
    let written = (|| -> std::io::Result<()> {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temp)?;
        file.write_all(content)?;
        file.sync_all()?;
        fs::rename(&temp, path)?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))
    })();
    let _ = fs::remove_file(&temp);
    written.map_err(|error| SandboxError::new(format!("write {}: {error}", path.display())))
}

fn lift(error: anyhow::Error) -> SandboxError {
    SandboxError::new(format!("{error:#}"))
}

#[cfg(test)]
#[path = "runtime/tests.rs"]
mod tests;
