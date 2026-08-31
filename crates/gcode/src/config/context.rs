//! Configuration resolution for gcode.
//!
//! Reads bootstrap.yaml → PostgreSQL hub → config_store → service configs.
//! Resolves unresolved secret marker and ${VAR} patterns.
//!
//! Source: src/gobby/config/bootstrap.py, src/gobby/config/persistence.py

#![allow(dead_code)]

use std::fmt;
use std::path::{Path, PathBuf};

use anyhow::Context as _;
use gobby_core::project::{find_project_root, read_project_id};
use postgres::Client;
use uuid::Uuid;

use super::layers::{HubConfigCapture, HubConfigCaptureStatus, ServiceSource};
use super::services::{
    resolve_code_vector_settings_from_source, resolve_embedding_config_from_service_source,
    resolve_falkordb_config_from_source, resolve_indexing_settings_from_source,
    resolve_qdrant_config_from_source,
};

use crate::cli_error::CliError;
use crate::daemon;
use crate::db;
use crate::git::{self, WorktreeKind};
use crate::utils::short_id;
use gobby_core::grant;

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

pub const FALKORDB_HOST_CONFIG_KEY: &str = "databases.falkordb.host";
pub const FALKORDB_PORT_CONFIG_KEY: &str = "databases.falkordb.port";
pub const FALKORDB_PASSWORD_CONFIG_KEY: &str = "databases.falkordb.password";

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct CodeVectorSettings {
    pub vector_dim: Option<usize>,
}

impl CodeVectorSettings {
    pub(crate) fn with_vector_dim(vector_dim: Option<usize>) -> Self {
        Self { vector_dim }
    }
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
    fn hub_capture(&self) -> HubConfigCapture {
        if self.embedding && self.code_vectors {
            HubConfigCapture::Required
        } else if self.falkordb || self.qdrant || self.embedding || self.code_vectors {
            HubConfigCapture::ImmediateBestEffort
        } else {
            HubConfigCapture::DeferredBestEffort
        }
    }

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
    /// Whether runtime configuration capture fell back to degraded mode.
    pub(crate) runtime_config_capture_degraded: bool,
    /// Shared indexing behavior.
    pub indexing: IndexingSettings,
    /// Gobby daemon base URL (e.g. http://localhost:60887)
    pub daemon_url: Option<String>,
    /// Grant-backed AI availability used by hybrid search and modality gates.
    pub grant_ai: Option<GrantAiRuntime>,
    /// Project read/index scope.
    pub index_scope: ProjectIndexScope,
}

/// Grant snapshot consumed by AI-aware gcode commands.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GrantAiRuntime {
    pub capabilities: gobby_core::grant::GrantCapabilities,
    pub daemon_reachable: bool,
    pub unexpired: bool,
    pub bundle: gobby_core::grant::GrantBundle,
}

type ResolvedServices = (
    Option<FalkorConfig>,
    Option<QdrantConfig>,
    Option<EmbeddingConfig>,
    IndexingSettings,
    CodeVectorSettings,
    bool,
    Option<i64>,
);

fn resolve_services(
    conn: &mut Client,
    layers: &super::layers::ConfigLayers,
    services: ServiceConfigSelection,
) -> anyhow::Result<ResolvedServices> {
    let (mut source, revision, capture_status) =
        ServiceSource::new(conn, layers, services.hub_capture())?;
    let falkordb = if services.falkordb {
        resolve_falkordb_config_from_source(&mut source)?
    } else {
        None
    };
    let qdrant = if services.qdrant {
        resolve_qdrant_config_from_source(&mut source)?
    } else {
        None
    };
    let embedding = if services.embedding {
        resolve_embedding_config_from_service_source(None, &mut source)?
    } else {
        None
    };
    let indexing = resolve_indexing_settings_from_source(&mut source)?;
    let code_vectors = if services.code_vectors {
        resolve_code_vector_settings_from_source(&mut source)?
    } else {
        CodeVectorSettings::default()
    };
    let runtime_config_capture_degraded = capture_status == HubConfigCaptureStatus::Degraded;
    Ok((
        falkordb,
        qdrant,
        embedding,
        indexing,
        code_vectors,
        runtime_config_capture_degraded,
        revision,
    ))
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

#[derive(Debug)]
struct MissingProjectIdentity;

impl fmt::Display for MissingProjectIdentity {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(
            "No gcode project found. Run `gcode init` to initialize, \
             or use `--project <path>` to specify a project directory.",
        )
    }
}

impl std::error::Error for MissingProjectIdentity {}

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
    pub(crate) fn runtime_config_capture_degraded(&self) -> bool {
        self.runtime_config_capture_degraded
    }

    #[cfg(test)]
    pub(crate) fn set_runtime_config_capture_degraded_for_test(&mut self, degraded: bool) {
        self.runtime_config_capture_degraded = degraded;
    }

    /// Resolve context from CLI args and filesystem state.
    pub fn resolve(project_override: Option<&str>, quiet: bool) -> anyhow::Result<Self> {
        Self::resolve_with_services(project_override, quiet, ServiceConfigSelection::all())
    }

    pub fn resolve_with_services(
        project_override: Option<&str>,
        quiet: bool,
        services: ServiceConfigSelection,
    ) -> anyhow::Result<Self> {
        let project_root = match project_override {
            Some(value) => resolve_override_root(value)?,
            None => detect_project_root()
                .map_err(|error| error.context(CliError::project_required()))?,
        };
        Self::resolve_from_root(project_root, project_override.is_some(), quiet, services)
    }

    fn resolve_from_root(
        project_root: PathBuf,
        explicit_root: bool,
        quiet: bool,
        services: ServiceConfigSelection,
    ) -> anyhow::Result<Self> {
        let identity = identity_for_resolved_root(&project_root, explicit_root)?;
        warn_project_identity(&identity, quiet);
        let project_id = identity.project_id;
        let index_scope = identity.index_scope;

        let acquired = grant::acquire(&project_root).map_err(CliError::grant)?;
        let database_url = db::database_url_from_acquired(&acquired)?;
        let falkordb = services
            .falkordb
            .then(|| db::falkor_from_grant(&acquired.bundle))
            .flatten();
        let qdrant = services
            .qdrant
            .then(|| db::qdrant_from_grant(&acquired.bundle))
            .flatten();

        let mut conn = db::connect_readonly(&database_url)?;
        validate_parent_code_index(&mut conn, &index_scope)?;
        let (embedding, indexing, code_vectors) =
            services_from_acquired_settings(&acquired, services)?;
        let grant_ai = Some(GrantAiRuntime {
            capabilities: acquired.bundle.capabilities.clone(),
            daemon_reachable: acquired.daemon_reachable,
            unexpired: acquired.permits_datastore(),
            bundle: acquired.bundle.clone(),
        });

        Ok(Self {
            database_url,
            project_root,
            project_id,
            quiet,
            falkordb,
            qdrant,
            embedding,
            code_vectors,
            runtime_config_capture_degraded: false,
            indexing,
            daemon_url: Some(gobby_core::daemon_url::daemon_url()),
            grant_ai,
            index_scope,
        })
    }

    /// Resolve selected service configs for a caller-supplied project id from the
    /// caller's grant alone.
    ///
    /// No project root or daemon project listing is consulted, so projection
    /// lifecycle commands (`graph clear`, `vector clear`, `invalidate`) work for
    /// indexed projects whose checkout or registry row is already gone. The grant
    /// must cover `project_id` (as principal project or overlay), which keeps the
    /// root-less path bound to the caller's authority.
    pub fn resolve_for_project_id_with_services(
        project_id: &str,
        quiet: bool,
        services: ServiceConfigSelection,
    ) -> anyhow::Result<Self> {
        let project_id = normalize_project_id(project_id)?;
        let acquired = grant::acquire_with(&grant::AcquireRequest::from_process_for_project_id(
            &project_id,
        ))
        .map_err(CliError::grant)?;
        let database_url = db::database_url_from_acquired(&acquired)?;
        let falkordb = services
            .falkordb
            .then(|| db::falkor_from_grant(&acquired.bundle))
            .flatten();
        let qdrant = services
            .qdrant
            .then(|| db::qdrant_from_grant(&acquired.bundle))
            .flatten();
        let (embedding, indexing, code_vectors) =
            services_from_acquired_settings(&acquired, services)?;
        let grant_ai = Some(GrantAiRuntime {
            capabilities: acquired.bundle.capabilities.clone(),
            daemon_reachable: acquired.daemon_reachable,
            unexpired: acquired.permits_datastore(),
            bundle: acquired.bundle.clone(),
        });

        Ok(Self {
            database_url,
            project_root: PathBuf::new(),
            project_id,
            quiet,
            falkordb,
            qdrant,
            embedding,
            code_vectors,
            runtime_config_capture_degraded: false,
            indexing,
            daemon_url: Some(gobby_core::daemon_url::daemon_url()),
            grant_ai,
            index_scope: ProjectIndexScope::Single,
        })
    }
}

pub(super) fn identity_for_resolved_root(
    project_root: &Path,
    explicit_root: bool,
) -> anyhow::Result<ProjectIdentity> {
    match resolve_project_identity(project_root, MissingIdentity::Error) {
        Ok(identity) => Ok(identity),
        Err(error) if !explicit_root && is_missing_project_identity(&error) => {
            Err(error.context(CliError::project_required()))
        }
        Err(error) => Err(error),
    }
}

fn is_missing_project_identity(error: &anyhow::Error) -> bool {
    error.downcast_ref::<MissingProjectIdentity>().is_some()
}

fn services_from_acquired_settings(
    acquired: &grant::AcquiredGrant,
    services: ServiceConfigSelection,
) -> anyhow::Result<(
    Option<EmbeddingConfig>,
    IndexingSettings,
    CodeVectorSettings,
)> {
    let Some(cached) = acquired.settings.as_ref() else {
        return Ok((
            None,
            IndexingSettings::default(),
            CodeVectorSettings::default(),
        ));
    };
    super::services::resolve_from_grant_settings(&cached.settings, services)
}

fn resolve_override_root(project_override: &str) -> anyhow::Result<PathBuf> {
    let path = PathBuf::from(project_override);
    if path.is_dir() {
        return Ok(path.canonicalize()?);
    }
    resolve_project_by_name(project_override)
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
                root.join(".gobby").join("isolation.json").display()
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
        MissingIdentity::Error => Err(MissingProjectIdentity.into()),
    }
}

use gobby_core::project::{is_self_referential_isolation_marker, resolve_parent_project_root};

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

    let machine_id = db::id_param(&gobby_core::machine::read_local_machine_id()?)?;
    let exists = conn
        .query_one(
            "SELECT EXISTS(
                SELECT 1 FROM code_indexed_file_states
                WHERE machine_id = $1 AND project_id = $2
            )",
            &[&machine_id, &db::id_param(parent_project_id)?],
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

/// Resolve a `--project` name through the calling daemon's local checkout view.
pub(super) fn resolve_project_by_name(name: &str) -> anyhow::Result<PathBuf> {
    Ok(daemon::lookup_project_by_name(name)?.root)
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

#[cfg(test)]
mod capture_tests {
    use super::{HubConfigCapture, ServiceConfigSelection};

    #[test]
    fn service_selection_sets_capture_strictness() {
        assert_eq!(
            ServiceConfigSelection::database_only().hub_capture(),
            HubConfigCapture::DeferredBestEffort
        );
        assert_eq!(
            ServiceConfigSelection::hybrid_search().hub_capture(),
            HubConfigCapture::ImmediateBestEffort
        );
        assert_eq!(
            ServiceConfigSelection::all().hub_capture(),
            HubConfigCapture::Required
        );
        assert_eq!(
            ServiceConfigSelection::vectors().hub_capture(),
            HubConfigCapture::Required
        );
    }
}
