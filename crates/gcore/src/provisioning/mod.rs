//! Standalone bootstrap and Docker service provisioning.
//!
//! The bundled service assets mirror the Python daemon package layout. Runtime
//! callers can copy them into `~/.gobby/services` and start the same profiles
//! the daemon manages, then persist daemon-style bootstrap keys in `gcore.yaml`.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

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

pub const DEFAULT_QDRANT_HOST: &str = "127.0.0.1";
pub const DEFAULT_QDRANT_HTTP_PORT: u16 = 6333;
pub const DEFAULT_QDRANT_GRPC_PORT: u16 = 6334;

pub const DEFAULT_LM_STUDIO_API_BASE: &str = "http://localhost:1234/v1";
pub const DEFAULT_LM_STUDIO_MODEL: &str = "text-embedding-nomic-embed-text-v1.5@f16";
pub const DEFAULT_OLLAMA_API_BASE: &str = "http://localhost:11434/v1";
pub const DEFAULT_OLLAMA_MODEL: &str = "nomic-embed-text";
pub const DEFAULT_EMBEDDING_VECTOR_DIM: usize = 768;

pub const COMPOSE_TEMPLATE: &str = include_str!("../../assets/docker-compose.services.yml");
const PGSEARCH_DOCKERFILE: &str = include_str!("../../assets/postgres-pgsearch/Dockerfile");
const PGSEARCH_VERSION: &str = include_str!("../../assets/postgres-pgsearch/version.json");
const PGSEARCH_INIT_PG_SEARCH: &str =
    include_str!("../../assets/postgres-pgsearch/initdb.d/01-pg_search.sql");
const PGSEARCH_INIT_PGAUDIT: &str =
    include_str!("../../assets/postgres-pgsearch/initdb.d/02-pgaudit.sql");
const PG_AUDIT_EXPORT: &str =
    include_str!("../../assets/postgres-pgsearch/scripts/pg_audit_export.sh");

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
            insert_nested_yaml_value(&mut mapping, key, value);
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
        "postgresql://{user}:{password}@{host}:{port}/{db}",
        user = DEFAULT_POSTGRES_USER,
        password = DEFAULT_POSTGRES_PASSWORD,
        host = DEFAULT_POSTGRES_HOST,
        db = DEFAULT_POSTGRES_DB
    )
}

fn insert_nested_yaml_value(mapping: &mut serde_yaml::Mapping, key: &str, value: &str) {
    let parts = key
        .split('.')
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>();
    if !parts.is_empty() {
        insert_nested_yaml_parts(mapping, &parts, value);
    }
}

fn insert_nested_yaml_parts(mapping: &mut serde_yaml::Mapping, parts: &[&str], value: &str) {
    let yaml_key = serde_yaml::Value::String(parts[0].to_string());
    if parts.len() == 1 {
        mapping.insert(yaml_key, serde_yaml::Value::String(value.to_string()));
        return;
    }

    let entry = mapping
        .entry(yaml_key)
        .or_insert_with(|| serde_yaml::Value::Mapping(serde_yaml::Mapping::new()));
    if !matches!(entry, serde_yaml::Value::Mapping(_)) {
        *entry = serde_yaml::Value::Mapping(serde_yaml::Mapping::new());
    }
    let serde_yaml::Value::Mapping(child) = entry else {
        unreachable!("entry was normalized to a mapping");
    };
    insert_nested_yaml_parts(child, &parts[1..], value);
}

mod bootstrap;
mod docker;
mod hub;

pub use bootstrap::*;
pub use docker::*;
pub use hub::*;

#[cfg(test)]
mod tests;
