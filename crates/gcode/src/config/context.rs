//! Configuration resolution for gcode.
//!
//! Reads bootstrap.yaml → PostgreSQL hub → config_store → service configs.
//! Resolves $secret:NAME and ${VAR} patterns.
//!
//! Source: src/gobby/config/bootstrap.py, src/gobby/config/persistence.py

use std::fmt;
use std::path::{Path, PathBuf};

use anyhow::Context as _;
use gobby_core::project::{find_project_root, read_project_id};
use postgres::Client;
use uuid::Uuid;

use super::services::{
    read_standalone_config_optional, resolve_code_vector_settings, resolve_embedding_config,
    resolve_falkordb_config, resolve_indexing_settings, resolve_qdrant_config,
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

pub const FALKORDB_GRAPH_NAME: &str = gobby_core::config::CODE_GRAPH_NAME;
pub const CODE_SYMBOL_COLLECTION_PREFIX: &str = "code_symbols_";

pub const GOBBY_FALKORDB_HOST_ENV: &str = "GOBBY_FALKORDB_HOST";
pub const GOBBY_FALKORDB_PORT_ENV: &str = "GOBBY_FALKORDB_PORT";
pub const GOBBY_FALKORDB_PASSWORD_ENV: &str = "GOBBY_FALKORDB_PASSWORD";

pub const FALKORDB_HOST_CONFIG_KEY: &str = "databases.falkordb.host";
pub const FALKORDB_PORT_CONFIG_KEY: &str = "databases.falkordb.port";
pub const FALKORDB_PASSWORD_CONFIG_KEY: &str = "databases.falkordb.password";

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct CodeVectorSettings {
    pub vector_dim: Option<usize>,
}

pub type IndexingSettings = gobby_core::config::IndexingConfig;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ServiceConfigSelection {
    pub falkordb: bool,
    pub qdrant: bool,
    pub embedding: bool,
    pub code_vectors: bool,
}

impl ServiceConfigSelection {
    pub const fn all() -> Self {
        Self {
            falkordb: true,
            qdrant: true,
            embedding: true,
            code_vectors: true,
        }
    }

    pub const fn database_only() -> Self {
        Self {
            falkordb: false,
            qdrant: false,
            embedding: false,
            code_vectors: false,
        }
    }

    pub const fn falkordb_only() -> Self {
        Self {
            falkordb: true,
            qdrant: false,
            embedding: false,
            code_vectors: false,
        }
    }

    pub const fn qdrant_only() -> Self {
        Self {
            falkordb: false,
            qdrant: true,
            embedding: false,
            code_vectors: false,
        }
    }

    pub const fn projection_cleanup() -> Self {
        Self {
            falkordb: true,
            qdrant: true,
            embedding: false,
            code_vectors: false,
        }
    }

    pub const fn vectors() -> Self {
        Self {
            falkordb: false,
            qdrant: true,
            embedding: true,
            code_vectors: true,
        }
    }

    pub const fn hybrid_search() -> Self {
        Self {
            falkordb: true,
            qdrant: true,
            embedding: true,
            code_vectors: false,
        }
    }
}

impl Default for ServiceConfigSelection {
    fn default() -> Self {
        Self::all()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CodeVectorConfigError {
    InvalidVectorDim { source: &'static str, value: String },
    Read { source: String },
}

impl fmt::Display for CodeVectorConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidVectorDim { source, value } => write!(
                f,
                "invalid code vector dimension from {source}: `{value}` must be a positive integer"
            ),
            Self::Read { source } => write!(f, "failed to read code vector config: {source}"),
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
    /// Shared indexing behavior.
    pub indexing: IndexingSettings,
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
        Self::resolve_with_services(project_override, quiet, ServiceConfigSelection::all())
    }

    pub fn resolve_with_services(
        project_override: Option<&str>,
        quiet: bool,
        services: ServiceConfigSelection,
    ) -> anyhow::Result<Self> {
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
        let falkordb = if services.falkordb {
            resolve_falkordb_config(&mut conn, standalone_config.clone(), quiet)?
        } else {
            None
        };
        let qdrant = if services.qdrant {
            resolve_qdrant_config(&mut conn, standalone_config.clone(), quiet)?
        } else {
            None
        };
        let embedding = if services.embedding {
            resolve_embedding_config(&mut conn, standalone_config.clone(), quiet)?
        } else {
            None
        };
        let indexing = resolve_indexing_settings(&mut conn, standalone_config.clone())?;
        let code_vectors = if services.code_vectors {
            resolve_code_vector_settings(&mut conn, standalone_config)?
        } else {
            CodeVectorSettings::default()
        };

        let daemon_url = Some(gobby_core::daemon_url::daemon_url());

        Ok(Self {
            database_url,
            project_root,
            project_id,
            quiet,
            falkordb,
            qdrant,
            embedding,
            code_vectors,
            indexing,
            daemon_url,
            index_scope,
        })
    }

    /// Resolve service config for a caller-supplied project id without touching cwd identity.
    ///
    /// This is for daemon-style calls that already know the target project id and must not
    /// discover a project root from cwd. The returned context therefore has an empty
    /// `project_root`, default code-vector settings, and `None` for services that are not
    /// needed by project-id-only graph operations.
    pub fn resolve_for_project_id(project_id: &str, quiet: bool) -> anyhow::Result<Self> {
        let project_id = normalize_project_id(project_id)?;
        let database_url = db::resolve_database_url()?;

        let standalone_config = read_standalone_config_optional();
        let mut conn = db::connect_readonly(&database_url)?;
        let falkordb = resolve_falkordb_config(&mut conn, standalone_config.clone(), quiet)?;
        let indexing = resolve_indexing_settings(&mut conn, standalone_config)?;

        let daemon_url = Some(gobby_core::daemon_url::daemon_url());

        Ok(Self {
            database_url,
            project_root: PathBuf::new(),
            project_id,
            quiet,
            falkordb,
            qdrant: None,
            embedding: None,
            code_vectors: CodeVectorSettings::default(),
            indexing,
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

    if let Some(marker) = crate::project::read_isolation_marker(&root) {
        if marker.parent_project_path.is_some() ^ marker.parent_project_id.is_some() {
            anyhow::bail!(
                "invalid isolation marker in {}: parent_project_path and parent_project_id must be set together",
                root.join(".gobby").join("project.json").display()
            );
        }

        if is_self_referential_isolation_marker(&marker, &root) {
            return resolve_non_isolated_project_identity(root, missing);
        }

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

    resolve_non_isolated_project_identity(root, missing)
}

fn resolve_non_isolated_project_identity(
    root: PathBuf,
    missing: MissingIdentity,
) -> anyhow::Result<ProjectIdentity> {
    let worktree = git::worktree_info(&root)?;
    if worktree.kind == WorktreeKind::Linked {
        let project_id = crate::project::code_index_id_for_root(&worktree.top_level);

        return Ok(ProjectIdentity {
            project_id,
            root: worktree.top_level,
            source: ProjectIdentitySource::LinkedWorktree,
            warning: None,
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
    Uuid::parse_str(project_id)
        .map(|id| id.to_string())
        .with_context(|| format!("--project-id must be a UUID, got `{project_id}`"))
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
    let (slash_suffix, backslash_suffix) = project_name_suffixes(name);
    let rows = conn.query(
        "SELECT root_path FROM code_indexed_projects
         WHERE root_path = $1
            OR right(root_path, length($2)) = $2
            OR right(root_path, length($3)) = $3
         ORDER BY last_indexed_at DESC NULLS LAST",
        &[&name, &slash_suffix, &backslash_suffix],
    )?;

    for row in rows {
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

pub(super) fn project_name_suffixes(name: &str) -> (String, String) {
    (format!("/{name}"), format!("\\{name}"))
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
            .unwrap_or_else(|_| std::env::temp_dir())
            .join(path)
    }
}
