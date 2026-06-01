//! Standalone bootstrap and Docker service provisioning.
//!
//! The bundled service assets mirror the Python daemon package layout. Runtime
//! callers can copy them into `~/.gobby/services` and start the same profiles
//! the daemon manages, then persist daemon-style bootstrap keys in `gcore.yaml`.

use std::collections::BTreeMap;
use std::fs;
use std::io::{Read as _, Write as _};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use anyhow::Context as _;
use serde::Deserialize;

use crate::config::{ConfigSource, embedding_keys, resolve_env_pattern};
use crate::degradation::CoreError;

pub const GCORE_CONFIG_FILENAME: &str = "gcore.yaml";
pub const SERVICES_DIRNAME: &str = "services";
pub const COMPOSE_FILENAME: &str = "docker-compose.yml";

pub const DEFAULT_POSTGRES_HOST: &str = "127.0.0.1";
pub const DEFAULT_POSTGRES_PORT: u16 = 60891;
pub const DEFAULT_POSTGRES_DB: &str = "gobby";
pub const DEFAULT_POSTGRES_USER: &str = "gobby";
pub const DEFAULT_POSTGRES_PASSWORD: &str = "gobby_dev";

pub const DEFAULT_FALKORDB_HOST: &str = "127.0.0.1";
pub const DEFAULT_FALKORDB_PORT: u16 = 16379;
pub const DEFAULT_FALKORDB_BROWSER_PORT: u16 = 13000;
pub const DEFAULT_FALKORDB_PASSWORD: &str = "gobbyfalkor";

pub const DEFAULT_QDRANT_HTTP_PORT: u16 = 6333;
pub const DEFAULT_QDRANT_GRPC_PORT: u16 = 6334;

pub const DEFAULT_LM_STUDIO_API_BASE: &str = "http://localhost:1234/v1";
pub const DEFAULT_LM_STUDIO_MODEL: &str = "text-embedding-nomic-embed-text-v1.5@f16";
pub const DEFAULT_OLLAMA_API_BASE: &str = "http://localhost:11434/v1";
pub const DEFAULT_OLLAMA_MODEL: &str = "nomic-embed-text";
pub const DEFAULT_EMBEDDING_VECTOR_DIM: usize = 768;

pub const COMPOSE_TEMPLATE: &str = include_str!("../assets/docker-compose.services.yml");
const PGSEARCH_DOCKERFILE: &str = include_str!("../assets/postgres-pgsearch/Dockerfile");
const PGSEARCH_VERSION: &str = include_str!("../assets/postgres-pgsearch/version.json");
const PGSEARCH_INIT_PG_SEARCH: &str =
    include_str!("../assets/postgres-pgsearch/initdb.d/01-pg_search.sql");
const PGSEARCH_INIT_PGAUDIT: &str =
    include_str!("../assets/postgres-pgsearch/initdb.d/02-pgaudit.sql");
const PG_AUDIT_EXPORT: &str =
    include_str!("../assets/postgres-pgsearch/scripts/pg_audit_export.sh");

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct StandaloneConfig {
    values: BTreeMap<String, String>,
}

impl StandaloneConfig {
    pub fn new(values: BTreeMap<String, String>) -> Self {
        Self { values }
    }

    pub fn empty() -> Self {
        Self::default()
    }

    pub fn read_at(path: &Path) -> anyhow::Result<Option<Self>> {
        if !path.exists() {
            return Ok(None);
        }
        let contents = fs::read_to_string(path)
            .map_err(|err| anyhow::anyhow!("failed to read {}: {err}", path.display()))?;
        Self::from_yaml_str(&contents)
            .map(Some)
            .map_err(|err| anyhow::anyhow!("failed to parse {}: {err}", path.display()))
    }

    pub fn from_yaml_str(contents: &str) -> anyhow::Result<Self> {
        if contents.trim().is_empty() {
            return Ok(Self::default());
        }
        let yaml: serde_yaml::Value = serde_yaml::from_str(contents)?;
        let mut values = BTreeMap::new();
        flatten_yaml_value(None, &yaml, &mut values)?;
        Ok(Self { values })
    }

    pub fn write_at(&self, path: &Path) -> anyhow::Result<()> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut mapping = serde_yaml::Mapping::new();
        for (key, value) in &self.values {
            mapping.insert(
                serde_yaml::Value::String(key.clone()),
                serde_yaml::Value::String(value.clone()),
            );
        }
        let yaml = serde_yaml::to_string(&serde_yaml::Value::Mapping(mapping))?;
        fs::write(path, yaml)?;
        Ok(())
    }

    pub fn get(&self, key: &str) -> Option<&str> {
        self.values.get(key).map(String::as_str)
    }

    pub fn set(&mut self, key: impl Into<String>, value: impl Into<String>) {
        self.values.insert(key.into(), value.into());
    }

    pub fn remove(&mut self, key: &str) {
        self.values.remove(key);
    }

    pub fn values(&self) -> &BTreeMap<String, String> {
        &self.values
    }
}

impl ConfigSource for StandaloneConfig {
    fn config_value(&mut self, key: &str) -> Option<String> {
        self.values.get(key).cloned().or_else(|| match key {
            "databases.falkordb.requirepass" => {
                self.values.get("databases.falkordb.password").cloned()
            }
            _ => None,
        })
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        if value.contains("$secret:") {
            anyhow::bail!("secret resolution requires daemon config_store");
        }
        resolve_env_pattern(value)?.ok_or_else(|| anyhow::anyhow!("unresolved pattern: {value}"))
    }
}

pub fn gcore_config_path(gobby_home: &Path) -> PathBuf {
    gobby_home.join(GCORE_CONFIG_FILENAME)
}

pub fn services_dir(gobby_home: &Path) -> PathBuf {
    gobby_home.join(SERVICES_DIRNAME)
}

pub fn compose_file_path(gobby_home: &Path) -> PathBuf {
    services_dir(gobby_home).join(COMPOSE_FILENAME)
}

pub fn default_database_url(port: u16) -> String {
    format!(
        "postgresql://{user}:{password}@localhost:{port}/{db}",
        user = DEFAULT_POSTGRES_USER,
        password = DEFAULT_POSTGRES_PASSWORD,
        db = DEFAULT_POSTGRES_DB
    )
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EnsureHubOptions {
    pub gobby_home: PathBuf,
    pub service_options: DockerServiceOptions,
    pub candidate_database_urls: Vec<String>,
    pub provision_services: bool,
}

impl EnsureHubOptions {
    pub fn new(gobby_home: PathBuf) -> Self {
        Self {
            service_options: DockerServiceOptions::new(gobby_home.clone()),
            gobby_home,
            candidate_database_urls: Vec::new(),
            provision_services: true,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DockerServiceOptions {
    pub gobby_home: PathBuf,
    pub postgres_port: u16,
    pub qdrant_http_port: u16,
    pub qdrant_grpc_port: u16,
    pub falkordb_host: String,
    pub falkordb_port: u16,
    pub falkordb_browser_port: u16,
    pub falkordb_password: String,
}

impl DockerServiceOptions {
    pub fn new(gobby_home: PathBuf) -> Self {
        Self {
            gobby_home,
            postgres_port: DEFAULT_POSTGRES_PORT,
            qdrant_http_port: DEFAULT_QDRANT_HTTP_PORT,
            qdrant_grpc_port: DEFAULT_QDRANT_GRPC_PORT,
            falkordb_host: DEFAULT_FALKORDB_HOST.to_string(),
            falkordb_port: DEFAULT_FALKORDB_PORT,
            falkordb_browser_port: DEFAULT_FALKORDB_BROWSER_PORT,
            falkordb_password: DEFAULT_FALKORDB_PASSWORD.to_string(),
        }
    }

    pub fn database_url(&self) -> String {
        default_database_url(self.postgres_port)
    }

    pub fn qdrant_url(&self) -> String {
        format!("http://localhost:{}", self.qdrant_http_port)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServiceAssetReport {
    pub services_dir: PathBuf,
    pub compose_file: PathBuf,
    pub env_file: PathBuf,
    pub postgres_asset_dir: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DockerProvisioningReport {
    pub services_dir: PathBuf,
    pub compose_file: PathBuf,
    pub env_file: PathBuf,
    pub started_profiles: Vec<String>,
    pub health_checks: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandSpec {
    pub program: String,
    pub args: Vec<String>,
    pub env: BTreeMap<String, String>,
    pub cwd: Option<PathBuf>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandOutput {
    pub status: i32,
    pub stdout: String,
    pub stderr: String,
}

pub trait CommandRunner {
    fn run(&mut self, spec: &CommandSpec) -> std::io::Result<CommandOutput>;
}

pub struct RealCommandRunner;

impl CommandRunner for RealCommandRunner {
    fn run(&mut self, spec: &CommandSpec) -> std::io::Result<CommandOutput> {
        let mut command = Command::new(&spec.program);
        command.args(&spec.args);
        if let Some(cwd) = &spec.cwd {
            command.current_dir(cwd);
        }
        for (key, value) in &spec.env {
            command.env(key, value);
        }
        let output = command.output()?;
        Ok(CommandOutput {
            status: output.status.code().unwrap_or(1),
            stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        })
    }
}

pub trait DockerHealthChecker {
    fn wait_postgres(&mut self, host: &str, port: u16) -> anyhow::Result<()>;
    fn wait_qdrant(&mut self, host: &str, port: u16) -> anyhow::Result<()>;
    fn wait_falkordb(&mut self, host: &str, port: u16) -> anyhow::Result<()>;
}

pub struct TcpDockerHealthChecker {
    pub retries: usize,
    pub interval: Duration,
}

impl Default for TcpDockerHealthChecker {
    fn default() -> Self {
        Self {
            retries: 30,
            interval: Duration::from_secs(2),
        }
    }
}

impl DockerHealthChecker for TcpDockerHealthChecker {
    fn wait_postgres(&mut self, host: &str, port: u16) -> anyhow::Result<()> {
        wait_for_tcp(host, port, self.retries, self.interval)
            .map_err(|err| anyhow::anyhow!("PostgreSQL did not become reachable: {err}"))
    }

    fn wait_qdrant(&mut self, host: &str, port: u16) -> anyhow::Result<()> {
        let healthz = || -> anyhow::Result<()> {
            let mut stream = TcpStream::connect((host, port))?;
            stream.set_read_timeout(Some(Duration::from_secs(3)))?;
            stream.set_write_timeout(Some(Duration::from_secs(3)))?;
            stream.write_all(b"GET /healthz HTTP/1.0\r\nHost: localhost\r\n\r\n")?;
            let mut body = String::new();
            stream.read_to_string(&mut body)?;
            if body.starts_with("HTTP/1.1 200") || body.starts_with("HTTP/1.0 200") {
                Ok(())
            } else {
                anyhow::bail!("unexpected Qdrant health response")
            }
        };
        wait_for(healthz, self.retries, self.interval)
            .map_err(|err| anyhow::anyhow!("Qdrant did not become healthy: {err}"))
    }

    fn wait_falkordb(&mut self, host: &str, port: u16) -> anyhow::Result<()> {
        wait_for_tcp(host, port, self.retries, self.interval)
            .map_err(|err| anyhow::anyhow!("FalkorDB did not become reachable: {err}"))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HubIdentity {
    pub system_identifier: String,
    pub database_name: String,
}

impl HubIdentity {
    fn display_label(&self) -> String {
        format!(
            "system_identifier={}, database={}",
            self.system_identifier, self.database_name
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HubIdentityProbeResult {
    Known(HubIdentity),
    UnknownInsufficientPrivilege { message: String },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecordedHubIdentityStatus {
    SingleReachable,
    VerifiedSameHub,
    IdentityUnknownInsufficientPrivilege { message: String },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecordedHubResolution {
    pub database_url: String,
    pub identity_status: RecordedHubIdentityStatus,
}

pub fn ensure_hub(
    options: &EnsureHubOptions,
) -> anyhow::Result<(String, Option<DockerProvisioningReport>)> {
    ensure_hub_with_identity(
        options,
        |name| std::env::var(name).ok(),
        postgres_database_reachable,
        probe_postgres_hub_identity,
        provision_docker_services,
    )
}

#[cfg(test)]
fn ensure_hub_with(
    options: &EnsureHubOptions,
    get_env: impl FnMut(&str) -> Option<String>,
    database_reachable: impl FnMut(&str) -> bool,
    provision: impl FnOnce(&DockerServiceOptions) -> anyhow::Result<DockerProvisioningReport>,
) -> anyhow::Result<(String, Option<DockerProvisioningReport>)> {
    ensure_hub_with_identity(
        options,
        get_env,
        database_reachable,
        |_| {
            Ok(HubIdentityProbeResult::UnknownInsufficientPrivilege {
                message: "identity_unknown_insufficient_privilege: test probe not configured"
                    .to_string(),
            })
        },
        provision,
    )
}

fn ensure_hub_with_identity(
    options: &EnsureHubOptions,
    mut get_env: impl FnMut(&str) -> Option<String>,
    mut database_reachable: impl FnMut(&str) -> bool,
    mut identity_probe: impl FnMut(&str) -> anyhow::Result<HubIdentityProbeResult>,
    provision: impl FnOnce(&DockerServiceOptions) -> anyhow::Result<DockerProvisioningReport>,
) -> anyhow::Result<(String, Option<DockerProvisioningReport>)> {
    let mut gcore_database_url = None;
    let mut bootstrap_database_url = None;

    for candidate in resolve_hub_database_urls(options, &mut get_env)? {
        match candidate.source {
            HubDatabaseUrlSource::Candidate | HubDatabaseUrlSource::Env => {
                if database_reachable(&candidate.database_url) {
                    return Ok((candidate.database_url, None));
                }
            }
            HubDatabaseUrlSource::GcoreConfig => {
                gcore_database_url = Some(candidate.database_url);
            }
            HubDatabaseUrlSource::Bootstrap => {
                bootstrap_database_url = Some(candidate.database_url);
            }
        }
    }

    if let Some(resolution) = resolve_recorded_hub_database_url(
        gcore_database_url.as_deref(),
        bootstrap_database_url.as_deref(),
        &mut database_reachable,
        &mut identity_probe,
    )? {
        if let RecordedHubIdentityStatus::IdentityUnknownInsufficientPrivilege { message } =
            &resolution.identity_status
        {
            log::warn!("{message}");
        }
        return Ok((resolution.database_url, None));
    }

    if !options.provision_services {
        anyhow::bail!(
            "no reachable Gobby PostgreSQL hub found and service provisioning is disabled"
        );
    }

    let report = provision(&options.service_options).context("failed to provision Gobby hub")?;
    Ok((
        default_database_url(options.service_options.postgres_port),
        Some(report),
    ))
}

pub fn resolve_recorded_hub_database_url(
    existing_database_url: Option<&str>,
    daemon_database_url: Option<&str>,
    mut database_reachable: impl FnMut(&str) -> bool,
    mut identity_probe: impl FnMut(&str) -> anyhow::Result<HubIdentityProbeResult>,
) -> anyhow::Result<Option<RecordedHubResolution>> {
    let existing_database_url =
        existing_database_url.and_then(|value| non_empty_trimmed(Some(value.to_string())));
    let daemon_database_url =
        daemon_database_url.and_then(|value| non_empty_trimmed(Some(value.to_string())));

    match (existing_database_url, daemon_database_url) {
        (None, None) => Ok(None),
        (Some(existing), None) => {
            if database_reachable(&existing) {
                Ok(Some(RecordedHubResolution {
                    database_url: existing,
                    identity_status: RecordedHubIdentityStatus::SingleReachable,
                }))
            } else {
                Ok(None)
            }
        }
        (None, Some(daemon)) => {
            if database_reachable(&daemon) {
                Ok(Some(RecordedHubResolution {
                    database_url: daemon,
                    identity_status: RecordedHubIdentityStatus::SingleReachable,
                }))
            } else {
                Ok(None)
            }
        }
        (Some(existing), Some(daemon)) if existing == daemon => {
            if database_reachable(&daemon) {
                Ok(Some(RecordedHubResolution {
                    database_url: daemon,
                    identity_status: RecordedHubIdentityStatus::VerifiedSameHub,
                }))
            } else {
                Ok(None)
            }
        }
        (Some(existing), Some(daemon)) => {
            let existing_reachable = database_reachable(&existing);
            let daemon_reachable = database_reachable(&daemon);

            match (existing_reachable, daemon_reachable) {
                (false, false) => Ok(None),
                (true, false) => Ok(Some(RecordedHubResolution {
                    database_url: existing,
                    identity_status: RecordedHubIdentityStatus::SingleReachable,
                })),
                (false, true) => Ok(Some(RecordedHubResolution {
                    database_url: daemon,
                    identity_status: RecordedHubIdentityStatus::SingleReachable,
                })),
                (true, true) => {
                    let existing_redacted = redact_database_url_for_error(&existing);
                    let daemon_redacted = redact_database_url_for_error(&daemon);
                    let existing_identity = identity_probe(&existing).with_context(|| {
                        format!("failed to probe PostgreSQL hub identity for {existing_redacted}")
                    })?;
                    let daemon_identity = identity_probe(&daemon).with_context(|| {
                        format!("failed to probe PostgreSQL hub identity for {daemon_redacted}")
                    })?;

                    match (existing_identity, daemon_identity) {
                        (
                            HubIdentityProbeResult::Known(existing_identity),
                            HubIdentityProbeResult::Known(daemon_identity),
                        ) if existing_identity == daemon_identity => {
                            Ok(Some(RecordedHubResolution {
                                database_url: daemon,
                                identity_status: RecordedHubIdentityStatus::VerifiedSameHub,
                            }))
                        }
                        (
                            HubIdentityProbeResult::Known(existing_identity),
                            HubIdentityProbeResult::Known(daemon_identity),
                        ) => Err(CoreError::HubConflict {
                            existing_database_url: existing_redacted,
                            existing_identity: existing_identity.display_label(),
                            daemon_database_url: daemon_redacted,
                            daemon_identity: daemon_identity.display_label(),
                        }
                        .into()),
                        (
                            HubIdentityProbeResult::UnknownInsufficientPrivilege { message },
                            _,
                        )
                        | (
                            _,
                            HubIdentityProbeResult::UnknownInsufficientPrivilege { message },
                        ) => Ok(Some(RecordedHubResolution {
                            database_url: existing.clone(),
                            identity_status:
                                RecordedHubIdentityStatus::IdentityUnknownInsufficientPrivilege {
                                    message: format!(
                                        "identity_unknown_insufficient_privilege: preserving existing recorded hub {existing}; daemon hub {daemon} was not adopted because identity could not be verified ({message})"
                                    ),
                                },
                        })),
                    }
                }
            }
        }
    }
}

fn redact_database_url_for_error(database_url: &str) -> String {
    let without_fragment = database_url
        .split_once('#')
        .map_or(database_url, |(head, _)| head);
    let without_query = without_fragment
        .split_once('?')
        .map_or(without_fragment, |(head, _)| head);
    if let Some((scheme, rest)) = without_query.split_once("://") {
        let host_and_path = rest
            .rsplit_once('@')
            .map_or(rest, |(_, host_and_path)| host_and_path);
        format!("{scheme}://{host_and_path}")
    } else {
        without_query.to_string()
    }
}

#[cfg(feature = "postgres")]
pub fn probe_postgres_hub_identity(database_url: &str) -> anyhow::Result<HubIdentityProbeResult> {
    use anyhow::Context;
    use postgres::error::SqlState;

    fn insufficient_privilege(error: &postgres::Error) -> bool {
        error.code() == Some(&SqlState::INSUFFICIENT_PRIVILEGE)
    }

    let mut conn = crate::postgres::connect_readonly(database_url)?;
    let has_privilege = match conn.query_one(
        "SELECT has_function_privilege(current_user, 'pg_control_system()', 'execute')",
        &[],
    ) {
        Ok(row) => row.get::<_, bool>(0),
        Err(error) if insufficient_privilege(&error) => {
            return Ok(HubIdentityProbeResult::UnknownInsufficientPrivilege {
                message: "identity_unknown_insufficient_privilege: current role cannot preflight pg_control_system()".to_string(),
            });
        }
        Err(error) => {
            return Err(error).context("failed to preflight pg_control_system() privilege");
        }
    };

    if !has_privilege {
        return Ok(HubIdentityProbeResult::UnknownInsufficientPrivilege {
            message: "identity_unknown_insufficient_privilege: current role cannot execute pg_control_system()".to_string(),
        });
    }

    let row = match conn.query_one(
        "SELECT system_identifier::text AS system_identifier, current_database() AS database_name FROM pg_control_system()",
        &[],
    ) {
        Ok(row) => row,
        Err(error) if insufficient_privilege(&error) => {
            return Ok(HubIdentityProbeResult::UnknownInsufficientPrivilege {
                message: "identity_unknown_insufficient_privilege: current role cannot execute pg_control_system()".to_string(),
            });
        }
        Err(error) => return Err(error).context("failed to query PostgreSQL hub identity"),
    };

    Ok(HubIdentityProbeResult::Known(HubIdentity {
        system_identifier: row
            .try_get("system_identifier")
            .context("PostgreSQL hub identity did not include system_identifier")?,
        database_name: row
            .try_get("database_name")
            .context("PostgreSQL hub identity did not include database_name")?,
    }))
}

#[cfg(not(feature = "postgres"))]
pub fn probe_postgres_hub_identity(_database_url: &str) -> anyhow::Result<HubIdentityProbeResult> {
    Ok(HubIdentityProbeResult::UnknownInsufficientPrivilege {
        message: "identity_unknown_insufficient_privilege: gobby-core was built without PostgreSQL support".to_string(),
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum HubDatabaseUrlSource {
    Candidate,
    Env,
    GcoreConfig,
    Bootstrap,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct HubDatabaseUrl {
    source: HubDatabaseUrlSource,
    database_url: String,
}

fn resolve_hub_database_urls(
    options: &EnsureHubOptions,
    get_env: &mut impl FnMut(&str) -> Option<String>,
) -> anyhow::Result<Vec<HubDatabaseUrl>> {
    let mut urls = Vec::new();
    urls.extend(
        options
            .candidate_database_urls
            .iter()
            .filter_map(|value| non_empty_trimmed(Some(value.clone())))
            .map(|database_url| HubDatabaseUrl {
                source: HubDatabaseUrlSource::Candidate,
                database_url,
            }),
    );
    if let Some(database_url) = non_empty_trimmed(get_env("GOBBY_POSTGRES_DSN")) {
        urls.push(HubDatabaseUrl {
            source: HubDatabaseUrlSource::Env,
            database_url,
        });
    }
    if let Some(database_url) = resolve_database_url_from_gcore_config(&options.gobby_home)? {
        urls.push(HubDatabaseUrl {
            source: HubDatabaseUrlSource::GcoreConfig,
            database_url,
        });
    }
    if let Some(database_url) =
        resolve_database_url_from_bootstrap_file(&options.gobby_home.join("bootstrap.yaml"))?
    {
        urls.push(HubDatabaseUrl {
            source: HubDatabaseUrlSource::Bootstrap,
            database_url,
        });
    }
    Ok(urls)
}

fn resolve_database_url_from_gcore_config(home: &Path) -> anyhow::Result<Option<String>> {
    let Some(config) = StandaloneConfig::read_at(&gcore_config_path(home))? else {
        return Ok(None);
    };
    Ok(config
        .get("databases.postgres.dsn")
        .and_then(|value| non_empty_trimmed(Some(value.to_string()))))
}

#[derive(Debug, Deserialize)]
struct HubBootstrap {
    hub_backend: Option<String>,
    database_url: Option<String>,
}

fn resolve_database_url_from_bootstrap_file(path: &Path) -> anyhow::Result<Option<String>> {
    if !path.exists() {
        return Ok(None);
    }
    let contents = fs::read_to_string(path)
        .with_context(|| format!("failed to read Gobby bootstrap at {}", path.display()))?;
    let bootstrap: HubBootstrap = serde_yaml::from_str(&contents)
        .with_context(|| format!("failed to parse {}", path.display()))?;
    if matches!(bootstrap.hub_backend.as_deref(), Some(backend) if backend != "postgres") {
        return Ok(None);
    }
    Ok(non_empty_trimmed(bootstrap.database_url))
}

fn non_empty_trimmed(value: Option<String>) -> Option<String> {
    let trimmed = value.as_ref()?.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

#[cfg(feature = "postgres")]
fn postgres_database_reachable(database_url: &str) -> bool {
    crate::postgres::connect_readonly(database_url).is_ok()
}

#[cfg(not(feature = "postgres"))]
fn postgres_database_reachable(_database_url: &str) -> bool {
    false
}

pub fn provision_docker_services(
    options: &DockerServiceOptions,
) -> anyhow::Result<DockerProvisioningReport> {
    let mut runner = RealCommandRunner;
    let mut health = TcpDockerHealthChecker::default();
    provision_docker_services_with(options, &mut runner, &mut health)
}

pub fn provision_docker_services_with(
    options: &DockerServiceOptions,
    runner: &mut impl CommandRunner,
    health: &mut impl DockerHealthChecker,
) -> anyhow::Result<DockerProvisioningReport> {
    let assets = prepare_service_assets(options)?;
    let spec = docker_compose_up_spec(options, &assets.compose_file, &assets.services_dir);
    let output = runner.run(&spec).map_err(|err| {
        anyhow::anyhow!("failed to execute docker compose for standalone services: {err}")
    })?;
    if output.status != 0 {
        anyhow::bail!(
            "docker compose up failed: {}",
            first_non_empty(&output.stderr, &output.stdout)
        );
    }

    health.wait_postgres(DEFAULT_POSTGRES_HOST, options.postgres_port)?;
    health.wait_qdrant(DEFAULT_POSTGRES_HOST, options.qdrant_http_port)?;
    health.wait_falkordb(&options.falkordb_host, options.falkordb_port)?;

    Ok(DockerProvisioningReport {
        services_dir: assets.services_dir,
        compose_file: assets.compose_file,
        env_file: assets.env_file,
        started_profiles: vec!["all".to_string()],
        health_checks: vec![
            "postgres".to_string(),
            "qdrant".to_string(),
            "falkordb".to_string(),
        ],
    })
}

pub fn prepare_service_assets(
    options: &DockerServiceOptions,
) -> anyhow::Result<ServiceAssetReport> {
    let services = services_dir(&options.gobby_home);
    let compose = services.join(COMPOSE_FILENAME);
    let pgsearch = services.join("postgres-pgsearch");
    let env_file = services.join(".env");

    fs::create_dir_all(pgsearch.join("initdb.d"))?;
    fs::create_dir_all(pgsearch.join("scripts"))?;
    fs::write(&compose, COMPOSE_TEMPLATE)?;
    fs::write(pgsearch.join("Dockerfile"), PGSEARCH_DOCKERFILE)?;
    fs::write(pgsearch.join("version.json"), PGSEARCH_VERSION)?;
    fs::write(
        pgsearch.join("initdb.d").join("01-pg_search.sql"),
        PGSEARCH_INIT_PG_SEARCH,
    )?;
    fs::write(
        pgsearch.join("initdb.d").join("02-pgaudit.sql"),
        PGSEARCH_INIT_PGAUDIT,
    )?;
    let audit_script = pgsearch.join("scripts").join("pg_audit_export.sh");
    fs::write(&audit_script, PG_AUDIT_EXPORT)?;
    make_executable(&audit_script)?;

    let manifest = pgsearch_manifest()?;
    update_env_file(
        &env_file,
        BTreeMap::from([
            (
                "GOBBY_PG_SEARCH_VERSION".to_string(),
                manifest.pg_search_version,
            ),
            ("GOBBY_PG_SEARCH_SHA256".to_string(), manifest.sha256),
            (
                "GOBBY_POSTGRES_PORT".to_string(),
                options.postgres_port.to_string(),
            ),
            (
                "GOBBY_POSTGRES_DB".to_string(),
                DEFAULT_POSTGRES_DB.to_string(),
            ),
            (
                "GOBBY_POSTGRES_USER".to_string(),
                DEFAULT_POSTGRES_USER.to_string(),
            ),
            (
                "GOBBY_POSTGRES_PASSWORD".to_string(),
                DEFAULT_POSTGRES_PASSWORD.to_string(),
            ),
            (
                "GOBBY_QDRANT_HTTP_PORT".to_string(),
                options.qdrant_http_port.to_string(),
            ),
            (
                "GOBBY_QDRANT_GRPC_PORT".to_string(),
                options.qdrant_grpc_port.to_string(),
            ),
            (
                "GOBBY_FALKORDB_PORT".to_string(),
                options.falkordb_port.to_string(),
            ),
            (
                "GOBBY_FALKORDB_BROWSER_PORT".to_string(),
                options.falkordb_browser_port.to_string(),
            ),
            (
                "GOBBY_FALKORDB_PASSWORD".to_string(),
                options.falkordb_password.clone(),
            ),
        ]),
    )?;

    Ok(ServiceAssetReport {
        services_dir: services,
        compose_file: compose,
        env_file,
        postgres_asset_dir: pgsearch,
    })
}

pub fn docker_compose_up_spec(
    options: &DockerServiceOptions,
    compose_file: &Path,
    services_dir: &Path,
) -> CommandSpec {
    CommandSpec {
        program: "docker".to_string(),
        args: vec![
            "compose".to_string(),
            "-f".to_string(),
            compose_file.display().to_string(),
            "--profile".to_string(),
            "all".to_string(),
            "up".to_string(),
            "-d".to_string(),
            "--remove-orphans".to_string(),
        ],
        env: BTreeMap::from([
            (
                "GOBBY_FALKORDB_PASSWORD".to_string(),
                options.falkordb_password.clone(),
            ),
            (
                "GOBBY_POSTGRES_PORT".to_string(),
                options.postgres_port.to_string(),
            ),
            (
                "GOBBY_QDRANT_HTTP_PORT".to_string(),
                options.qdrant_http_port.to_string(),
            ),
        ]),
        cwd: Some(services_dir.to_path_buf()),
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EmbeddingBootstrap {
    pub provider: String,
    pub api_base: String,
    pub model: String,
    pub vector_dim: usize,
    pub query_prefix: Option<String>,
    pub api_key: Option<String>,
}

impl EmbeddingBootstrap {
    pub fn lm_studio() -> Self {
        Self {
            provider: "lm-studio".to_string(),
            api_base: DEFAULT_LM_STUDIO_API_BASE.to_string(),
            model: DEFAULT_LM_STUDIO_MODEL.to_string(),
            vector_dim: DEFAULT_EMBEDDING_VECTOR_DIM,
            query_prefix: None,
            api_key: None,
        }
    }

    pub fn ollama() -> Self {
        Self {
            provider: "ollama".to_string(),
            api_base: DEFAULT_OLLAMA_API_BASE.to_string(),
            model: DEFAULT_OLLAMA_MODEL.to_string(),
            vector_dim: DEFAULT_EMBEDDING_VECTOR_DIM,
            query_prefix: None,
            api_key: None,
        }
    }
}

pub fn write_standalone_bootstrap(
    path: &Path,
    database_url: &str,
    options: &DockerServiceOptions,
    compose_file: Option<&Path>,
    embedding: Option<&EmbeddingBootstrap>,
) -> anyhow::Result<StandaloneConfig> {
    let mut config = StandaloneConfig::empty();
    config.set("databases.postgres.dsn", database_url);
    config.set("databases.falkordb.host", &options.falkordb_host);
    config.set("databases.falkordb.port", options.falkordb_port.to_string());
    config.set("databases.falkordb.password", &options.falkordb_password);
    config.set("databases.qdrant.url", options.qdrant_url());
    if let Some(embedding) = embedding {
        remove_legacy_embedding_keys(&mut config);
        config.set(embedding_keys::AI_PROVIDER, &embedding.provider);
        config.set(embedding_keys::AI_API_BASE, &embedding.api_base);
        config.set(embedding_keys::AI_MODEL, &embedding.model);
        config.set(embedding_keys::AI_DIM, embedding.vector_dim.to_string());
        if let Some(query_prefix) = &embedding.query_prefix {
            config.set(embedding_keys::AI_QUERY_PREFIX, query_prefix);
        }
        if let Some(api_key) = &embedding.api_key {
            config.set(embedding_keys::AI_API_KEY, api_key);
        }
    }
    if let Some(compose_file) = compose_file {
        config.set("services.compose_file", compose_file.display().to_string());
    }
    config.write_at(path)?;
    Ok(config)
}

fn remove_legacy_embedding_keys(config: &mut StandaloneConfig) {
    for key in embedding_keys::legacy_keys() {
        config.remove(&key);
    }
}

fn flatten_yaml_value(
    prefix: Option<&str>,
    value: &serde_yaml::Value,
    output: &mut BTreeMap<String, String>,
) -> anyhow::Result<()> {
    match value {
        serde_yaml::Value::Null => Ok(()),
        serde_yaml::Value::Mapping(mapping) => {
            for (key, value) in mapping {
                let Some(key) = key.as_str() else {
                    anyhow::bail!("gcore.yaml keys must be strings");
                };
                let joined = match prefix {
                    Some(prefix) if !prefix.is_empty() => format!("{prefix}.{key}"),
                    _ => key.to_string(),
                };
                match value {
                    serde_yaml::Value::Mapping(_) if !key.contains('.') => {
                        flatten_yaml_value(Some(&joined), value, output)?;
                    }
                    _ => {
                        if let Some(text) = scalar_to_string(value)? {
                            output.insert(joined, text);
                        }
                    }
                }
            }
            Ok(())
        }
        _ => {
            let Some(prefix) = prefix else {
                anyhow::bail!("gcore.yaml must be a mapping");
            };
            if let Some(text) = scalar_to_string(value)? {
                output.insert(prefix.to_string(), text);
            }
            Ok(())
        }
    }
}

fn scalar_to_string(value: &serde_yaml::Value) -> anyhow::Result<Option<String>> {
    Ok(match value {
        serde_yaml::Value::Null => None,
        serde_yaml::Value::String(value) => Some(value.clone()),
        serde_yaml::Value::Bool(value) => Some(value.to_string()),
        serde_yaml::Value::Number(value) => Some(value.to_string()),
        other => Some(serde_yaml::to_string(other)?.trim().to_string()),
    })
}

#[derive(Debug, Deserialize)]
struct PgSearchVersionFile {
    pg_search_version: String,
    pg_search_sha256: String,
    pg_search_sha256_by_arch: Option<BTreeMap<String, String>>,
}

struct PgSearchManifest {
    pg_search_version: String,
    sha256: String,
}

fn pgsearch_manifest() -> anyhow::Result<PgSearchManifest> {
    let parsed: PgSearchVersionFile = serde_json::from_str(PGSEARCH_VERSION)?;
    let arch = debian_arch(std::env::consts::ARCH);
    let sha256 = parsed
        .pg_search_sha256_by_arch
        .and_then(|by_arch| by_arch.get(&arch).cloned())
        .unwrap_or(parsed.pg_search_sha256);
    Ok(PgSearchManifest {
        pg_search_version: parsed.pg_search_version,
        sha256,
    })
}

fn debian_arch(arch: &str) -> String {
    match arch {
        "x86_64" | "amd64" => "amd64".to_string(),
        "aarch64" | "arm64" => "arm64".to_string(),
        other => other.to_string(),
    }
}

fn update_env_file(path: &Path, updates: BTreeMap<String, String>) -> anyhow::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut lines = Vec::new();
    if path.exists() {
        for line in fs::read_to_string(path)?.lines() {
            let key = line.split_once('=').map(|(key, _)| key).unwrap_or(line);
            if !updates.contains_key(key) {
                lines.push(line.to_string());
            }
        }
        if lines.last().is_some_and(|line| !line.trim().is_empty()) {
            lines.push(String::new());
        }
    }
    for (key, value) in updates {
        lines.push(format!("{key}={value}"));
    }
    fs::write(path, format!("{}\n", lines.join("\n")))?;
    Ok(())
}

fn first_non_empty<'a>(first: &'a str, second: &'a str) -> &'a str {
    if first.trim().is_empty() {
        second.trim()
    } else {
        first.trim()
    }
}

fn wait_for_tcp(host: &str, port: u16, retries: usize, interval: Duration) -> anyhow::Result<()> {
    wait_for(
        || {
            TcpStream::connect((host, port))
                .map(|_| ())
                .map_err(Into::into)
        },
        retries,
        interval,
    )
}

fn wait_for(
    mut check: impl FnMut() -> anyhow::Result<()>,
    retries: usize,
    interval: Duration,
) -> anyhow::Result<()> {
    let mut last_error = None;
    for attempt in 0..retries {
        match check() {
            Ok(()) => return Ok(()),
            Err(err) => last_error = Some(err),
        }
        if attempt + 1 < retries {
            std::thread::sleep(interval);
        }
    }
    Err(last_error.unwrap_or_else(|| anyhow::anyhow!("health check failed")))
}

fn make_executable(path: &Path) -> anyhow::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut permissions = fs::metadata(path)?.permissions();
        permissions.set_mode(0o755);
        fs::set_permissions(path, permissions)?;
    }
    #[cfg(not(unix))]
    {
        let _ = path;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::TEST_ENV_LOCK;
    use std::sync::MutexGuard;

    struct EnvGuard {
        _lock: MutexGuard<'static, ()>,
    }

    impl EnvGuard {
        fn new() -> Self {
            let guard = Self {
                _lock: TEST_ENV_LOCK
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner()),
            };
            guard.clear();
            guard
        }

        fn clear(&self) {
            for key in [
                "GOBBY_FALKORDB_HOST",
                "GOBBY_FALKORDB_PORT",
                "GOBBY_FALKORDB_PASSWORD",
                "GOBBY_POSTGRES_DSN",
                "GOBBY_QDRANT_URL",
                "GOBBY_QDRANT_API_KEY",
                "GOBBY_EMBEDDING_URL",
                "GOBBY_EMBEDDING_MODEL",
                "GOBBY_EMBEDDING_API_KEY",
                "GOBBY_EMBEDDING_QUERY_PREFIX",
                "GOBBY_EMBEDDING_TIMEOUT_SECONDS",
            ] {
                // SAFETY: TEST_ENV_LOCK serializes all test environment mutation
                // here, and the loop only touches the fixed key list above.
                unsafe { std::env::remove_var(key) };
            }
        }
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            self.clear();
        }
    }

    #[test]
    fn gcore_yaml_reads_flat_and_nested_keys() {
        let config = StandaloneConfig::from_yaml_str(&format!(
            r#"
databases.postgres.dsn: postgresql://flat/db
databases:
  falkordb:
    port: 16379
{api_key}: local-api-key
"#,
            api_key = embedding_keys::AI_API_KEY,
        ))
        .expect("parse config");

        assert_eq!(
            config.get("databases.postgres.dsn"),
            Some("postgresql://flat/db")
        );
        assert_eq!(config.get("databases.falkordb.port"), Some("16379"));
        assert_eq!(
            config.get(embedding_keys::AI_API_KEY),
            Some("local-api-key")
        );
    }

    #[test]
    fn gcore_yaml_writes_flat_keys() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join(GCORE_CONFIG_FILENAME);
        let mut config = StandaloneConfig::empty();
        config.set("databases.postgres.dsn", "postgresql://local/db");
        config.set(embedding_keys::AI_DIM, "768");

        config.write_at(&path).expect("write config");
        let raw = fs::read_to_string(&path).expect("read config");

        assert!(raw.contains("databases.postgres.dsn:"));
        assert!(raw.contains(&format!("{}:", embedding_keys::AI_DIM)));
        assert_eq!(
            StandaloneConfig::read_at(&path)
                .expect("read config")
                .expect("config present")
                .get(embedding_keys::AI_DIM),
            Some("768")
        );
    }

    #[test]
    fn standalone_config_resolves_service_keys_and_plain_api_key() {
        let _env = EnvGuard::new();
        let mut config = StandaloneConfig::from_yaml_str(&format!(
            r#"
databases.falkordb.host: 127.0.0.1
databases.falkordb.port: "16379"
databases.falkordb.password: falkor-pass
databases.qdrant.url: http://localhost:6333
{api_base}: http://localhost:1234/v1
{model}: text-embedding-nomic-embed-text-v1.5@f16
{api_key}: test-key
"#,
            api_base = embedding_keys::AI_API_BASE,
            model = embedding_keys::AI_MODEL,
            api_key = embedding_keys::AI_API_KEY,
        ))
        .expect("parse config");

        let falkor = crate::config::resolve_falkordb_config(&mut config).expect("falkor");
        assert_eq!(falkor.password.as_deref(), Some("falkor-pass"));
        let qdrant = crate::config::resolve_qdrant_config(&mut config).expect("qdrant");
        assert_eq!(qdrant.url.as_deref(), Some("http://localhost:6333"));
        let embedding = crate::config::resolve_embedding_config(&mut config).expect("embedding");
        assert_eq!(embedding.api_key.as_deref(), Some("test-key"));
    }

    #[test]
    fn writes_ai_embeddings_standalone_api_key() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join(GCORE_CONFIG_FILENAME);
        let options = DockerServiceOptions::new(dir.path().join(".gobby"));
        let embedding = EmbeddingBootstrap {
            provider: "openai-compatible".to_string(),
            api_base: "http://localhost:1234/v1".to_string(),
            model: "embed-small".to_string(),
            vector_dim: 1024,
            query_prefix: Some("query: ".to_string()),
            api_key: Some("local-api-key".to_string()),
        };

        let config = write_standalone_bootstrap(
            &path,
            "postgresql://localhost/gobby",
            &options,
            None,
            Some(&embedding),
        )
        .expect("write standalone bootstrap");

        assert_eq!(
            config.get(embedding_keys::AI_PROVIDER),
            Some("openai-compatible")
        );
        assert_eq!(
            config.get(embedding_keys::AI_API_BASE),
            Some("http://localhost:1234/v1")
        );
        assert_eq!(config.get(embedding_keys::AI_MODEL), Some("embed-small"));
        assert_eq!(config.get(embedding_keys::AI_DIM), Some("1024"));
        assert_eq!(config.get(embedding_keys::AI_QUERY_PREFIX), Some("query: "));
        assert_eq!(
            config.get(embedding_keys::AI_API_KEY),
            Some("local-api-key")
        );
        for key in embedding_keys::legacy_keys() {
            assert_eq!(config.get(&key), None, "legacy key was written: {key}");
        }
    }

    #[test]
    fn compose_template_matches_daemon_checkout_when_present() {
        let daemon =
            Path::new("/Users/josh/Projects/gobby/src/gobby/data/docker-compose.services.yml");
        if !daemon.exists() {
            return;
        }
        let daemon_template = fs::read_to_string(daemon).expect("read daemon compose template");
        assert_eq!(COMPOSE_TEMPLATE, daemon_template);
    }

    #[test]
    fn docker_provisioning_prepares_assets_runs_compose_and_health_checks() {
        let dir = tempfile::tempdir().expect("tempdir");
        let mut runner = RecordingRunner::default();
        let mut health = RecordingHealth::default();
        let options = DockerServiceOptions::new(dir.path().join(".gobby"));

        let report = provision_docker_services_with(&options, &mut runner, &mut health)
            .expect("provision services");

        assert_eq!(runner.commands.len(), 1);
        assert_eq!(runner.commands[0].program, "docker");
        assert!(runner.commands[0].args.contains(&"--profile".to_string()));
        assert!(runner.commands[0].args.contains(&"all".to_string()));
        assert_eq!(health.checks, vec!["postgres", "qdrant", "falkordb"]);
        assert_eq!(report.started_profiles, vec!["all"]);
        assert_eq!(report.health_checks, vec!["postgres", "qdrant", "falkordb"]);
        assert_eq!(
            fs::read_to_string(&report.compose_file).expect("read compose"),
            COMPOSE_TEMPLATE
        );
        assert!(
            report
                .services_dir
                .join("postgres-pgsearch")
                .join("Dockerfile")
                .exists()
        );
        assert!(
            fs::read_to_string(&report.env_file)
                .expect("read env")
                .contains("GOBBY_PG_SEARCH_VERSION=0.23.4")
        );
    }

    #[test]
    fn ensure_hub_reuses_then_provisions() {
        let _env = EnvGuard::new();
        let dir = tempfile::tempdir().expect("tempdir");
        let home = dir.path().join(".gobby");
        fs::create_dir_all(&home).expect("create gobby home");
        let mut config = StandaloneConfig::empty();
        config.set("databases.postgres.dsn", "postgresql://reachable/gobby");
        config
            .write_at(&gcore_config_path(&home))
            .expect("write gcore config");

        let options = EnsureHubOptions::new(home.clone());
        let (database_url, report) = ensure_hub_with(
            &options,
            |_| None,
            |url| url == "postgresql://reachable/gobby",
            |_| panic!("reachable DSN should not provision services"),
        )
        .expect("reuse reachable DSN");
        assert_eq!(database_url, "postgresql://reachable/gobby");
        assert!(report.is_none());

        let mut options = EnsureHubOptions::new(home);
        options.service_options.postgres_port = 15432;
        options.candidate_database_urls = vec!["postgresql://unreachable/gobby".to_string()];
        let (database_url, report) = ensure_hub_with(
            &options,
            |_| None,
            |_| false,
            |service_options| {
                Ok(DockerProvisioningReport {
                    services_dir: service_options.gobby_home.join("services"),
                    compose_file: compose_file_path(&service_options.gobby_home),
                    env_file: service_options.gobby_home.join("services/.env"),
                    started_profiles: vec!["all".to_string()],
                    health_checks: vec!["postgres".to_string()],
                })
            },
        )
        .expect("provision fallback hub");
        assert_eq!(database_url, default_database_url(15432));
        assert_eq!(
            report.expect("provisioning report").health_checks,
            vec!["postgres"]
        );
    }

    #[test]
    fn no_double_provision_when_reachable() {
        let _env = EnvGuard::new();
        let dir = tempfile::tempdir().expect("tempdir");
        let home = dir.path().join(".gobby");
        fs::create_dir_all(&home).expect("create gobby home");
        let mut config = StandaloneConfig::empty();
        config.set("databases.postgres.dsn", "postgresql://recorded/gobby");
        config
            .write_at(&gcore_config_path(&home))
            .expect("write gcore config");

        let mut options = EnsureHubOptions::new(home);
        options.service_options.postgres_port = 15432;
        let (database_url, report) = ensure_hub_with_identity(
            &options,
            |_| None,
            |url| url == "postgresql://recorded/gobby",
            |_| {
                Ok(HubIdentityProbeResult::Known(HubIdentity {
                    system_identifier: "cluster-a".to_string(),
                    database_name: "gobby".to_string(),
                }))
            },
            |_| panic!("reachable recorded DSN should not provision services"),
        )
        .expect("reuse reachable recorded hub");

        assert_eq!(database_url, "postgresql://recorded/gobby");
        assert!(report.is_none());
    }

    #[test]
    fn divergent_hubs_surface_conflict() {
        let _env = EnvGuard::new();
        let dir = tempfile::tempdir().expect("tempdir");
        let home = dir.path().join(".gobby");
        fs::create_dir_all(&home).expect("create gobby home");
        let mut config = StandaloneConfig::empty();
        config.set(
            "databases.postgres.dsn",
            "postgresql://standalone:secret@standalone/gobby?sslmode=require",
        );
        config
            .write_at(&gcore_config_path(&home))
            .expect("write gcore config");
        fs::write(
            home.join("bootstrap.yaml"),
            "hub_backend: postgres\ndatabase_url: postgresql://daemon:secret@daemon/gobby?application_name=gobby\n",
        )
        .expect("write bootstrap");

        let err = ensure_hub_with_identity(
            &EnsureHubOptions::new(home),
            |_| None,
            |url| {
                matches!(
                    url,
                    "postgresql://standalone:secret@standalone/gobby?sslmode=require"
                        | "postgresql://daemon:secret@daemon/gobby?application_name=gobby"
                )
            },
            |url| {
                let system_identifier =
                    if url.starts_with("postgresql://standalone:secret@standalone/") {
                        "cluster-a"
                    } else {
                        "cluster-b"
                    };
                Ok(HubIdentityProbeResult::Known(HubIdentity {
                    system_identifier: system_identifier.to_string(),
                    database_name: "gobby".to_string(),
                }))
            },
            |_| panic!("conflicting reachable hubs should not provision services"),
        )
        .expect_err("surface divergent hub conflict");

        let message = err.to_string();
        assert!(message.contains("postgresql://standalone/gobby"));
        assert!(message.contains("postgresql://daemon/gobby"));
        assert!(!message.contains("secret"));
        assert!(!message.contains("sslmode"));
        assert!(!message.contains("application_name"));
    }

    #[test]
    fn insufficient_identity_privilege_preserves_hub() {
        let _env = EnvGuard::new();
        let dir = tempfile::tempdir().expect("tempdir");
        let home = dir.path().join(".gobby");
        fs::create_dir_all(&home).expect("create gobby home");
        let mut config = StandaloneConfig::empty();
        config.set("databases.postgres.dsn", "postgresql://standalone/gobby");
        config
            .write_at(&gcore_config_path(&home))
            .expect("write gcore config");
        fs::write(
            home.join("bootstrap.yaml"),
            "hub_backend: postgres\ndatabase_url: postgresql://daemon/gobby\n",
        )
        .expect("write bootstrap");

        let (database_url, report) = ensure_hub_with_identity(
            &EnsureHubOptions::new(home),
            |_| None,
            |url| {
                matches!(
                    url,
                    "postgresql://standalone/gobby" | "postgresql://daemon/gobby"
                )
            },
            |_| {
                Ok(HubIdentityProbeResult::UnknownInsufficientPrivilege {
                    message: "identity_unknown_insufficient_privilege".to_string(),
                })
            },
            |_| panic!("unknown identity for reachable hubs should not provision services"),
        )
        .expect("preserve existing recorded hub");

        assert_eq!(database_url, "postgresql://standalone/gobby");
        assert!(report.is_none());
    }

    #[derive(Default)]
    struct RecordingRunner {
        commands: Vec<CommandSpec>,
    }

    impl CommandRunner for RecordingRunner {
        fn run(&mut self, spec: &CommandSpec) -> std::io::Result<CommandOutput> {
            self.commands.push(spec.clone());
            Ok(CommandOutput {
                status: 0,
                stdout: String::new(),
                stderr: String::new(),
            })
        }
    }

    #[derive(Default)]
    struct RecordingHealth {
        checks: Vec<&'static str>,
    }

    impl DockerHealthChecker for RecordingHealth {
        fn wait_postgres(&mut self, _host: &str, _port: u16) -> anyhow::Result<()> {
            self.checks.push("postgres");
            Ok(())
        }

        fn wait_qdrant(&mut self, _host: &str, _port: u16) -> anyhow::Result<()> {
            self.checks.push("qdrant");
            Ok(())
        }

        fn wait_falkordb(&mut self, _host: &str, _port: u16) -> anyhow::Result<()> {
            self.checks.push("falkordb");
            Ok(())
        }
    }
}
