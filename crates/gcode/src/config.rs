//! Configuration resolution for gcode.
//!
//! Reads bootstrap.yaml → PostgreSQL hub → config_store → service configs.
//! Resolves $secret:NAME and ${VAR} patterns.
//!
//! Source: src/gobby/config/bootstrap.py, src/gobby/config/persistence.py

use std::fmt;
use std::path::{Path, PathBuf};

use gobby_core::config::ConfigSource;
use gobby_core::project::{find_project_root, read_project_id};
use gobby_core::provisioning::{GCORE_CONFIG_FILENAME, StandaloneConfig};
use postgres::Client;

use crate::db;
use crate::git::{self, WorktreeKind};
use crate::secrets;
use crate::utils::short_id;

/// FalkorDB connection configuration.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FalkorConfig {
    pub host: String,
    pub port: u16,
    pub password: Option<String>,
    pub graph_name: String,
}

/// Qdrant connection configuration.
pub type QdrantConfig = gobby_core::config::QdrantConfig;

/// Embedding API configuration (OpenAI-compatible endpoint).
pub type EmbeddingConfig = gobby_core::config::EmbeddingConfig;

pub const FALKORDB_GRAPH_NAME: &str = "gobby_code";
pub const CODE_SYMBOL_COLLECTION_PREFIX: &str = "code_symbols_";
pub const GOBBY_EMBEDDING_VECTOR_DIM_ENV: &str = "GOBBY_EMBEDDING_VECTOR_DIM";
pub const EMBEDDING_VECTOR_DIM_CONFIG_KEY: &str = "embeddings.vector_dim";

pub const GOBBY_FALKORDB_HOST_ENV: &str = "GOBBY_FALKORDB_HOST";
pub const GOBBY_FALKORDB_PORT_ENV: &str = "GOBBY_FALKORDB_PORT";
pub const GOBBY_FALKORDB_PASSWORD_ENV: &str = "GOBBY_FALKORDB_PASSWORD";

pub const FALKORDB_HOST_CONFIG_KEY: &str = "databases.falkordb.host";
pub const FALKORDB_PORT_CONFIG_KEY: &str = "databases.falkordb.port";
pub const FALKORDB_PASSWORD_CONFIG_KEY: &str = "databases.falkordb.requirepass";

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct CodeVectorSettings {
    pub vector_dim: Option<usize>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CodeVectorConfigError {
    InvalidVectorDim { source: &'static str, value: String },
}

impl fmt::Display for CodeVectorConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidVectorDim { source, value } => write!(
                f,
                "invalid code vector dimension from {source}: `{value}` must be a positive integer"
            ),
        }
    }
}

impl std::error::Error for CodeVectorConfigError {}

impl FalkorConfig {
    pub fn connection_config(&self) -> gobby_core::config::FalkorConfig {
        gobby_core::config::FalkorConfig {
            host: self.host.clone(),
            port: self.port,
            password: self.password.clone(),
        }
    }
}

/// Resolved runtime context for gcode commands.
pub struct Context {
    /// PostgreSQL hub DSN
    pub database_url: String,
    /// Project root directory
    pub project_root: PathBuf,
    /// Project ID (from .gobby/project.json or DB lookup)
    pub project_id: String,
    /// Suppress warnings
    pub quiet: bool,
    /// FalkorDB config (None if unavailable)
    pub falkordb: Option<FalkorConfig>,
    /// Qdrant config (None if unavailable)
    pub qdrant: Option<QdrantConfig>,
    /// Embedding API config (None if unavailable → no semantic search)
    pub embedding: Option<EmbeddingConfig>,
    /// Code-symbol vector projection settings owned by gcode.
    pub code_vectors: CodeVectorSettings,
    /// Gobby daemon base URL (e.g. http://localhost:60887)
    pub daemon_url: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MissingIdentity {
    Error,
    Generate,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProjectIdentitySource {
    ProjectJson,
    GcodeJson,
    IsolatedRoot,
    LinkedWorktree,
    Generated,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProjectIdentity {
    pub project_id: String,
    pub root: PathBuf,
    pub source: ProjectIdentitySource,
    pub warning: Option<String>,
    pub should_write_gcode_json: bool,
}

impl Context {
    /// Resolve context from CLI args and filesystem state.
    pub fn resolve(project_override: Option<&str>, quiet: bool) -> anyhow::Result<Self> {
        let database_url = db::resolve_database_url()?;
        let project_root = match project_override {
            Some(p) => {
                let path = PathBuf::from(p);
                if path.is_dir() {
                    path.canonicalize()?
                } else {
                    // Not a directory — try name lookup in the PostgreSQL hub.
                    resolve_project_by_name(p, &database_url)?
                }
            }
            None => detect_project_root()?,
        };

        let identity = resolve_project_identity(&project_root, MissingIdentity::Error)?;
        warn_project_identity(&identity, quiet);
        let project_id = identity.project_id;

        // Resolve service configs from config_store (best-effort).
        let standalone_config = read_standalone_config();
        let mut conn = db::connect_readonly(&database_url)?;
        let falkordb = resolve_falkordb_config(&mut conn, standalone_config.clone(), quiet);
        let qdrant = resolve_qdrant_config(&mut conn, standalone_config.clone(), quiet);
        let embedding = resolve_embedding_config(&mut conn, standalone_config.clone(), quiet);
        let code_vectors = resolve_code_vector_settings(&mut conn, standalone_config)?;

        let daemon_url = resolve_daemon_url();

        Ok(Self {
            database_url,
            project_root,
            project_id,
            quiet,
            falkordb,
            qdrant,
            embedding,
            code_vectors,
            daemon_url,
        })
    }
}

pub fn resolve_project_identity(
    project_root: &Path,
    missing: MissingIdentity,
) -> anyhow::Result<ProjectIdentity> {
    let root = project_root
        .canonicalize()
        .unwrap_or_else(|_| absolute_fallback(project_root));

    if let Some(marker) = crate::project::read_isolation_marker(&root)
        && !is_self_referential_isolation_marker(&marker, &root)
    {
        return Ok(ProjectIdentity {
            project_id: crate::project::code_index_id_for_root(&root),
            root,
            source: ProjectIdentitySource::IsolatedRoot,
            warning: None,
            should_write_gcode_json: false,
        });
    }

    let worktree = git::worktree_info(&root)?;
    if worktree.kind == WorktreeKind::Linked {
        let project_id = crate::project::code_index_id_for_root(&worktree.top_level);
        let copied_id = read_project_id(&worktree.top_level).ok();
        let warning = copied_id
            .filter(|id| id != &project_id)
            .map(|id| {
                format!(
                    "linked git worktree {} has copied .gobby/project.json id {}; using filesystem-scoped code index id {}",
                    worktree.top_level.display(),
                    short_id(&id),
                    short_id(&project_id)
                )
            });

        return Ok(ProjectIdentity {
            project_id,
            root: worktree.top_level,
            source: ProjectIdentitySource::LinkedWorktree,
            warning,
            should_write_gcode_json: false,
        });
    }

    let gobby_dir = root.join(".gobby");
    if gobby_dir.join("project.json").exists() {
        return Ok(ProjectIdentity {
            project_id: read_project_id(&root)?,
            root,
            source: ProjectIdentitySource::ProjectJson,
            warning: None,
            should_write_gcode_json: false,
        });
    }
    if gobby_dir.join("gcode.json").exists() {
        return Ok(ProjectIdentity {
            project_id: crate::project::read_gcode_json(&root)?,
            root,
            source: ProjectIdentitySource::GcodeJson,
            warning: None,
            should_write_gcode_json: false,
        });
    }

    match missing {
        MissingIdentity::Generate => Ok(ProjectIdentity {
            project_id: crate::project::code_index_id_for_root(&root),
            root,
            source: ProjectIdentitySource::Generated,
            warning: None,
            should_write_gcode_json: true,
        }),
        MissingIdentity::Error => anyhow::bail!(
            "No gcode project found. Run `gcode init` to initialize, \
             or use `--project <path>` to specify a project directory."
        ),
    }
}

fn is_self_referential_isolation_marker(
    marker: &crate::project::IsolationMarker,
    root: &Path,
) -> bool {
    let Some(parent_project_path) = marker.parent_project_path.as_deref() else {
        return false;
    };
    let parent = PathBuf::from(parent_project_path);
    let parent = if parent.is_absolute() {
        parent
    } else {
        root.join(parent)
    };
    let parent = parent.canonicalize().unwrap_or(parent);
    parent == root
}

pub fn warn_project_identity(identity: &ProjectIdentity, quiet: bool) {
    if quiet {
        return;
    }
    if let Some(warning) = &identity.warning {
        eprintln!("Warning: {warning}");
    }
}

/// Resolve a `--project` name to a project root by looking up `code_indexed_projects`.
///
/// Matches against the basename of `root_path` in the PostgreSQL hub.
fn resolve_project_by_name(name: &str, database_url: &str) -> anyhow::Result<PathBuf> {
    let mut conn = db::connect_readonly(database_url)?;
    let rows = conn.query(
        "SELECT root_path FROM code_indexed_projects
         WHERE root_path = $1 OR root_path LIKE '%' || '/' || $1
         ORDER BY last_indexed_at DESC NULLS LAST
         LIMIT 1",
        &[&name],
    )?;

    if let Some(row) = rows.first() {
        let root_path: String = row.try_get("root_path")?;
        let path = PathBuf::from(&root_path);
        if path.is_dir() {
            return Ok(path);
        }
    }

    anyhow::bail!(
        "Project '{}' not found. Run `gcode projects` to see indexed projects.",
        name
    )
}

/// Detect project root by walking up the directory tree.
///
/// Resolution order:
/// 1. `.gobby/project.json` or `.gobby/gcode.json` (identity file)
/// 2. VCS root (`.git` or `.hg`)
/// 3. Current working directory
pub fn detect_project_root() -> anyhow::Result<PathBuf> {
    let cwd = std::env::current_dir()?;
    detect_project_root_from(&cwd)
}

pub fn detect_project_root_from(start: &Path) -> anyhow::Result<PathBuf> {
    let start = start
        .canonicalize()
        .unwrap_or_else(|_| absolute_fallback(start));
    let start = if start.is_file() {
        start
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_else(|| start.clone())
    } else {
        start
    };

    // First: look for an identity file (.gobby/project.json or .gobby/gcode.json)
    if let Some(root) = find_project_root(&start) {
        return Ok(root.canonicalize().unwrap_or(root));
    }

    // Second: prefer the Git worktree top-level, including linked worktrees.
    if let Ok(info) = git::worktree_info(&start)
        && info.kind != WorktreeKind::NotGit
    {
        return Ok(info.top_level);
    }

    // Third: fall back to VCS root
    let mut dir = start.as_path();
    loop {
        if dir.join(".git").exists() || dir.join(".hg").exists() {
            return Ok(dir.to_path_buf());
        }
        match dir.parent() {
            Some(parent) => dir = parent,
            None => return Ok(start), // Last resort: start
        }
    }
}

/// Resolve Gobby daemon base URL.
///
/// Resolution order:
/// 1. `GOBBY_PORT` env var (explicit override)
/// 2. `~/.gobby/bootstrap.yaml` `daemon_port` + `bind_host` keys
/// 3. Default: `http://localhost:60887`
pub(crate) fn resolve_daemon_url() -> Option<String> {
    // Env var override takes priority (empty value falls through to defaults)
    if let Ok(port) = std::env::var("GOBBY_PORT")
        && !port.is_empty()
    {
        return Some(format!("http://localhost:{port}"));
    }

    // Read from bootstrap.yaml
    let bootstrap_path = db::bootstrap_path().ok()?;
    if let Ok(contents) = std::fs::read_to_string(&bootstrap_path)
        && let Ok(yaml) = serde_yaml::from_str::<serde_yaml::Value>(&contents)
        && let Some(port) = yaml.get("daemon_port").and_then(|v| v.as_u64())
    {
        let host = yaml
            .get("bind_host")
            .and_then(|v| v.as_str())
            .unwrap_or("localhost");
        return Some(format!("http://{host}:{port}"));
    }

    // Well-known default (matches gsqz)
    Some("http://localhost:60887".to_string())
}

/// Resolve project ID from identity files or generate deterministically.
///
/// Resolution order:
/// 1. `.gobby/project.json` — gobby's file (reads `"id"`, falls back to `"project_id"`)
/// 2. `.gobby/gcode.json` — gcode's standalone identity
/// 3. Generate deterministic UUID5 from canonical path (no filesystem writes)
#[cfg(test)]
fn resolve_project_id(project_root: &Path) -> anyhow::Result<String> {
    Ok(resolve_project_identity(project_root, MissingIdentity::Error)?.project_id)
}

fn absolute_fallback(path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    }
}

// ── Config store adapter ─────────────────────────────────────────────

pub(crate) struct PostgresConfigSource<'a> {
    conn: &'a mut Client,
}

impl gobby_core::config::ConfigSource for PostgresConfigSource<'_> {
    fn config_value(&mut self, key: &str) -> Option<String> {
        let key = canonical_config_key(key);
        gobby_core::postgres::read_config_value(self.conn, key)
            .ok()
            .flatten()
            .and_then(|raw| gobby_core::config::decode_config_value(&raw))
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        secrets::resolve_config_value(value, self.conn)
    }
}

struct FallbackConfigSource<'a> {
    postgres: PostgresConfigSource<'a>,
    standalone: Option<StandaloneConfig>,
}

impl ConfigSource for FallbackConfigSource<'_> {
    fn config_value(&mut self, key: &str) -> Option<String> {
        self.postgres.config_value(key).or_else(|| {
            self.standalone
                .as_mut()
                .and_then(|standalone| standalone.config_value(key))
        })
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        self.postgres.resolve_value(value)
    }
}

fn read_standalone_config() -> Option<StandaloneConfig> {
    let home = db::gobby_home().ok()?;
    StandaloneConfig::read_at(&home.join(GCORE_CONFIG_FILENAME))
        .ok()
        .flatten()
}

#[cfg(test)]
struct ClosureConfigSource<R, S> {
    read_config_value: R,
    resolve_value: S,
}

#[cfg(test)]
impl<R, S> ConfigSource for ClosureConfigSource<R, S>
where
    R: FnMut(&str) -> Option<String>,
    S: FnMut(&str) -> anyhow::Result<String>,
{
    fn config_value(&mut self, key: &str) -> Option<String> {
        (self.read_config_value)(key).and_then(|raw| gobby_core::config::decode_config_value(&raw))
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        (self.resolve_value)(value)
    }
}

fn canonical_config_key(key: &str) -> &str {
    match key {
        FALKORDB_HOST_CONFIG_KEY => FALKORDB_HOST_CONFIG_KEY,
        FALKORDB_PORT_CONFIG_KEY => FALKORDB_PORT_CONFIG_KEY,
        FALKORDB_PASSWORD_CONFIG_KEY => FALKORDB_PASSWORD_CONFIG_KEY,
        _ => key,
    }
}

#[cfg(test)]
fn resolve_falkordb_config_from_values<R, S>(
    read_config_value: R,
    resolve_value: S,
) -> Option<FalkorConfig>
where
    R: FnMut(&str) -> Option<String>,
    S: FnMut(&str) -> anyhow::Result<String>,
{
    let mut source = ClosureConfigSource {
        read_config_value,
        resolve_value,
    };
    resolve_falkordb_config_from_source(&mut source)
}

#[cfg(test)]
fn resolve_qdrant_config_from_values<R, S>(
    read_config_value: R,
    resolve_value: S,
) -> Option<QdrantConfig>
where
    R: FnMut(&str) -> Option<String>,
    S: FnMut(&str) -> anyhow::Result<String>,
{
    let mut source = ClosureConfigSource {
        read_config_value,
        resolve_value,
    };
    gobby_core::config::resolve_qdrant_config(&mut source)
}

#[cfg(test)]
fn resolve_embedding_config_from_values<R, S>(
    read_config_value: R,
    resolve_value: S,
) -> Option<EmbeddingConfig>
where
    R: FnMut(&str) -> Option<String>,
    S: FnMut(&str) -> anyhow::Result<String>,
{
    let mut source = ClosureConfigSource {
        read_config_value,
        resolve_value,
    };
    gobby_core::config::resolve_embedding_config(&mut source)
}

#[cfg(test)]
fn resolve_code_vector_settings_from_values<R>(
    read_config_value: R,
) -> Result<CodeVectorSettings, CodeVectorConfigError>
where
    R: FnMut(&str) -> Option<String>,
{
    let mut source = ClosureConfigSource {
        read_config_value,
        resolve_value: |value: &str| Ok(value.to_string()),
    };
    resolve_code_vector_settings_from_source(&mut source)
}

/// Resolve FalkorDB configuration from config_store + env vars.
fn resolve_falkordb_config(
    conn: &mut Client,
    standalone: Option<StandaloneConfig>,
    _quiet: bool,
) -> Option<FalkorConfig> {
    let mut source = FallbackConfigSource {
        postgres: PostgresConfigSource { conn },
        standalone,
    };
    resolve_falkordb_config_from_source(&mut source)
}

fn resolve_falkordb_config_from_source(source: &mut impl ConfigSource) -> Option<FalkorConfig> {
    let connection = gobby_core::config::resolve_falkordb_config(source)?;

    Some(FalkorConfig {
        host: connection.host,
        port: connection.port,
        password: connection.password,
        graph_name: FALKORDB_GRAPH_NAME.to_string(),
    })
}

/// Resolve Qdrant configuration from config_store + env vars.
fn resolve_qdrant_config(
    conn: &mut Client,
    standalone: Option<StandaloneConfig>,
    _quiet: bool,
) -> Option<QdrantConfig> {
    let mut source = FallbackConfigSource {
        postgres: PostgresConfigSource { conn },
        standalone,
    };
    gobby_core::config::resolve_qdrant_config(&mut source)
}

/// Resolve embedding API configuration from config_store + env vars.
///
/// Returns None if no api_base is found (→ no semantic search, BM25 only).
fn resolve_embedding_config(
    conn: &mut Client,
    standalone: Option<StandaloneConfig>,
    _quiet: bool,
) -> Option<EmbeddingConfig> {
    let mut source = FallbackConfigSource {
        postgres: PostgresConfigSource { conn },
        standalone,
    };
    gobby_core::config::resolve_embedding_config(&mut source)
}

pub(crate) fn resolve_code_vector_settings(
    conn: &mut Client,
    standalone: Option<StandaloneConfig>,
) -> Result<CodeVectorSettings, CodeVectorConfigError> {
    let mut source = FallbackConfigSource {
        postgres: PostgresConfigSource { conn },
        standalone,
    };
    resolve_code_vector_settings_from_source(&mut source)
}

pub(crate) fn resolve_code_vector_settings_from_source(
    source: &mut impl ConfigSource,
) -> Result<CodeVectorSettings, CodeVectorConfigError> {
    let vector_dim = match std::env::var(GOBBY_EMBEDDING_VECTOR_DIM_ENV)
        .ok()
        .filter(|value| !value.trim().is_empty())
    {
        Some(value) => Some(parse_vector_dim(
            GOBBY_EMBEDDING_VECTOR_DIM_ENV,
            value.trim(),
        )?),
        None => source
            .config_value(EMBEDDING_VECTOR_DIM_CONFIG_KEY)
            .map(|value| parse_vector_dim(EMBEDDING_VECTOR_DIM_CONFIG_KEY, value.trim()))
            .transpose()?,
    };

    Ok(CodeVectorSettings { vector_dim })
}

fn parse_vector_dim(source: &'static str, value: &str) -> Result<usize, CodeVectorConfigError> {
    value
        .parse::<usize>()
        .ok()
        .filter(|size| *size > 0)
        .ok_or_else(|| CodeVectorConfigError::InvalidVectorDim {
            source,
            value: value.to_string(),
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;

    fn write_project_json(root: &Path, json: serde_json::Value) {
        let gobby_dir = root.join(".gobby");
        std::fs::create_dir_all(&gobby_dir).expect("create .gobby");
        std::fs::write(
            gobby_dir.join("project.json"),
            serde_json::to_string_pretty(&json).expect("serialize project json"),
        )
        .expect("write project json");
    }

    fn run_git(dir: &Path, args: &[&str]) {
        let status = Command::new("git")
            .arg("-C")
            .arg(dir)
            .args(args)
            .status()
            .expect("run git");
        assert!(status.success(), "git {:?} failed", args);
    }

    fn create_linked_worktree(tmp: &tempfile::TempDir) -> (PathBuf, PathBuf) {
        let repo = tmp.path().join("repo");
        let linked = tmp.path().join("linked");
        std::fs::create_dir(&repo).expect("create repo");
        run_git(&repo, &["init"]);
        std::fs::write(repo.join("README.md"), "hello\n").expect("write readme");
        run_git(&repo, &["add", "README.md"]);
        run_git(
            &repo,
            &[
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Test User",
                "commit",
                "-m",
                "initial",
            ],
        );
        run_git(
            &repo,
            &[
                "worktree",
                "add",
                "-b",
                "linked-branch",
                linked.to_str().unwrap(),
            ],
        );
        (repo, linked)
    }

    fn clear_service_env() {
        for key in [
            "GOBBY_FALKORDB_HOST",
            "GOBBY_FALKORDB_PORT",
            "GOBBY_FALKORDB_PASSWORD",
            "GOBBY_QDRANT_URL",
            "GOBBY_QDRANT_API_KEY",
            "GOBBY_EMBEDDING_URL",
            "GOBBY_EMBEDDING_MODEL",
            "GOBBY_EMBEDDING_API_KEY",
            "GOBBY_EMBEDDING_VECTOR_DIM",
        ] {
            unsafe { std::env::remove_var(key) };
        }
    }

    fn config_value_for<'a>(
        values: &'a std::collections::HashMap<&'a str, &'a str>,
    ) -> impl FnMut(&str) -> Option<String> + 'a {
        |key| values.get(key).map(|value| (*value).to_string())
    }

    #[test]
    #[serial_test::serial]
    fn adapter_env_precedence_and_json_decode() {
        clear_service_env();
        unsafe { std::env::set_var("GOBBY_FALKORDB_HOST", "env-falkor.local") };
        let values = std::collections::HashMap::from([
            ("databases.falkordb.host", r#""stored-falkor.local""#),
            ("databases.falkordb.port", r#""16380""#),
            ("databases.falkordb.requirepass", r#""stored-pass""#),
            ("databases.qdrant.url", r#""http://qdrant.local:6333""#),
            ("databases.qdrant.api_key", r#""qdrant-key""#),
            ("embeddings.api_base", r#""http://embeddings.local:11434""#),
            ("embeddings.model", r#""embed-model""#),
            ("embeddings.api_key", "null"),
        ]);

        let falkor = resolve_falkordb_config_from_values(config_value_for(&values), |value| {
            Ok(value.to_string())
        })
        .expect("falkordb config");
        let qdrant = resolve_qdrant_config_from_values(config_value_for(&values), |value| {
            Ok(value.to_string())
        })
        .expect("qdrant config");
        let embedding = resolve_embedding_config_from_values(config_value_for(&values), |value| {
            Ok(value.to_string())
        })
        .expect("embedding config");

        assert_eq!(falkor.host, "env-falkor.local");
        assert_eq!(falkor.port, 16380);
        assert_eq!(falkor.password.as_deref(), Some("stored-pass"));
        assert_eq!(qdrant.url.as_deref(), Some("http://qdrant.local:6333"));
        assert_eq!(qdrant.api_key.as_deref(), Some("qdrant-key"));
        assert_eq!(embedding.api_base, "http://embeddings.local:11434");
        assert_eq!(embedding.model, "embed-model");
        assert_eq!(embedding.api_key, None);
        clear_service_env();
    }

    #[test]
    #[serial_test::serial]
    fn adapter_resolves_config_store_secrets() {
        clear_service_env();
        let values = std::collections::HashMap::from([
            ("databases.falkordb.host", "falkor.local"),
            (
                "databases.falkordb.requirepass",
                "$secret:falkordb_password",
            ),
            ("databases.qdrant.url", "http://qdrant.local:6333"),
            ("databases.qdrant.api_key", "$secret:qdrant_api_key"),
            ("embeddings.api_base", "http://embeddings.local:11434"),
            ("embeddings.api_key", "$secret:embedding_api_key"),
        ]);

        fn resolve_secret_stub(value: &str) -> anyhow::Result<String> {
            match value {
                "$secret:falkordb_password" => Ok("resolved-falkor".to_string()),
                "$secret:qdrant_api_key" => Ok("resolved-qdrant".to_string()),
                "$secret:embedding_api_key" => Ok("resolved-embedding".to_string()),
                value => Ok(value.to_string()),
            }
        }

        let falkor =
            resolve_falkordb_config_from_values(config_value_for(&values), resolve_secret_stub)
                .expect("falkordb config");
        let qdrant =
            resolve_qdrant_config_from_values(config_value_for(&values), resolve_secret_stub)
                .expect("qdrant config");
        let embedding =
            resolve_embedding_config_from_values(config_value_for(&values), resolve_secret_stub)
                .expect("embedding config");

        assert_eq!(falkor.password.as_deref(), Some("resolved-falkor"));
        assert_eq!(qdrant.api_key.as_deref(), Some("resolved-qdrant"));
        assert_eq!(embedding.api_key.as_deref(), Some("resolved-embedding"));
    }

    #[test]
    #[serial_test::serial]
    fn vector_dim_setting_resolves_env_and_config_store() {
        clear_service_env();
        let values = std::collections::HashMap::from([("embeddings.vector_dim", "1536")]);

        let settings = resolve_code_vector_settings_from_values(config_value_for(&values))
            .expect("config-store vector settings");
        assert_eq!(settings.vector_dim, Some(1536));

        unsafe { std::env::set_var("GOBBY_EMBEDDING_VECTOR_DIM", "3072") };
        let settings = resolve_code_vector_settings_from_values(config_value_for(&values))
            .expect("env vector settings");
        assert_eq!(settings.vector_dim, Some(3072));

        unsafe { std::env::remove_var("GOBBY_EMBEDDING_VECTOR_DIM") };
        let null_values = std::collections::HashMap::from([("embeddings.vector_dim", "null")]);
        let settings = resolve_code_vector_settings_from_values(config_value_for(&null_values))
            .expect("null config-store vector settings");
        assert_eq!(settings.vector_dim, None);

        let invalid_values =
            std::collections::HashMap::from([("embeddings.vector_dim", r#""wide""#)]);
        let err = resolve_code_vector_settings_from_values(config_value_for(&invalid_values))
            .expect_err("invalid vector dim must error");
        assert!(matches!(
            err,
            CodeVectorConfigError::InvalidVectorDim { .. }
        ));
        clear_service_env();
    }

    #[test]
    fn falkor_config_wrapper_shape() {
        let source = include_str!("config.rs");
        assert!(source.contains("pub struct FalkorConfig"));
        assert!(source.contains("pub graph_name: String"));
        assert!(source.contains("gobby_core::config::resolve_falkordb_config"));
        assert!(source.contains("graph_name: FALKORDB_GRAPH_NAME.to_string()"));
    }

    #[test]
    fn phase7_context_and_falkor_resolver_visible() {
        let source = include_str!("config.rs");
        assert!(source.contains("pub falkordb: Option<FalkorConfig>"));
        assert!(source.contains("let falkordb = resolve_falkordb_config("));
        assert!(source.contains("pub const FALKORDB_GRAPH_NAME: &str = \"gobby_code\";"));
        assert!(source.contains("graph_name: FALKORDB_GRAPH_NAME.to_string()"));
    }

    #[test]
    fn phase7_falkordb_config_store_keys_visible() {
        let source = include_str!("config.rs");
        for key in [
            FALKORDB_HOST_CONFIG_KEY,
            FALKORDB_PORT_CONFIG_KEY,
            FALKORDB_PASSWORD_CONFIG_KEY,
            GOBBY_FALKORDB_HOST_ENV,
            GOBBY_FALKORDB_PORT_ENV,
            GOBBY_FALKORDB_PASSWORD_ENV,
        ] {
            assert!(source.contains(key), "missing {key}");
        }
    }

    #[test]
    fn phase7_neo4j_transition_state_absent() {
        let source = include_str!("config.rs");
        let config_type = ["pub struct Neo", "4jConfig"].concat();
        let resolver = ["resolve_neo", "4j_config"].concat();
        let context_field = ["pub neo", "4j: Option<Neo", "4jConfig>"].concat();
        assert!(!source.contains(&config_type));
        assert!(!source.contains(&resolver));
        assert!(!source.contains(&context_field));
    }

    #[test]
    fn test_resolve_project_id_requires_project_context() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let err = resolve_project_id(tmp.path()).expect_err("missing project context must fail");

        assert!(
            err.to_string().contains("No gcode project found"),
            "unexpected error: {err}"
        );
        assert!(
            err.to_string().contains("gcode init"),
            "unexpected error: {err}"
        );
    }

    #[test]
    fn main_repo_keeps_project_json_id() {
        let tmp = tempfile::tempdir().expect("tempdir");
        write_project_json(
            tmp.path(),
            serde_json::json!({
                "id": "main-project-id",
                "name": "main"
            }),
        );

        let identity =
            resolve_project_identity(tmp.path(), MissingIdentity::Error).expect("identity");

        assert_eq!(identity.project_id, "main-project-id");
        assert_eq!(identity.source, ProjectIdentitySource::ProjectJson);
        assert!(!identity.should_write_gcode_json);
        assert!(identity.warning.is_none());
    }

    #[test]
    fn self_referential_parent_marker_keeps_project_json_id() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path().canonicalize().expect("canonical root");
        write_project_json(
            &root,
            serde_json::json!({
                "id": "main-project-id",
                "name": "main",
                "parent_project_path": root.to_string_lossy(),
                "parent_project_id": "main-project-id"
            }),
        );

        let identity = resolve_project_identity(&root, MissingIdentity::Error).expect("identity");

        assert_eq!(identity.project_id, "main-project-id");
        assert_eq!(identity.source, ProjectIdentitySource::ProjectJson);
        assert!(!identity.should_write_gcode_json);
        assert!(identity.warning.is_none());
    }

    #[test]
    fn isolated_marker_uses_path_derived_id_without_warning() {
        let tmp = tempfile::tempdir().expect("tempdir");
        write_project_json(
            tmp.path(),
            serde_json::json!({
                "id": "parent-id",
                "parent_project_path": "/parent",
                "parent_project_id": "parent-id"
            }),
        );

        let identity =
            resolve_project_identity(tmp.path(), MissingIdentity::Error).expect("identity");

        assert_eq!(
            identity.project_id,
            crate::project::code_index_id_for_root(tmp.path())
        );
        assert_eq!(identity.source, ProjectIdentitySource::IsolatedRoot);
        assert!(!identity.should_write_gcode_json);
        assert!(identity.warning.is_none());
    }

    #[test]
    fn linked_worktree_uses_path_id_and_warns_only_for_copied_project_id() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let (_repo, linked) = create_linked_worktree(&tmp);

        let identity = resolve_project_identity(&linked, MissingIdentity::Error).expect("identity");

        assert_eq!(
            identity.project_id,
            crate::project::code_index_id_for_root(&linked)
        );
        assert_eq!(identity.source, ProjectIdentitySource::LinkedWorktree);
        assert!(identity.warning.is_none());
        assert!(!identity.should_write_gcode_json);

        write_project_json(
            &linked,
            serde_json::json!({
                "id": "copied-parent-id",
                "name": "linked"
            }),
        );
        let copied =
            resolve_project_identity(&linked, MissingIdentity::Error).expect("copied identity");

        assert_eq!(copied.source, ProjectIdentitySource::LinkedWorktree);
        assert_eq!(
            copied.project_id,
            crate::project::code_index_id_for_root(&linked)
        );
        assert!(copied.warning.as_deref().unwrap_or("").contains("copied"));
        assert!(!copied.should_write_gcode_json);
    }

    #[test]
    fn generated_identity_writes_only_for_non_isolated_roots() {
        let tmp = tempfile::tempdir().expect("tempdir");

        let identity =
            resolve_project_identity(tmp.path(), MissingIdentity::Generate).expect("identity");

        assert_eq!(identity.source, ProjectIdentitySource::Generated);
        assert!(identity.should_write_gcode_json);
        assert_eq!(
            identity.project_id,
            crate::project::code_index_id_for_root(tmp.path())
        );
    }
}
