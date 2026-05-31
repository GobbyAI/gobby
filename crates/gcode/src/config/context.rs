//! Configuration resolution for gcode.
//!
//! Reads bootstrap.yaml → PostgreSQL hub → config_store → service configs.
//! Resolves $secret:NAME and ${VAR} patterns.
//!
//! Source: src/gobby/config/bootstrap.py, src/gobby/config/persistence.py

use std::fmt;
use std::path::{Path, PathBuf};

use gobby_core::project::{find_project_root, read_project_id};
use postgres::Client;

use super::services::{
    read_standalone_config_optional, resolve_code_vector_settings, resolve_embedding_config,
    resolve_falkordb_config, resolve_qdrant_config,
};
use crate::db;
use crate::git::{self, WorktreeKind};
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
#[derive(Debug, Clone)]
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
    /// Project read/index scope.
    pub index_scope: ProjectIndexScope,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub enum ProjectIndexScope {
    #[default]
    Single,
    Overlay {
        overlay_project_id: String,
        overlay_root: PathBuf,
        parent_project_id: String,
        parent_root: PathBuf,
    },
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
    IsolatedOverlay,
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
    pub index_scope: ProjectIndexScope,
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
        let index_scope = identity.index_scope;

        // Resolve service configs from config_store (best-effort).
        let standalone_config = read_standalone_config_optional();
        let mut conn = db::connect_readonly(&database_url)?;
        validate_parent_code_index(&mut conn, &index_scope)?;
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
            index_scope,
        })
    }

    /// Resolve service config for a caller-supplied project id without touching cwd identity.
    pub fn resolve_for_project_id(project_id: &str, quiet: bool) -> anyhow::Result<Self> {
        let project_id = normalize_project_id(project_id)?;
        let database_url = db::resolve_database_url()?;

        let standalone_config = read_standalone_config_optional();
        let mut conn = db::connect_readonly(&database_url)?;
        let falkordb = resolve_falkordb_config(&mut conn, standalone_config, quiet);

        let daemon_url = resolve_daemon_url();

        Ok(Self {
            database_url,
            project_root: PathBuf::new(),
            project_id,
            quiet,
            falkordb,
            qdrant: None,
            embedding: None,
            code_vectors: CodeVectorSettings::default(),
            daemon_url,
            index_scope: ProjectIndexScope::Single,
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
        if let (Some(parent_project_path), Some(parent_project_id)) = (
            marker.parent_project_path.as_deref(),
            marker.parent_project_id.as_deref(),
        ) {
            let overlay_project_id = crate::project::code_index_id_for_root(&root);
            let parent_root = resolve_parent_project_root(&root, parent_project_path);
            let parent_project_id = normalize_project_id(parent_project_id)?;
            return Ok(ProjectIdentity {
                project_id: overlay_project_id.clone(),
                root: root.clone(),
                source: ProjectIdentitySource::IsolatedOverlay,
                warning: None,
                should_write_gcode_json: false,
                index_scope: ProjectIndexScope::Overlay {
                    overlay_project_id,
                    overlay_root: root,
                    parent_project_id,
                    parent_root,
                },
            });
        }

        return Ok(ProjectIdentity {
            project_id: crate::project::code_index_id_for_root(&root),
            root,
            source: ProjectIdentitySource::IsolatedRoot,
            warning: None,
            should_write_gcode_json: false,
            index_scope: ProjectIndexScope::Single,
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
            index_scope: ProjectIndexScope::Single,
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
            index_scope: ProjectIndexScope::Single,
        });
    }
    if gobby_dir.join("gcode.json").exists() {
        return Ok(ProjectIdentity {
            project_id: crate::project::read_gcode_json(&root)?,
            root,
            source: ProjectIdentitySource::GcodeJson,
            warning: None,
            should_write_gcode_json: false,
            index_scope: ProjectIndexScope::Single,
        });
    }

    match missing {
        MissingIdentity::Generate => Ok(ProjectIdentity {
            project_id: crate::project::code_index_id_for_root(&root),
            root,
            source: ProjectIdentitySource::Generated,
            warning: None,
            should_write_gcode_json: true,
            index_scope: ProjectIndexScope::Single,
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
    resolve_parent_project_root(root, parent_project_path) == root
}

fn resolve_parent_project_root(root: &Path, parent_project_path: &str) -> PathBuf {
    let parent = PathBuf::from(parent_project_path);
    let parent = if parent.is_absolute() {
        parent
    } else {
        root.join(parent)
    };
    parent.canonicalize().unwrap_or(parent)
}

fn normalize_project_id(project_id: &str) -> anyhow::Result<String> {
    let project_id = project_id.trim();
    if project_id.is_empty() {
        anyhow::bail!("--project-id must not be empty");
    }
    Ok(project_id.to_string())
}

pub(crate) fn validate_parent_code_index(
    conn: &mut Client,
    scope: &ProjectIndexScope,
) -> anyhow::Result<()> {
    let ProjectIndexScope::Overlay {
        parent_project_id,
        parent_root,
        ..
    } = scope
    else {
        return Ok(());
    };

    let exists = conn
        .query_one(
            "SELECT EXISTS(
                SELECT 1 FROM code_indexed_files WHERE project_id = $1
            )",
            &[parent_project_id],
        )
        .and_then(|row| row.try_get::<_, bool>(0))?;

    if !exists {
        anyhow::bail!(
            "parent code index missing for {} ({})",
            parent_root.display(),
            short_id(parent_project_id)
        );
    }

    Ok(())
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
    let escaped_name = escape_like(name);
    let rows = conn.query(
        "SELECT root_path FROM code_indexed_projects
         WHERE root_path = $1 OR root_path LIKE '%' || '/' || $2 ESCAPE '\\'
         ORDER BY last_indexed_at DESC NULLS LAST
         LIMIT 1",
        &[&name, &escaped_name],
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

pub(super) fn escape_like(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    for ch in value.chars() {
        if matches!(ch, '\\' | '%' | '_') {
            escaped.push('\\');
        }
        escaped.push(ch);
    }
    escaped
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
pub(super) fn resolve_project_id(project_root: &Path) -> anyhow::Result<String> {
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
