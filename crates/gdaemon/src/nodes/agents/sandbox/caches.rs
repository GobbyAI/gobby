//! Spawn-time grants and the run layout. Ports the spawn-time steps of
//! `spawn_cache_policy.py` (`sandbox_config_for_spawn`, the cache and PATH fill),
//! `sandbox_run_environment.py`, `sandbox_policy.prepare_sandbox_run_paths`, and
//! `cargo_target.py`, plus the nextest and project write grants.

use super::{
    PolicyInputs,
    paths::{dedupe, expand_user, resolve_nonstrict, text},
};
use crate::node_commands::Sandbox;
use anyhow::{Result, anyhow, bail};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};

const UV_CACHE_DIR: &str = "UV_CACHE_DIR";
const CARGO_HOME: &str = "CARGO_HOME";
const CARGO_TARGET_DIR: &str = "CARGO_TARGET_DIR";
const PATH: &str = "PATH";
const SESSION_ENV: &str = "GOBBY_SESSION_ID";

/// `SANDBOX_CACHE_POLICY`: each toolchain cache variable and its `<tmp>/gobby/` directory.
const SANDBOX_CACHE_POLICY: &[(&str, &str)] = &[
    ("GOCACHE", "go-cache"),
    ("GOMODCACHE", "go-mod-cache"),
    ("npm_config_cache", "npm-cache"),
    ("YARN_CACHE_FOLDER", "yarn-cache"),
    ("PNPM_HOME", "pnpm-home"),
    ("PIP_CACHE_DIR", "pip-cache"),
    ("GRADLE_USER_HOME", "gradle-home"),
    ("COURSIER_CACHE", "coursier-cache"),
    ("NUGET_PACKAGES", "nuget-packages"),
    ("COMPOSER_CACHE_DIR", "composer-cache"),
    ("PUB_CACHE", "pub-cache"),
    ("GEM_HOME", "gem-home"),
    ("BUNDLE_PATH", "bundle-path"),
    ("HEX_HOME", "hex-home"),
    ("MIX_HOME", "mix-home"),
];

/// `RUN_CACHE_ENV_VARS`: each points into the run's cache directory.
const RUN_CACHE_ENV_VARS: &[&str] = &[
    "UV_CACHE_DIR",
    "GOCACHE",
    "GOMODCACHE",
    "npm_config_cache",
    "YARN_CACHE_FOLDER",
    "PNPM_HOME",
    "PIP_CACHE_DIR",
    "GRADLE_USER_HOME",
    "COURSIER_CACHE",
    "NUGET_PACKAGES",
    "COMPOSER_CACHE_DIR",
    "PUB_CACHE",
    "GEM_HOME",
    "BUNDLE_PATH",
    "HEX_HOME",
    "MIX_HOME",
    "XDG_CACHE_HOME",
    "ZIG_GLOBAL_CACHE_DIR",
];

fn non_empty<'a>(env: &'a BTreeMap<String, String>, key: &str) -> Option<&'a str> {
    env.get(key)
        .map(String::as_str)
        .filter(|value| !value.is_empty())
}

/// The temp root Python reads from `TMPDIR`, falling back to `/tmp`.
fn temp_root(env: &BTreeMap<String, String>) -> PathBuf {
    PathBuf::from(non_empty(env, "TMPDIR").unwrap_or("/tmp"))
}

fn session_of(env: &BTreeMap<String, String>) -> String {
    non_empty(env, SESSION_ENV)
        .unwrap_or("unknown-session")
        .to_string()
}

/// `_safe_component`: replace unsafe characters, trim dashes, and cap the length.
pub(super) fn safe_component(value: &str, limit: usize, fallback: &str) -> String {
    let safe: String = value
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '_' || c == '-' {
                c
            } else {
                '-'
            }
        })
        .collect();
    let trimmed: String = safe.trim_matches('-').chars().take(limit).collect();
    if trimmed.is_empty() {
        fallback.to_string()
    } else {
        trimmed
    }
}

/// `get_agent_session_cache_dir`: `<tmp>/gobby/<dir>/<safe session>-<digest>`.
fn session_cache_dir(tmp: &Path, dir: &str, session: &str) -> PathBuf {
    let prefix = safe_component(session, 80, "unknown-session");
    tmp.join("gobby")
        .join(dir)
        .join(format!("{prefix}-{}", super::hash16(session.as_bytes())))
}

fn shared_cargo_home(gobby: &Path) -> PathBuf {
    gobby.join("cache").join("cargo-home")
}

/// `native_bin_dir`: the managed native-tool directory, from the daemon's own environment.
fn managed_tool_bin(inputs: &PolicyInputs) -> String {
    match std::env::var_os("GOBBY_NATIVE_BIN_DIR").filter(|dir| !dir.is_empty()) {
        Some(dir) => dir.to_string_lossy().into_owned(),
        None => text(&inputs.home.join(".gobby").join("bin")),
    }
}

/// `merge_spawn_path`: keep the preferred entries, drop duplicates, and put the managed
/// tool bin ahead of other isolated `.gobby/bin` entries. The payload env is the only
/// PATH source; the daemon's PATH is not merged.
fn merge_spawn_path(preferred: Option<&str>, inputs: &PolicyInputs) -> String {
    let managed = managed_tool_bin(inputs);
    let mut entries = dedupe(
        preferred
            .unwrap_or_default()
            .split(':')
            .filter(|entry| !entry.is_empty())
            .map(str::to_string),
    );
    entries.retain(|entry| *entry != managed);
    let isolated = |entry: &str| {
        let path = expand_user(entry, &inputs.home);
        path.file_name().is_some_and(|name| name == "bin")
            && path
                .parent()
                .and_then(Path::file_name)
                .is_some_and(|name| name == ".gobby")
    };
    let insert_at = entries.iter().take_while(|entry| isolated(entry)).count();
    entries.insert(insert_at, managed);
    entries.join(":")
}

/// `apply_spawn_cache_policy` and `_apply_sandbox_cache_policy`: fill each missing cache and PATH entry.
fn fill_spawn_env(env: &mut BTreeMap<String, String>, inputs: &PolicyInputs) {
    let session = session_of(env);
    let tmp = temp_root(env);
    let defaults = [
        (
            UV_CACHE_DIR,
            text(&session_cache_dir(&tmp, "uv-cache", &session)),
        ),
        (CARGO_HOME, text(&shared_cargo_home(&inputs.gobby_home))),
    ];
    for (name, value) in defaults {
        if env.get(name).is_none_or(String::is_empty) {
            env.insert(name.to_string(), value);
        }
    }
    let merged = merge_spawn_path(env.get(PATH).map(String::as_str), inputs);
    env.insert(PATH.to_string(), merged);
    for (name, dir) in SANDBOX_CACHE_POLICY {
        if env.get(*name).is_none_or(String::is_empty) {
            env.insert(
                name.to_string(),
                text(&session_cache_dir(&tmp, dir, &session)),
            );
        }
    }
}

/// `sandbox_write_paths`: the cache, Cargo target, and hook inbox paths a sandboxed run writes.
fn sandbox_write_paths(env: &BTreeMap<String, String>, inputs: &PolicyInputs) -> Vec<String> {
    let mut paths: Vec<String> = [UV_CACHE_DIR, CARGO_HOME]
        .into_iter()
        .chain(SANDBOX_CACHE_POLICY.iter().map(|(name, _)| *name))
        .map(|name| env.get(name).cloned().unwrap_or_default())
        .collect();
    paths.push(env.get(CARGO_TARGET_DIR).cloned().unwrap_or_default());
    paths.push(text(
        &inputs.home.join(".gobby").join("hooks").join("inbox"),
    ));
    dedupe(paths)
}

/// `get_project_sandbox_config(root).extra_write_paths`: the checkout's declared write
/// roots, each resolved and required to be a strict descendant of the checkout.
pub(super) fn project_write_paths(root: &Path) -> Result<Vec<String>> {
    let config_path = root.join(".gobby").join("project.json");
    if !config_path.is_file() {
        return Ok(Vec::new());
    }
    let bytes = fs::read(&config_path)
        .map_err(|error| anyhow!("cannot read {}: {error}", config_path.display()))?;
    let value: serde_json::Value = serde_json::from_slice(&bytes)
        .map_err(|error| anyhow!("cannot parse {}: {error}", config_path.display()))?;
    let Some(section) = value.get("sandbox") else {
        return Ok(Vec::new());
    };
    let invalid = || {
        anyhow!(
            "Invalid project sandbox entry '<sandbox section>' for project root {:?}: \
             sandbox must be an object containing only extra_write_paths as a list of strings",
            text(root)
        )
    };
    let object = section.as_object().ok_or_else(invalid)?;
    if object.keys().any(|key| key != "extra_write_paths") {
        return Err(invalid());
    }
    let entries: Vec<String> = match object.get("extra_write_paths") {
        None => Vec::new(),
        Some(list) => list
            .as_array()
            .ok_or_else(invalid)?
            .iter()
            .map(|entry| entry.as_str().map(str::to_string).ok_or_else(invalid))
            .collect::<Result<_>>()?,
    };
    let project = resolve_nonstrict(root);
    let mut resolved = Vec::new();
    for entry in entries {
        let candidate = PathBuf::from(&entry);
        let candidate = if candidate.is_absolute() {
            candidate
        } else {
            project.join(candidate)
        };
        let path = resolve_nonstrict(&candidate);
        if path == project || !path.starts_with(&project) {
            bail!(
                "Invalid project sandbox extra_write_paths entry {entry:?} for project root {:?}: \
                 resolved path {:?}; entries must resolve to a strict descendant of the project root",
                text(&project),
                text(&path)
            );
        }
        resolved.push(text(&path));
    }
    Ok(dedupe(resolved))
}

/// The nextest store grant: added when the source checkout declares `<checkout>/.nextest`.
pub(super) fn grant_declared_nextest(config: &mut Sandbox, source_root: &Path, project: &[String]) {
    let store = text(&resolve_nonstrict(source_root).join(".nextest"));
    if project.contains(&store) && !config.extra_write_paths.contains(&store) {
        config.extra_write_paths.push(store);
    }
}

/// `ensure_agent_cargo_target_dir`: the unsandboxed agent target for this checkout.
pub(super) fn agent_cargo_target_dir(inputs: &PolicyInputs) -> String {
    let project_root = inputs
        .gobby_home
        .join("cache")
        .join("cargo-target-v2")
        .join(safe_component(&inputs.project_id, 80, "unknown-project"));
    let checkout = super::checkout_component(&inputs.workspace);
    text(&project_root.join(format!("{checkout}-agent")))
}

/// `sandbox_config_for_spawn`: fill the spawn caches and PATH, then grant the project
/// roots, the cache paths, the Cargo target, and the hook inbox, and force registries on.
pub(super) fn spawn_grants(
    mut config: Sandbox,
    project: &[String],
    env: &mut BTreeMap<String, String>,
    inputs: &PolicyInputs,
) -> Sandbox {
    fill_spawn_env(env, inputs);
    let mut extra = config.extra_write_paths.clone();
    for path in project
        .iter()
        .cloned()
        .chain(sandbox_write_paths(env, inputs))
    {
        if !path.is_empty() && !extra.contains(&path) {
            extra.push(path);
        }
    }
    config.extra_write_paths = extra;
    config.allow_package_registries = true;
    config
}

/// `sandbox_policy.py::secure_policy_directory`: the per-run policy directory.
fn secure_policy_directory(gobby: &Path, run_id: &str) -> Result<PathBuf> {
    let safe: String = run_id
        .chars()
        .filter(|c| c.is_alphanumeric() || *c == '-' || *c == '_')
        .collect();
    if safe.is_empty() {
        bail!("agent run ID has no safe policy-directory characters");
    }
    Ok(gobby.join("run").join("sandbox").join(safe))
}

/// `sandbox_cache_path`: refuse a cache path reached through a link, because a run
/// could plant one for the next run's grant to follow.
fn check_cache_path(shared: &Path, path: &Path) -> Result<()> {
    let cache_root = shared.parent().unwrap_or(shared);
    let relative = path.strip_prefix(cache_root).unwrap_or(path);
    let expected = resolve_nonstrict(cache_root).join(relative);
    if resolve_nonstrict(path) != expected {
        bail!(
            "Sandbox cache path {} resolves outside the sandbox cache {}; remove it",
            path.display(),
            shared.display()
        );
    }
    Ok(())
}

/// `SandboxRunPaths`: one run's root, its writable siblings, and its cache layout.
#[derive(Debug, Clone)]
pub(super) struct RunLayout {
    pub tmp: PathBuf,
    pub hooks: PathBuf,
    pub logs: PathBuf,
    pub cache: PathBuf,
    pub shared_cache: PathBuf,
    pub cargo_target: PathBuf,
}

impl RunLayout {
    /// The four writable run directories, in the oracle's order.
    pub(super) fn writable(&self) -> [PathBuf; 4] {
        [
            self.tmp.clone(),
            self.hooks.clone(),
            self.logs.clone(),
            self.cache.clone(),
        ]
    }

    pub(super) fn cargo_home(&self) -> PathBuf {
        self.shared_cache.join("cargo-home")
    }

    /// `SandboxRunPaths.environment`. Without a sccache socket, the sccache keys are blank.
    pub(super) fn environment(&self, provider: &str) -> BTreeMap<String, String> {
        let mut values: BTreeMap<String, String> = RUN_CACHE_ENV_VARS
            .iter()
            .map(|name| {
                let dir = name.replace('_', "-").to_lowercase();
                (name.to_string(), text(&self.cache.join(dir)))
            })
            .collect();
        values.insert(CARGO_HOME.to_string(), text(&self.cargo_home()));
        values.insert(CARGO_TARGET_DIR.to_string(), text(&self.cargo_target));
        values.insert("TMPDIR".to_string(), text(&self.tmp));
        if provider == "claude" {
            values.insert("CLAUDE_CODE_TMPDIR".to_string(), text(&self.tmp));
        }
        values.insert("TMPPREFIX".to_string(), text(&self.tmp.join("zsh")));
        values.insert("GOBBY_LOG_DIR".to_string(), text(&self.logs));
        values.insert("OCR_NO_UPDATE".to_string(), "1".to_string());
        for (name, value) in [
            ("CARGO_BUILD_JOBS", "4"),
            ("RUSTC_WRAPPER", ""),
            ("SCCACHE_DIR", ""),
            ("SCCACHE_SERVER_UDS", ""),
        ] {
            values.insert(name.to_string(), value.to_string());
        }
        values
    }
}

/// `prepare_sandbox_run_paths` without filesystem writes: the run root, its
/// subdirectories, and the shared and per-checkout Cargo targets. The caller creates
/// the directories, and the pre-commit prewarm is the only side effect.
pub(super) fn run_layout(
    inputs: &PolicyInputs,
    env: &BTreeMap<String, String>,
) -> Result<RunLayout> {
    let gobby = inputs.gobby_home.as_path();
    let managed = resolve_nonstrict(&gobby.join("runtime").join("managed-executions"));
    let bootstrap_root = non_empty(env, "GOBBY_MANAGED_EXECUTION_BOOTSTRAP")
        .map(|raw| resolve_nonstrict(Path::new(raw)))
        .and_then(|bootstrap| {
            let parent = bootstrap.parent()?.to_path_buf();
            parent.starts_with(&managed).then_some(parent)
        });
    let root = match bootstrap_root {
        Some(root) => root,
        None => secure_policy_directory(gobby, &inputs.run_id)?,
    };
    let shared_cache = gobby.join("cache").join("sandbox");
    let cargo_target = shared_cache
        .join("cargo-target")
        .join(super::checkout_component(&inputs.workspace));
    check_cache_path(&shared_cache, &shared_cache.join("cargo-home"))?;
    check_cache_path(&shared_cache, &cargo_target)?;
    Ok(RunLayout {
        tmp: root.join("tmp"),
        hooks: root.join("hooks"),
        logs: root.join("logs"),
        cache: root.join("cache"),
        shared_cache,
        cargo_target,
    })
}
