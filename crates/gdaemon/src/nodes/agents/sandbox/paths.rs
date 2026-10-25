//! Path resolution and the grants `compute_sandbox_paths` assembles. Ports
//! `sandbox_policy.py`, `sandbox.py::compute_sandbox_paths`,
//! `sandbox_control_paths.py`, `sandbox_read_roots.py`, `sandbox_credentials.py`,
//! `srt_socket_paths.py`, and `integrations/rtk.py::sandbox_paths`.

use super::{PolicyInputs, tables};
use crate::node_commands::Sandbox;
use anyhow::{Result, anyhow, bail};
use std::{
    collections::{BTreeMap, HashSet},
    fs,
    iter::once,
    os::unix::fs::PermissionsExt,
    path::{Component, Path, PathBuf},
    process::Command,
};

/// A credential env var the sandbox masks for one provider's API hosts.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct CredentialEnv {
    pub name: String,
    pub inject_hosts: Vec<String>,
}

/// The resolved grant lists that `settings::render` turns into SRT settings.
#[derive(Debug, Clone, Default)]
pub(super) struct ResolvedPaths {
    pub read_paths: Vec<String>,
    pub write_paths: Vec<String>,
    pub deny_read_paths: Vec<String>,
    pub deny_write_paths: Vec<String>,
    pub allowed_domains: Vec<String>,
    pub denied_domains: Vec<String>,
    pub allow_unix_sockets: Vec<String>,
    pub credential_env_vars: Vec<CredentialEnv>,
    pub provider: String,
}

/// RTK's narrow grants; both lists are empty when RTK is absent.
#[derive(Debug, Default)]
pub(super) struct RtkGrants {
    pub read: Vec<PathBuf>,
    pub write: Vec<PathBuf>,
}

pub(super) fn text(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

/// Keep the first occurrence of each string, in order.
pub(super) fn dedupe(items: impl IntoIterator<Item = String>) -> Vec<String> {
    let mut seen = HashSet::new();
    items
        .into_iter()
        .filter(|item| seen.insert(item.clone()))
        .collect()
}

fn dedupe_paths(items: impl IntoIterator<Item = PathBuf>) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = Vec::new();
    for item in items {
        if !out.contains(&item) {
            out.push(item);
        }
    }
    out
}

fn non_empty<'a>(env: &'a BTreeMap<String, String>, key: &str) -> Option<&'a str> {
    env.get(key)
        .map(String::as_str)
        .filter(|value| !value.is_empty())
}

/// `os.path.expanduser` for the `~` and `~/...` forms the policy uses.
pub(super) fn expand_user(raw: &str, home: &Path) -> PathBuf {
    if raw == "~" {
        return home.to_path_buf();
    }
    match raw.strip_prefix("~/") {
        Some(rest) => home.join(rest),
        None => PathBuf::from(raw),
    }
}

/// `os.path.abspath`: lexical normalization with no symlink resolution.
pub(super) fn lexical_normal(path: &Path) -> PathBuf {
    let mut parts: Vec<&std::ffi::OsStr> = Vec::new();
    for component in path.components() {
        match component {
            Component::Normal(name) => parts.push(name),
            Component::ParentDir => {
                parts.pop();
            }
            Component::CurDir | Component::RootDir | Component::Prefix(_) => {}
        }
    }
    let mut out = if path.is_absolute() {
        PathBuf::from("/")
    } else {
        PathBuf::new()
    };
    for part in parts {
        out.push(part);
    }
    out
}

/// `Path.resolve(strict=False)`: canonicalize the longest existing prefix and
/// re-append the rest, so a path that does not exist yet still resolves.
pub(super) fn resolve_nonstrict(path: &Path) -> PathBuf {
    let normal = lexical_normal(path);
    let mut existing = normal.clone();
    let mut rest: Vec<std::ffi::OsString> = Vec::new();
    loop {
        if let Ok(canonical) = existing.canonicalize() {
            let mut out = canonical;
            for part in rest.iter().rev() {
                out.push(part);
            }
            return out;
        }
        match (existing.file_name(), existing.parent()) {
            (Some(name), Some(parent)) => {
                rest.push(name.to_os_string());
                existing = parent.to_path_buf();
            }
            _ => return normal,
        }
    }
}

/// `_absolute_path`: expand, anchor relative paths at `base`, and normalize.
fn absolute_path(raw: &str, base: Option<&Path>, home: &Path) -> PathBuf {
    let mut path = expand_user(raw, home);
    if !path.is_absolute()
        && let Some(base) = base
    {
        path = base.join(path);
    }
    lexical_normal(&path)
}

/// `canonical_path`: an absolute, symlink-resolved policy path.
fn canonical_pathbuf(raw: &str, base: Option<&Path>, home: &Path) -> PathBuf {
    resolve_nonstrict(&absolute_path(raw, base, home))
}

/// `canonical_paths`: canonicalize and de-duplicate, preserving order.
pub(super) fn canonical_paths<S: AsRef<str>>(
    raws: impl IntoIterator<Item = S>,
    base: Option<&Path>,
    home: &Path,
) -> Vec<String> {
    dedupe(
        raws.into_iter()
            .map(|raw| text(&canonical_pathbuf(raw.as_ref(), base, home))),
    )
}

/// `deny_paths`: each entry's literal and symlink-resolved forms, in order.
pub(super) fn deny_paths<S: AsRef<str>>(
    raws: impl IntoIterator<Item = S>,
    base: Option<&Path>,
    home: &Path,
) -> Vec<String> {
    dedupe(raws.into_iter().flat_map(|raw| {
        let raw = raw.as_ref();
        [
            text(&absolute_path(raw, base, home)),
            text(&canonical_pathbuf(raw, base, home)),
        ]
    }))
}

/// `policy_subtree_prefix`: the slash-terminated directory a policy path or glob matches within.
pub(super) fn subtree_prefix(entry: &str) -> String {
    let mut head = entry;
    if let Some(index) = entry.find(['*', '?', '[', '{']) {
        head = match entry[..index].rfind('/') {
            Some(slash) => &entry[..slash],
            None => "",
        };
    }
    format!("{}/", head.trim_end_matches('/'))
}

/// `_existing_home_roots`-style filter applied to home-relative roots.
fn existing_home_roots(roots: &[&str], home: &Path) -> Vec<String> {
    canonical_paths(
        roots
            .iter()
            .map(|root| home.join(root))
            .filter(|path| path.exists())
            .map(|path| text(&path)),
        None,
        home,
    )
}

/// `credential_roots`: install credentials and the daemon's startup files. The oracle
/// denies the bootstrap the daemon bound at startup. `PolicyInputs` carries no bound
/// path, so the default `gobby_home/bootstrap.yaml` stands in for it.
fn credential_roots(gobby: &Path) -> Vec<PathBuf> {
    vec![
        gobby.join("bootstrap.yaml"),
        gobby.join(".secret_kek"),
        gobby.join("grants"),
        gobby.join("tools").join("srt"),
        gobby.join("break_glass"),
        gobby.join(".break_glass-staging"),
    ]
}

/// `private_srt_socket_roots`: the private SRT socket roots, including this UID's `/tmp` root.
fn private_srt_socket_roots(gobby: &Path, env: &BTreeMap<String, String>) -> Vec<PathBuf> {
    // SAFETY: getuid cannot fail and takes no pointers.
    let uid = unsafe { libc::getuid() };
    let mut roots = vec![
        gobby.join("runtime").join("srt-sock"),
        PathBuf::from(format!("/tmp/gobby-srt-{uid}")),
    ];
    if let Some(dir) = non_empty(env, "GOBBY_SRT_SOCKET_DIR") {
        roots.push(PathBuf::from(dir));
    }
    roots
}

/// `managed_execution_root`: the daemon-owned root for credential-scoped executions.
pub(super) fn managed_execution_root(gobby: &Path) -> PathBuf {
    gobby.join("runtime").join("managed-executions")
}

/// `sensitive_roots`: Gobby roots excluded from every managed allow surface.
fn sensitive_roots(gobby: &Path, home: &Path, env: &BTreeMap<String, String>) -> Vec<String> {
    let mut roots = credential_roots(gobby);
    roots.push(gobby.join("gcode-runtime"));
    roots.push(managed_execution_root(gobby));
    roots.extend(private_srt_socket_roots(gobby, env));
    deny_paths(roots.iter().map(|root| text(root)), None, home)
}

/// `sensitive_write_roots`: credential and daemon-state roots no write grant may override.
fn sensitive_write_roots(gobby: &Path, home: &Path, env: &BTreeMap<String, String>) -> Vec<String> {
    let mut roots = vec![
        home.join(".ssh"),
        home.join(".aws"),
        home.join(".gnupg"),
        home.join(".kube"),
        home.join(".config").join("gcloud"),
    ];
    roots.extend(credential_roots(gobby));
    roots.extend(private_srt_socket_roots(gobby, env));
    deny_paths(roots.iter().map(|root| text(root)), None, home)
}

/// `credential_read_roots`: user and every provider's credentials, denied for all providers.
fn credential_read_roots(home: &Path) -> Vec<String> {
    let paths = tables::USER_CREDENTIAL_READ_PATHS
        .iter()
        .copied()
        .chain(
            tables::AUTH_PATHS
                .iter()
                .flat_map(|(_, paths)| paths.iter().copied()),
        )
        .chain(
            tables::AUTH_READ_ONLY_PATHS
                .iter()
                .flat_map(|(_, paths)| paths.iter().copied()),
        );
    deny_paths(paths, None, home)
}

/// `provider_credential_read_exceptions`: the active provider's declared necessities.
fn provider_credential_read_exceptions(provider: &str, home: &Path) -> Vec<String> {
    canonical_paths(
        tables::lookup(tables::AUTH_PATHS, provider)
            .iter()
            .chain(tables::lookup(tables::AUTH_READ_ONLY_PATHS, provider))
            .copied(),
        None,
        home,
    )
}

/// `gcode_runtime_write_exceptions`: renewal writes inside this workspace's gcode home.
fn gcode_runtime_write_exceptions(workspace: &Path, gobby: &Path, home: &Path) -> Vec<String> {
    let digest = super::gcode_runtime_digest(workspace);
    canonical_paths(
        [text(&gobby.join("gcode-runtime").join(digest))],
        None,
        home,
    )
}

/// `provider_write_exceptions`: provider-owned state roots the CLI writes at runtime.
fn provider_write_exceptions(provider: &str, home: &Path) -> Vec<String> {
    if provider == "grok" {
        return canonical_paths(
            tables::GROK_RUNTIME_DIRECTORIES
                .iter()
                .map(|name| format!("~/.grok/{name}")),
            None,
            home,
        );
    }
    canonical_paths(tables::lookup(tables::AUTH_PATHS, provider), None, home)
}

/// `_nearest_package_root`: the first package.json among the executable's two nearest parents.
fn nearest_package_root(target: &Path, home: &Path) -> Option<PathBuf> {
    let home = resolve_nonstrict(home);
    for parent in target.ancestors().skip(1).take(2) {
        if parent == home {
            break;
        }
        if parent.join("package.json").is_file() {
            return Some(parent.to_path_buf());
        }
    }
    None
}

/// `shutil.which` over an explicit search path: the first executable regular file.
pub(super) fn which(name: &str, search: Option<&str>) -> Option<PathBuf> {
    search?
        .split(':')
        .filter(|dir| !dir.is_empty())
        .map(|dir| Path::new(dir).join(name))
        .find(|candidate| {
            candidate
                .metadata()
                .is_ok_and(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
        })
}

/// `provider_read_exceptions`: auth and config roots plus the provider and node executables.
fn provider_read_exceptions(
    provider: &str,
    env: &BTreeMap<String, String>,
    provider_executable: Option<&Path>,
    home: &Path,
) -> Vec<String> {
    let mut paths: Vec<String> = Vec::new();
    paths.extend(
        tables::lookup(tables::AUTH_PATHS, provider)
            .iter()
            .map(|path| path.to_string()),
    );
    paths.extend(
        tables::lookup(tables::AUTH_READ_ONLY_PATHS, provider)
            .iter()
            .map(|path| path.to_string()),
    );
    paths.push("~/.gitconfig".to_string());
    paths.push("~/.config/git".to_string());

    let search = env.get("PATH").map(String::as_str);
    let provider_exe = provider_executable
        .filter(|path| !path.as_os_str().is_empty())
        .map(Path::to_path_buf)
        .or_else(|| which(provider, search));
    for executable in [provider_exe, which("node", search)].into_iter().flatten() {
        let absolute = std::path::absolute(expand_user(&text(&executable), home))
            .unwrap_or_else(|_| executable.clone());
        paths.push(text(&absolute));
        let target = resolve_nonstrict(&absolute);
        paths.push(text(&target));
        if let Some(root) = nearest_package_root(&target, home) {
            paths.push(text(&root));
        }
    }
    canonical_paths(paths, None, home)
}

/// `gobby_read_exceptions`: Gobby state, runtime, prompt, and uv resources agents need.
fn gobby_read_exceptions(env: &BTreeMap<String, String>, home: &Path) -> Vec<String> {
    let mut paths = vec![
        home.join(".cache").join("uv"),
        home.join("Library").join("Caches").join("uv"),
    ];
    for key in [
        "GOBBY_CODE_INDEX_RUNTIME_HOME",
        "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
        "GOBBY_PROMPT_FILE",
    ] {
        if let Some(value) = non_empty(env, key) {
            paths.push(PathBuf::from(value));
        }
    }
    if let Some(uv) = which("uv", env.get("PATH").map(String::as_str)) {
        let absolute = std::path::absolute(&uv).unwrap_or_else(|_| uv.clone());
        paths.push(resolve_nonstrict(&absolute));
        paths.push(absolute);
    }
    paths.push(home.join(".local").join("share").join("uv"));
    canonical_paths(paths.iter().map(|path| text(path)), None, home)
}

/// `toolchain_read_roots`: existing compiler, SDK, package, and shared-cache roots.
fn toolchain_read_roots(home: &Path) -> Vec<String> {
    existing_home_roots(
        &[
            tables::TOOLCHAIN_READ_ROOTS,
            tables::TOOLCHAIN_SHARED_READ_ROOTS,
        ]
        .concat(),
        home,
    )
}

/// `toolchain_credential_paths`: registry credentials nested inside granted toolchain roots.
fn toolchain_credential_paths(home: &Path) -> Vec<String> {
    canonical_paths(
        tables::TOOLCHAIN_CREDENTIAL_PATHS
            .iter()
            .map(|path| text(&home.join(path))),
        None,
        home,
    )
}

/// `provider_control_write_paths`: host and inherited control files, including alternate homes.
fn provider_control_write_paths(
    workspace: &Path,
    env: &BTreeMap<String, String>,
    extra_roots: &[PathBuf],
    home: &Path,
) -> Vec<String> {
    let mut paths = vec![text(&home.join(".claude.json"))];
    let mut auth_files: Vec<PathBuf> = Vec::new();
    if let Some(configured) = non_empty(env, "GROK_AUTH_PATH") {
        let auth = resolve_nonstrict(&expand_user(configured, home));
        auth_files.push(auth.with_file_name("auth.json.lock"));
        auth_files.push(auth);
    }

    let mut control_roots: Vec<PathBuf> = Vec::new();
    for extra in extra_roots {
        // File and glob grants are not configuration directories: stop at the first
        // magic component and, for a file, climb to its directory.
        let components: Vec<Component<'_>> = extra.components().collect();
        let mut extra = match components.iter().position(|component| {
            component
                .as_os_str()
                .to_string_lossy()
                .contains(['*', '?', '['])
        }) {
            Some(index) => components[..index].iter().collect::<PathBuf>(),
            None => extra.clone(),
        };
        if extra.is_file() || auth_files.contains(&resolve_nonstrict(&extra)) {
            extra = extra.parent().map_or(extra.clone(), Path::to_path_buf);
        }
        control_roots.push(extra.clone());
        control_roots.extend(extra.ancestors().skip(1).map(Path::to_path_buf));
    }

    let roots = dedupe_paths(
        [home.to_path_buf(), workspace.to_path_buf()]
            .into_iter()
            .chain(workspace.ancestors().skip(1).map(Path::to_path_buf))
            .chain(control_roots),
    );
    for root in &roots {
        paths.push(text(&root.join(".mcp.json")));
        for (directory, controls) in tables::PROVIDER_CONTROLS {
            paths.extend(
                controls
                    .iter()
                    .map(|name| text(&root.join(directory).join(name))),
            );
        }
    }

    for (variable, directory) in tables::CONFIG_HOME_ENV {
        if let Some(configured) = non_empty(env, variable) {
            let root = expand_user(configured, home);
            for name in tables::lookup(tables::PROVIDER_CONTROLS, directory) {
                paths.push(text(&root.join(name)));
            }
            if *variable == "CLAUDE_CONFIG_DIR" {
                paths.push(text(&root.join(".claude.json")));
            }
        }
    }
    if let Some(configured) = non_empty(env, "GOBBY_DROID_HOOKS_FILE") {
        paths.push(configured.to_string());
    }
    if let Some(configured) = non_empty(env, "GOBBY_HOOKS_DIR") {
        paths.push(text(&expand_user(configured, home).join("hooks.json")));
    }
    dedupe(paths)
}

/// `allowed_domains`: provider, operator, Git, registry, and loopback domains.
fn allowed_domains(config: &Sandbox, provider: &str) -> Vec<String> {
    let mut domains: Vec<String> = tables::provider_domains(provider)
        .iter()
        .chain(tables::provider_auth_domains(provider))
        .map(|domain| domain.to_string())
        .collect();
    domains.extend(config.allowed_domains.iter().cloned());
    if config.allow_git_network {
        domains.extend(tables::GIT_DOMAINS.iter().map(|domain| domain.to_string()));
    }
    if config.allow_package_registries {
        domains.extend(
            tables::PACKAGE_REGISTRY_DOMAINS
                .iter()
                .map(|domain| domain.to_string()),
        );
    }
    domains.extend(["localhost", "127.0.0.1"].map(String::from));
    dedupe(
        domains
            .into_iter()
            .filter(|domain| !domain.is_empty())
            .map(|domain| domain.to_lowercase()),
    )
}

/// `credential_env_vars` filtered to the names the run environment actually carries.
fn credential_env_vars(provider: &str, env: &BTreeMap<String, String>) -> Vec<CredentialEnv> {
    let inject_hosts = dedupe(
        tables::provider_domains(provider)
            .iter()
            .map(|host| host.to_string()),
    );
    tables::credential_env(provider)
        .iter()
        .filter(|name| env.contains_key(**name))
        .map(|name| CredentialEnv {
            name: name.to_string(),
            inject_hosts: inject_hosts.clone(),
        })
        .collect()
}

/// `gobby_home/run`'s counterpart for RTK: resolve `rtk` binaries and state directories.
fn rtk_platform_paths(
    env: &BTreeMap<String, String>,
    home: &Path,
) -> (PathBuf, PathBuf, PathBuf, PathBuf) {
    let (config_root, data_root) = if cfg!(target_os = "macos") {
        let root = home.join("Library").join("Application Support");
        (root.clone(), root)
    } else {
        (
            env.get("XDG_CONFIG_HOME")
                .map_or_else(|| home.join(".config"), PathBuf::from),
            env.get("XDG_DATA_HOME")
                .map_or_else(|| home.join(".local").join("share"), PathBuf::from),
        )
    };
    let config_dir = config_root.join("rtk");
    let data_dir = data_root.join("rtk");
    let mut database = env
        .get("RTK_DB_PATH")
        .map_or_else(|| data_dir.join("history.db"), PathBuf::from);
    let mut tee = env
        .get("RTK_TEE_DIR")
        .map_or_else(|| data_dir.join("tee"), PathBuf::from);

    let config_path = config_dir.join("config.toml");
    if config_path.is_file() {
        let config = fs::read_to_string(&config_path)
            .ok()
            .and_then(|raw| toml::from_str::<toml::Table>(&raw).ok())
            .unwrap_or_default();
        let section = |name: &str| {
            config
                .get(name)
                .and_then(toml::Value::as_table)
                .cloned()
                .unwrap_or_default()
        };
        if !env.contains_key("RTK_DB_PATH")
            && let Some(value) = section("tracking")
                .get("database_path")
                .and_then(toml::Value::as_str)
                .filter(|value| !value.is_empty())
        {
            database = expand_user(value, home);
        }
        if !env.contains_key("RTK_TEE_DIR")
            && let Some(value) = section("tee")
                .get("directory")
                .and_then(toml::Value::as_str)
                .filter(|value| !value.is_empty())
        {
            tee = expand_user(value, home);
        }
    }
    (config_dir, data_dir, database, tee)
}

/// `rtk_candidates`: the explicit override, Gobby's managed binary, and `PATH`'s `rtk`.
fn rtk_candidates(env: &BTreeMap<String, String>, home: &Path) -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    if let Some(explicit) = non_empty(env, "GOBBY_RTK_BIN") {
        candidates.push(PathBuf::from(explicit));
    }
    candidates.push(home.join(".gobby").join("bin").join("rtk"));
    if let Some(found) = which("rtk", env.get("PATH").map(String::as_str)) {
        candidates.push(found);
    }
    let mut seen = HashSet::new();
    candidates
        .into_iter()
        .filter(|candidate| seen.insert(text(&expand_user(&text(candidate), home))))
        .collect()
}

/// `integrations/rtk.py::sandbox_paths`: grants only when RTK's state or binary exists.
fn rtk_sandbox_paths(env: &BTreeMap<String, String>, home: &Path) -> RtkGrants {
    let (config_dir, data_dir, database, tee) = rtk_platform_paths(env, home);
    let binaries: Vec<PathBuf> = rtk_candidates(env, home)
        .iter()
        .map(|candidate| expand_user(&text(candidate), home))
        .filter(|candidate| candidate.is_file())
        .map(|candidate| resolve_nonstrict(&candidate))
        .collect();
    let has_state = config_dir.exists() || data_dir.exists();
    if !has_state && binaries.is_empty() {
        return RtkGrants::default();
    }
    let database_dir = database
        .parent()
        .map_or_else(PathBuf::new, Path::to_path_buf);
    RtkGrants {
        read: dedupe_paths(binaries.into_iter().chain([config_dir, data_dir.clone()])),
        write: dedupe_paths([data_dir, database_dir, tee]),
    }
}

/// `_git_metadata_write_paths`: the Git metadata directories a worktree commit writes.
fn git_metadata_write_paths(workspace: &Path, home: &Path) -> Result<Vec<String>> {
    let output = Command::new("git")
        .args(["rev-parse", "--git-dir", "--git-common-dir"])
        .current_dir(workspace)
        .output()
        .map_err(|error| anyhow!("Failed to resolve Git metadata for sandbox: {error}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        if stderr.to_lowercase().contains("not a git repository") {
            return Ok(Vec::new());
        }
        let detail = if stderr.is_empty() {
            output.status.to_string()
        } else {
            stderr
        };
        bail!("Failed to resolve Git metadata for sandbox: {detail}");
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    let mut paths = Vec::new();
    for raw in stdout
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
    {
        let mut path = expand_user(raw, home);
        if !path.is_absolute() {
            path = workspace.join(path);
        }
        paths.push(text(&resolve_nonstrict(&path)));
    }
    Ok(dedupe(paths))
}

/// `sandbox_policy.py::default_write_paths`: the workspace plus explicit write roots.
fn default_write_paths(config: &Sandbox, workspace: &Path, home: &Path) -> Vec<String> {
    canonical_paths(
        once(text(workspace)).chain(config.extra_write_paths.iter().cloned()),
        Some(workspace),
        home,
    )
}

/// `assert_sensitive_path_contract`: reject a grant that exposes a sensitive root.
fn assert_sensitive_path_contract(
    allow_lists: &[&[String]],
    gobby: &Path,
    home: &Path,
    env: &BTreeMap<String, String>,
) -> Result<()> {
    let protected: Vec<PathBuf> = sensitive_roots(gobby, home, env)
        .into_iter()
        .map(PathBuf::from)
        .collect();
    let grant_cache: Vec<PathBuf> = deny_paths([text(&gobby.join("grants"))], None, home)
        .into_iter()
        .map(PathBuf::from)
        .collect();
    let private_sockets: Vec<PathBuf> = deny_paths(
        private_srt_socket_roots(gobby, env)
            .iter()
            .map(|root| text(root)),
        None,
        home,
    )
    .into_iter()
    .map(PathBuf::from)
    .collect();
    for allowed_text in allow_lists.iter().flat_map(|list| list.iter()) {
        let allowed = resolve_nonstrict(Path::new(&subtree_prefix(allowed_text)));
        let is_glob = allowed_text.contains(['*', '?', '[', '{']);
        for sensitive in &protected {
            let exposes = sensitive.starts_with(&allowed)
                || ((is_glob
                    || grant_cache.contains(sensitive)
                    || private_sockets.contains(sensitive))
                    && allowed.starts_with(sensitive));
            if exposes {
                bail!(
                    "sandbox allow path contains sensitive root: {}",
                    allowed.display()
                );
            }
        }
    }
    Ok(())
}

/// `assert_credential_read_contract`: a read grant may not shadow a credential deny
/// unless it is a declared necessity.
fn assert_credential_read_contract(
    read_paths: &[String],
    credential_roots: &[String],
    required: &[String],
    home: &Path,
) -> Result<()> {
    let required: Vec<PathBuf> = required.iter().map(PathBuf::from).collect();
    let mut safe_user_reads = vec![resolve_nonstrict(&home.join(".ssh")).join("known_hosts")];
    let keychain =
        resolve_nonstrict(&home.join("Library").join("Keychains")).join("login.keychain-db");
    if required.contains(&keychain) {
        safe_user_reads.push(keychain);
    }
    let user_roots: Vec<PathBuf> = tables::USER_CREDENTIAL_READ_PATHS
        .iter()
        .map(|path| resolve_nonstrict(&expand_user(path, home)))
        .collect();
    for raw_allowed in read_paths {
        let allowed = PathBuf::from(raw_allowed);
        let safe = safe_user_reads.contains(&allowed);
        for root in &user_roots {
            if (allowed.starts_with(root) || root.starts_with(&allowed)) && !safe {
                bail!(
                    "sandbox read grant shadows credential deny: {}",
                    allowed.display()
                );
            }
        }
        for raw_root in credential_roots {
            let root = PathBuf::from(raw_root);
            let needed = required.iter().any(|needed| allowed.starts_with(needed));
            if (allowed.starts_with(&root) || root.starts_with(&allowed)) && !safe && !needed {
                bail!(
                    "sandbox read grant shadows credential deny: {}",
                    allowed.display()
                );
            }
        }
    }
    Ok(())
}

/// `compute_sandbox_paths`: the resolved grants for one `srt` run, from the
/// effective sandbox config and the run environment.
pub(super) fn compute(
    config: &Sandbox,
    inputs: &PolicyInputs,
    env: &BTreeMap<String, String>,
) -> Result<ResolvedPaths> {
    let home = inputs.home.as_path();
    let gobby = inputs.gobby_home.as_path();
    let provider = inputs.provider.as_str();
    let workspace = canonical_pathbuf(&text(&inputs.workspace), None, home);

    let provider_reads =
        provider_read_exceptions(provider, env, inputs.provider_executable.as_deref(), home);
    let credential_denies = credential_read_roots(home);
    let git_paths = git_metadata_write_paths(&workspace, home)?;
    let rtk = rtk_sandbox_paths(env, home);
    let write_paths = canonical_paths(
        default_write_paths(config, &workspace, home)
            .into_iter()
            .chain(git_paths)
            .chain(gcode_runtime_write_exceptions(&workspace, gobby, home))
            .chain(provider_write_exceptions(provider, home))
            .chain(rtk.write.iter().map(|path| text(path))),
        None,
        home,
    );
    let read_paths = canonical_paths(
        once(text(&workspace))
            .chain(write_paths.iter().cloned())
            .chain(gobby_read_exceptions(env, home))
            .chain(rtk.read.iter().map(|path| text(path)))
            .chain(toolchain_read_roots(home))
            .chain(provider_reads)
            .chain(once("~/.ssh/known_hosts".to_string()))
            .chain(config.extra_read_paths.iter().cloned()),
        Some(&workspace),
        home,
    );
    let deny_read_paths = deny_paths(
        sensitive_roots(gobby, home, env)
            .into_iter()
            .chain(credential_denies.iter().cloned())
            .chain(toolchain_credential_paths(home))
            .chain(config.extra_deny_read_paths.iter().cloned()),
        Some(&workspace),
        home,
    );
    let control_roots: Vec<PathBuf> = config
        .extra_write_paths
        .iter()
        .map(|path| canonical_pathbuf(path, Some(&workspace), home))
        .collect();
    let deny_write_paths = deny_paths(
        sensitive_write_roots(gobby, home, env)
            .into_iter()
            .chain(provider_control_write_paths(
                &workspace,
                env,
                &control_roots,
                home,
            ))
            .chain(toolchain_credential_paths(home))
            .chain(config.extra_deny_write_paths.iter().cloned()),
        Some(&workspace),
        home,
    );
    let allow_unix_sockets =
        canonical_paths(config.allow_unix_sockets.iter(), Some(&workspace), home);

    assert_sensitive_path_contract(
        &[
            read_paths.as_slice(),
            write_paths.as_slice(),
            allow_unix_sockets.as_slice(),
        ],
        gobby,
        home,
        env,
    )?;
    assert_credential_read_contract(
        &read_paths,
        &credential_denies,
        &provider_credential_read_exceptions(provider, home),
        home,
    )?;

    Ok(ResolvedPaths {
        read_paths,
        write_paths,
        deny_read_paths,
        deny_write_paths,
        allowed_domains: allowed_domains(config, provider),
        denied_domains: dedupe(
            config
                .denied_domains
                .iter()
                .filter(|domain| !domain.is_empty())
                .map(|domain| domain.to_lowercase()),
        ),
        allow_unix_sockets,
        credential_env_vars: credential_env_vars(provider, env),
        provider: provider.to_string(),
    })
}
