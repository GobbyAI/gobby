//! Configuration resolution for gcode.
//!
//! Reads bootstrap.yaml → PostgreSQL hub → config_store → service configs.
//! Resolves $secret:NAME and ${VAR} patterns.
//!
//! Source: src/gobby/config/bootstrap.py, src/gobby/config/persistence.py

use std::path::{Path, PathBuf};

use gobby_core::project::{find_project_root, read_project_id};
use postgres::Client;

use crate::db;
use crate::git::{self, WorktreeKind};
use crate::secrets;

/// Neo4j connection configuration.
#[derive(Debug, Clone)]
pub struct Neo4jConfig {
    pub url: String,
    pub auth: Option<String>,
    pub database: String,
}

/// FalkorDB connection configuration.
#[derive(Debug, Clone)]
pub struct FalkorConfig {
    pub host: String,
    pub port: u16,
    pub password: Option<String>,
    pub graph_name: String,
}

/// Qdrant connection configuration.
#[derive(Debug, Clone)]
pub struct QdrantConfig {
    pub url: Option<String>,
    pub api_key: Option<String>,
    pub collection_prefix: String,
}

/// Embedding API configuration (OpenAI-compatible endpoint).
#[derive(Debug, Clone)]
pub struct EmbeddingConfig {
    pub api_base: String,
    pub model: String,
    pub api_key: Option<String>,
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
    /// Neo4j config (None if unavailable)
    pub neo4j: Option<Neo4jConfig>,
    /// Qdrant config (None if unavailable)
    pub qdrant: Option<QdrantConfig>,
    /// Embedding API config (None if unavailable → no semantic search)
    pub embedding: Option<EmbeddingConfig>,
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
        let mut conn = db::connect_readonly(&database_url)?;
        let falkordb = resolve_falkordb_config(&mut conn, quiet);
        let neo4j = resolve_neo4j_config(&mut conn, quiet);
        let qdrant = resolve_qdrant_config(&mut conn, quiet);
        let embedding = resolve_embedding_config(&mut conn, quiet);

        let daemon_url = resolve_daemon_url();

        Ok(Self {
            database_url,
            project_root,
            project_id,
            quiet,
            falkordb,
            neo4j,
            qdrant,
            embedding,
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

    if crate::project::read_isolation_marker(&root).is_some() {
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

fn short_id(id: &str) -> &str {
    id.get(..8).unwrap_or(id)
}

// ── Config store helpers ─────────────────────────────────────────────

/// Read a value from the config_store table, returning None if missing.
/// Values are stored as JSON — decode string values while preserving legacy text.
fn read_config_value(conn: &mut Client, key: &str) -> Option<String> {
    let raw: String = conn
        .query_opt("SELECT value FROM config_store WHERE key = $1", &[&key])
        .ok()??
        .try_get("value")
        .ok()?;
    decode_config_value(&raw)
}

fn decode_config_value(raw: &str) -> Option<String> {
    match serde_json::from_str::<serde_json::Value>(raw) {
        Ok(serde_json::Value::String(text)) => Some(text),
        Ok(value) => Some(value.to_string()),
        Err(_) => Some(raw.to_string()),
    }
}

const FALKORDB_DEFAULT_PORT: u16 = 16379;
const FALKORDB_GRAPH_NAME: &str = "gobby_code";

trait FalkorConfigSource {
    fn config_value(&mut self, key: &str) -> Option<String>;
    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String>;
}

struct PostgresFalkorConfigSource<'a> {
    conn: &'a mut Client,
}

impl FalkorConfigSource for PostgresFalkorConfigSource<'_> {
    fn config_value(&mut self, key: &str) -> Option<String> {
        read_config_value(self.conn, key)
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        secrets::resolve_config_value(value, self.conn)
    }
}

struct ClosureFalkorConfigSource<R, S> {
    read_config_value: R,
    resolve_value: S,
}

impl<R, S> FalkorConfigSource for ClosureFalkorConfigSource<R, S>
where
    R: FnMut(&str) -> Option<String>,
    S: FnMut(&str) -> anyhow::Result<String>,
{
    fn config_value(&mut self, key: &str) -> Option<String> {
        (self.read_config_value)(key)
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        (self.resolve_value)(value)
    }
}

fn resolve_falkordb_config_from_values<R, S>(
    read_config_value: R,
    quiet: bool,
    resolve_value: S,
) -> Option<FalkorConfig>
where
    R: FnMut(&str) -> Option<String>,
    S: FnMut(&str) -> anyhow::Result<String>,
{
    let mut source = ClosureFalkorConfigSource {
        read_config_value,
        resolve_value,
    };
    resolve_falkordb_config_from_source(&mut source, quiet)
}

/// Resolve FalkorDB configuration from config_store + env vars.
fn resolve_falkordb_config(conn: &mut Client, quiet: bool) -> Option<FalkorConfig> {
    let mut source = PostgresFalkorConfigSource { conn };
    resolve_falkordb_config_from_source(&mut source, quiet)
}

fn resolve_falkordb_config_from_source(
    source: &mut impl FalkorConfigSource,
    quiet: bool,
) -> Option<FalkorConfig> {
    let host = std::env::var("GOBBY_FALKORDB_HOST")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .or_else(|| {
            source
                .config_value("databases.falkordb.host")
                .filter(|value| !value.trim().is_empty())
        })?;

    let raw_port = std::env::var("GOBBY_FALKORDB_PORT")
        .ok()
        .or_else(|| source.config_value("databases.falkordb.port"));
    let port = parse_falkordb_port(raw_port.as_deref(), quiet);

    let raw_password = std::env::var("GOBBY_FALKORDB_PASSWORD")
        .ok()
        .or_else(|| source.config_value("databases.falkordb.requirepass"))
        .filter(|value| !value.trim().is_empty());
    let password = match raw_password {
        Some(value) => match source.resolve_value(&value) {
            Ok(resolved) => Some(resolved),
            Err(e) => {
                if !quiet {
                    eprintln!("Warning: failed to resolve FalkorDB password: {e}");
                }
                None
            }
        },
        None => None,
    };

    Some(FalkorConfig {
        host,
        port,
        password,
        graph_name: FALKORDB_GRAPH_NAME.to_string(),
    })
}

fn parse_falkordb_port(raw_port: Option<&str>, quiet: bool) -> u16 {
    match raw_port {
        Some(raw) => match raw.parse::<u16>() {
            Ok(port) => port,
            Err(e) => {
                if !quiet {
                    eprintln!(
                        "Warning: invalid FalkorDB port `{raw}` ({e}); using {FALKORDB_DEFAULT_PORT}"
                    );
                }
                FALKORDB_DEFAULT_PORT
            }
        },
        None => FALKORDB_DEFAULT_PORT,
    }
}

/// Resolve Neo4j configuration from config_store + env vars.
fn resolve_neo4j_config(conn: &mut Client, quiet: bool) -> Option<Neo4jConfig> {
    // Read from config_store with env var overrides.
    let url = std::env::var("GOBBY_NEO4J_URL")
        .ok()
        .or_else(|| read_config_value(conn, "databases.neo4j.url"))
        .or_else(|| Some("http://localhost:8474".to_string()))?;

    let raw_auth = std::env::var("GOBBY_NEO4J_AUTH")
        .ok()
        .or_else(|| read_config_value(conn, "databases.neo4j.auth"));

    // Resolve $secret: patterns in auth
    let auth = match raw_auth {
        Some(v) => match secrets::resolve_config_value(&v, conn) {
            Ok(resolved) => Some(resolved),
            Err(e) => {
                if !quiet {
                    eprintln!("Warning: failed to resolve Neo4j auth: {e}");
                }
                None
            }
        },
        None => None,
    };

    let database =
        read_config_value(conn, "databases.neo4j.database").unwrap_or_else(|| "neo4j".to_string());

    Some(Neo4jConfig {
        url,
        auth,
        database,
    })
}

/// Resolve Qdrant configuration from config_store + env vars.
fn resolve_qdrant_config(conn: &mut Client, quiet: bool) -> Option<QdrantConfig> {
    let url = std::env::var("GOBBY_QDRANT_URL")
        .ok()
        .or_else(|| read_config_value(conn, "databases.qdrant.url"));

    let raw_api_key = read_config_value(conn, "databases.qdrant.api_key");
    let api_key = match raw_api_key {
        Some(v) => match secrets::resolve_config_value(&v, conn) {
            Ok(resolved) => Some(resolved),
            Err(e) => {
                if !quiet {
                    eprintln!("Warning: failed to resolve Qdrant API key: {e}");
                }
                None
            }
        },
        None => None,
    };

    let collection_prefix = read_config_value(conn, "databases.qdrant.collection_prefix")
        .unwrap_or_else(|| "code_symbols_".to_string());

    // Only return Some if there's a URL (qdrant_path = embedded mode, not accessible from CLI)
    url.as_ref()?;

    Some(QdrantConfig {
        url,
        api_key,
        collection_prefix,
    })
}

/// Resolve embedding API configuration from config_store + env vars.
///
/// Returns None if no api_base is found (→ no semantic search, BM25 only).
fn resolve_embedding_config(conn: &mut Client, quiet: bool) -> Option<EmbeddingConfig> {
    // Env var overrides
    let api_base = std::env::var("GOBBY_EMBEDDING_URL").ok();

    let api_base = api_base.or_else(|| read_config_value(conn, "embeddings.api_base"))?;

    // Model (env override → config_store → default)
    let model = std::env::var("GOBBY_EMBEDDING_MODEL")
        .ok()
        .or_else(|| read_config_value(conn, "embeddings.model"))
        .unwrap_or_else(|| "nomic-embed-text".to_string());

    // API key (env override → config_store with secret resolution)
    let api_key = std::env::var("GOBBY_EMBEDDING_API_KEY").ok().or_else(|| {
        let raw = read_config_value(conn, "embeddings.api_key")?;
        match secrets::resolve_config_value(&raw, conn) {
            Ok(resolved) => Some(resolved),
            Err(e) => {
                if !quiet {
                    eprintln!("Warning: failed to resolve embedding API key: {e}");
                }
                None
            }
        }
    });

    Some(EmbeddingConfig {
        api_base,
        model,
        api_key,
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

    #[test]
    fn test_decode_config_store_values() {
        assert_eq!(
            decode_config_value("\"http://test:7474\""),
            Some("http://test:7474".to_string())
        );
        assert_eq!(
            decode_config_value("http://legacy:7474"),
            Some("http://legacy:7474".to_string())
        );
    }

    #[test]
    #[serial_test::serial]
    fn test_config_env_override() {
        unsafe { std::env::set_var("GOBBY_NEO4J_URL", "http://env-override:9999") };
        let url = std::env::var("GOBBY_NEO4J_URL").unwrap();
        assert_eq!(url, "http://env-override:9999");
        unsafe { std::env::remove_var("GOBBY_NEO4J_URL") };
    }

    fn clear_falkordb_env() {
        unsafe { std::env::remove_var("GOBBY_FALKORDB_HOST") };
        unsafe { std::env::remove_var("GOBBY_FALKORDB_PORT") };
        unsafe { std::env::remove_var("GOBBY_FALKORDB_PASSWORD") };
    }

    fn config_value_for<'a>(
        values: &'a std::collections::HashMap<&'a str, &'a str>,
    ) -> impl FnMut(&str) -> Option<String> + 'a {
        |key| values.get(key).map(|value| (*value).to_string())
    }

    #[test]
    #[serial_test::serial]
    fn falkordb_config_store_only_resolves_host_port_password() {
        clear_falkordb_env();
        let values = std::collections::HashMap::from([
            ("databases.falkordb.host", "falkor.local"),
            ("databases.falkordb.port", "16380"),
            ("databases.falkordb.requirepass", "stored-pass"),
        ]);

        let config =
            resolve_falkordb_config_from_values(config_value_for(&values), true, |value| {
                Ok(value.to_string())
            })
            .expect("falkordb config");

        assert_eq!(config.host, "falkor.local");
        assert_eq!(config.port, 16380);
        assert_eq!(config.password.as_deref(), Some("stored-pass"));
        assert_eq!(config.graph_name, "gobby_code");
    }

    #[test]
    #[serial_test::serial]
    fn falkordb_env_only_resolves_host_port_password() {
        clear_falkordb_env();
        unsafe { std::env::set_var("GOBBY_FALKORDB_HOST", "env-falkor.local") };
        unsafe { std::env::set_var("GOBBY_FALKORDB_PORT", "16381") };
        unsafe { std::env::set_var("GOBBY_FALKORDB_PASSWORD", "env-pass") };

        let values = std::collections::HashMap::new();
        let config =
            resolve_falkordb_config_from_values(config_value_for(&values), true, |value| {
                Ok(value.to_string())
            })
            .expect("falkordb config");

        assert_eq!(config.host, "env-falkor.local");
        assert_eq!(config.port, 16381);
        assert_eq!(config.password.as_deref(), Some("env-pass"));
        clear_falkordb_env();
    }

    #[test]
    #[serial_test::serial]
    fn falkordb_env_host_overrides_config_store_host() {
        clear_falkordb_env();
        unsafe { std::env::set_var("GOBBY_FALKORDB_HOST", "env-host.local") };
        let values = std::collections::HashMap::from([
            ("databases.falkordb.host", "stored-host.local"),
            ("databases.falkordb.port", "16382"),
        ]);

        let config =
            resolve_falkordb_config_from_values(config_value_for(&values), true, |value| {
                Ok(value.to_string())
            })
            .expect("falkordb config");

        assert_eq!(config.host, "env-host.local");
        assert_eq!(config.port, 16382);
        clear_falkordb_env();
    }

    #[test]
    #[serial_test::serial]
    fn falkordb_secret_password_resolves_through_secret_resolver() {
        clear_falkordb_env();
        let values = std::collections::HashMap::from([
            ("databases.falkordb.host", "falkor.local"),
            ("databases.falkordb.requirepass", "$secret:requirepass"),
        ]);

        let config =
            resolve_falkordb_config_from_values(config_value_for(&values), true, |value| {
                assert_eq!(value, "$secret:requirepass");
                Ok("resolved-pass".to_string())
            })
            .expect("falkordb config");

        assert_eq!(config.password.as_deref(), Some("resolved-pass"));
    }

    #[test]
    #[serial_test::serial]
    fn falkordb_config_missing_host_returns_none() {
        clear_falkordb_env();
        let values = std::collections::HashMap::from([
            ("databases.falkordb.port", "16379"),
            ("databases.falkordb.requirepass", "stored-pass"),
        ]);

        let config =
            resolve_falkordb_config_from_values(config_value_for(&values), true, |value| {
                Ok(value.to_string())
            });

        assert!(config.is_none());
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
    fn isolated_marker_uses_path_derived_id_without_warning() {
        let tmp = tempfile::tempdir().expect("tempdir");
        write_project_json(
            tmp.path(),
            serde_json::json!({
                "id": "copied-parent-id",
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
