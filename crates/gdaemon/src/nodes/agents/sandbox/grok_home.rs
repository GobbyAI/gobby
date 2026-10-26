//! The private Grok home a Grok run gets (port of `grok_sandbox_home.py`). Control
//! files are forked, auth is linked rather than copied so refresh tokens stay in the
//! host store, and runtime state is shared with the host home.

use super::{PolicyInputs, paths, tables};
use anyhow::{Context, Result, bail};
use std::{
    fs::{self, DirBuilder, OpenOptions},
    os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt, symlink},
    path::{Path, PathBuf},
};

/// What a Grok run needs from its managed home.
pub(super) struct GrokSandboxHome {
    pub home: PathBuf,
    /// Control files the run may read but never write.
    pub protected_write_paths: Vec<String>,
    /// Runtime state the run writes, shared with the host home.
    pub runtime_write_paths: Vec<String>,
}

const SOURCE_RECEIPT: &str = "grok-home-source.json";
/// Server-refreshed policy caches stay writable inside the managed home.
const REFRESHABLE_CONTROL_FILES: [&str; 2] = ["managed_config.toml", "requirements.toml"];
const SIGNED_CACHE_FILES: [&str; 3] = [
    "managed_config.sig.json",
    "managed_identity.sig.json",
    "managed_config_cache.json",
];

/// Prepares the run's managed Grok home under `cache/grok-home`. `assets` holds the
/// completed-source receipt that makes a resumed home trustworthy.
pub(super) fn prepare(
    inputs: &PolicyInputs,
    cache: &Path,
    assets: &Path,
) -> Result<GrokSandboxHome> {
    let (gobby, user_home, env) = (
        inputs.gobby_home.as_path(),
        inputs.home.as_path(),
        &inputs.env,
    );
    let raw_source = env
        .get("GROK_HOME")
        .cloned()
        .unwrap_or_else(|| paths::text(&user_home.join(".grok")));
    let expanded = paths::expand_user(&raw_source, user_home);
    if is_link(&expanded) || is_link(cache) {
        bail!("Grok sandbox home must use real directories");
    }
    let mut source = paths::resolve_nonstrict(&expanded);
    let run_root = paths::resolve_nonstrict(&gobby.join("run").join("sandbox"));
    if source.file_name().is_some_and(|name| name == "grok-home") && source.starts_with(&run_root) {
        let old_receipt = source
            .parent()
            .and_then(Path::parent)
            .context("Grok source has no parent")?
            .join("assets")
            .join(SOURCE_RECEIPT);
        if is_link(&old_receipt) {
            bail!("Grok source receipt must not be a symlink");
        }
        let original = receipt_source(&old_receipt)?.context("invalid Grok source receipt")?;
        source = paths::resolve_nonstrict(Path::new(&original));
    }
    let source_text = paths::text(&source);
    let source_list = vec![source_text.clone()];
    let credential_roots = paths::credential_read_roots(user_home);
    let grok_required = paths::provider_credential_read_exceptions("grok", user_home);
    paths::assert_sensitive_path_contract(&[source_list.as_slice(), &[]], gobby, user_home, env)?;
    paths::assert_credential_read_contract(
        &source_list,
        &credential_roots,
        &grok_required,
        user_home,
    )?;

    let auth = match env.get("GROK_AUTH_PATH").filter(|raw| !raw.is_empty()) {
        Some(raw) => paths::expand_user(raw, user_home),
        None => source.join("auth.json"),
    };
    let auth = paths::resolve_nonstrict(&auth);
    let auth_lock = auth.with_file_name("auth.json.lock");
    let auth_dir = auth.parent().map(Path::to_path_buf).unwrap_or_default();
    let auth_writes = vec![
        paths::text(&auth),
        paths::text(&auth_lock),
        paths::text(&auth_dir.join(".*.*.tmp")),
    ];
    paths::assert_sensitive_path_contract(
        &[auth_writes.as_slice(), auth_writes.as_slice()],
        gobby,
        user_home,
        env,
    )?;
    paths::assert_credential_read_contract(
        &[
            paths::text(&auth),
            paths::text(&paths::resolve_nonstrict(&auth_lock)),
        ],
        &credential_roots,
        &grok_required,
        user_home,
    )?;
    if is_link(&auth_lock) {
        bail!("Grok auth lock must not be a symlink");
    }

    let mut protected: Vec<String> = tables::GROK_CONTROL_FILES
        .iter()
        .copied()
        .map(|name| paths::text(&source.join(name)))
        .collect();
    let registry = source.join("hooks-paths");
    if is_link(&registry) {
        bail!("Grok hook registry must not be a symlink");
    }
    if registry.is_file() {
        protected.extend(
            fs::read_to_string(&registry)?
                .lines()
                .map(str::trim)
                .filter(|line| !line.is_empty() && !line.starts_with('#'))
                .filter(|line| Path::new(line).is_absolute())
                .map(str::to_string),
        );
    }
    let mut runtime_write_paths = auth_writes.clone();
    runtime_write_paths.extend(
        tables::GROK_RUNTIME_DIRECTORIES
            .iter()
            .map(|name| paths::text(&source.join(name))),
    );
    let resolved_runtime: Vec<String> = runtime_write_paths
        .iter()
        .map(|path| paths::text(&paths::resolve_nonstrict(Path::new(path))))
        .collect();
    paths::assert_sensitive_path_contract(
        &[resolved_runtime.as_slice(), resolved_runtime.as_slice()],
        gobby,
        user_home,
        env,
    )?;
    paths::assert_credential_read_contract(
        &resolved_runtime,
        &credential_roots,
        &grok_required,
        user_home,
    )?;

    // Keep the installed hooks and local trust choices immutable inside the run.
    let managed = cache.join("grok-home");
    protected.extend(
        tables::GROK_CONTROL_FILES
            .iter()
            .copied()
            .filter(|name| !REFRESHABLE_CONTROL_FILES.contains(name))
            .map(|name| paths::text(&managed.join(name))),
    );
    let receipt_path = assets.join(SOURCE_RECEIPT);
    if is_link(&managed) {
        bail!("Grok managed home must not be a symlink");
    }
    if managed.exists() {
        if is_link(&receipt_path) || !receipt_path.is_file() {
            bail!("Grok managed home has no completed source receipt");
        }
        if receipt_source(&receipt_path)?.as_deref() != Some(source_text.as_str()) {
            bail!("Grok managed home source changed");
        }
        if paths::resolve_nonstrict(&managed.join("auth.json")) != auth {
            bail!("Grok managed home auth target changed");
        }
        return Ok(GrokSandboxHome {
            home: managed,
            protected_write_paths: protected,
            runtime_write_paths,
        });
    }

    private_dir(&managed)?;
    private_dir(assets)?;
    let seeded = tables::GROK_CONTROL_FILES
        .iter()
        .copied()
        .filter(|name| !matches!(*name, "hooks" | "bin"))
        .chain(SIGNED_CACHE_FILES);
    for name in seeded {
        let original = source.join(name);
        if is_link(&original) {
            bail!(
                "Grok control file must not be a symlink: {}",
                original.display()
            );
        }
        if original.is_file() {
            copy_private(&original, &managed.join(name))?;
        }
    }
    declare_gobby_mcp_server(gobby, &managed.join("config.toml"))?;

    let hooks = managed.join("hooks");
    private_dir(&hooks)?;
    let original_hooks = source.join("hooks");
    if is_link(&original_hooks) {
        bail!("Grok hook directory must not be a symlink");
    }
    if original_hooks.is_dir() {
        for entry in fs::read_dir(&original_hooks)? {
            let entry = entry?;
            let original = entry.path();
            let target = hooks.join(entry.file_name());
            if original.extension().is_some_and(|ext| ext == "json") {
                if is_link(&original) || !original.is_file() {
                    bail!(
                        "Grok hook JSON must be a regular file: {}",
                        original.display()
                    );
                }
                copy_private(&original, &target)?;
            } else {
                symlink(&original, &target)?;
            }
        }
    }
    // Grok follows auth.json links before atomic publish. Keep its lock shared too:
    // refresh tokens are single-use, so private locks would race the host.
    private_dir(&auth_dir)?;
    OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(false)
        .mode(0o600)
        .open(&auth_lock)?;
    symlink(&auth, managed.join("auth.json"))?;
    symlink(&auth_lock, managed.join("auth.json.lock"))?;
    let credentials = source.join("mcp_credentials.json");
    if credentials.exists() {
        symlink(&credentials, managed.join("mcp_credentials.json"))?;
    }
    for name in tables::GROK_RUNTIME_DIRECTORIES
        .iter()
        .copied()
        .chain(["skills", "personas"])
    {
        let original = source.join(name);
        if tables::GROK_RUNTIME_DIRECTORIES.contains(&name) {
            private_dir(&original)?;
        }
        if original.is_dir() {
            symlink(&original, managed.join(name))?;
        }
    }
    fs::write(
        &receipt_path,
        serde_json::json!({ "source": source_text }).to_string(),
    )?;
    fs::set_permissions(&receipt_path, fs::Permissions::from_mode(0o600))?;
    Ok(GrokSandboxHome {
        home: managed,
        protected_write_paths: protected,
        runtime_write_paths,
    })
}

/// Gives the run the installed `gmcp` as its `gobby` MCP server, replacing any operator
/// entry of that name, because Grok's config outranks every Claude-compat source.
fn declare_gobby_mcp_server(gobby: &Path, config_path: &Path) -> Result<()> {
    let text = if config_path.is_file() {
        fs::read_to_string(config_path)?
    } else {
        String::new()
    };
    let mut config: toml::Table = text
        .parse()
        .with_context(|| format!("Grok config is not valid TOML: {}", config_path.display()))?;
    let mut servers = match config.remove("mcp_servers") {
        None => toml::Table::new(),
        Some(toml::Value::Table(servers)) => servers,
        Some(_) => bail!(
            "Grok config mcp_servers is not a table: {}",
            config_path.display()
        ),
    };
    let mut server = toml::Table::new();
    server.insert(
        "command".to_string(),
        toml::Value::String(gobby.join("bin").join("gmcp").display().to_string()),
    );
    server.insert("args".to_string(), toml::Value::Array(Vec::new()));
    servers.insert("gobby".to_string(), toml::Value::Table(server));
    config.insert("mcp_servers".to_string(), toml::Value::Table(servers));
    fs::write(config_path, toml::to_string(&config)?)?;
    fs::set_permissions(config_path, fs::Permissions::from_mode(0o600))?;
    Ok(())
}

fn is_link(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok_and(|meta| meta.file_type().is_symlink())
}

fn private_dir(path: &Path) -> Result<()> {
    DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(path)
        .with_context(|| format!("create {}", path.display()))
}

fn copy_private(from: &Path, to: &Path) -> Result<()> {
    fs::copy(from, to)?;
    fs::set_permissions(to, fs::Permissions::from_mode(0o600))?;
    Ok(())
}

/// The `source` a receipt records, or `None` when the receipt does not carry one.
fn receipt_source(path: &Path) -> Result<Option<String>> {
    let receipt: serde_json::Value = serde_json::from_slice(&fs::read(path)?)?;
    Ok(receipt
        .get("source")
        .and_then(serde_json::Value::as_str)
        .map(str::to_string))
}
