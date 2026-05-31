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

use serde::Deserialize;

use crate::config::{ConfigSource, embedding_keys, resolve_env_pattern};

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
