use std::collections::BTreeMap;

use crate::provisioning::StandaloneConfig;

use super::ConfigSource;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DaemonServedConfig {
    revision: i64,
    values: BTreeMap<String, String>,
}

impl DaemonServedConfig {
    pub fn new(revision: i64, values: BTreeMap<String, String>) -> Self {
        Self { revision, values }
    }

    pub fn revision(&self) -> i64 {
        self.revision
    }

    pub fn served_keys(&self) -> impl Iterator<Item = &str> {
        self.values.keys().map(String::as_str)
    }
}

impl ConfigSource for DaemonServedConfig {
    fn snapshot_revision(&mut self) -> anyhow::Result<Option<i64>> {
        Ok(Some(self.revision))
    }

    fn config_value(&mut self, key: &str) -> Option<String> {
        if is_routing_key(key) {
            return None;
        }
        self.values.get(key).cloned()
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        Ok(value.to_string())
    }
}

#[derive(Debug, Clone)]
pub enum DaemonOrPrimary<P> {
    Daemon(DaemonServedConfig),
    /// Daemon-served values plus a datastore-backed primary consulted only
    /// for secret-reference keys. The daemon never serves secrets in
    /// plaintext; keys the contract marks `reference` fall through to the
    /// primary, which returns the stored `$secret:` form and resolves it
    /// datastore-side.
    DaemonWithSecrets(DaemonServedConfig, P),
    Primary(P),
}

impl<P: ConfigSource> ConfigSource for DaemonOrPrimary<P> {
    fn snapshot_revision(&mut self) -> anyhow::Result<Option<i64>> {
        match self {
            Self::Daemon(source) => source.snapshot_revision(),
            Self::DaemonWithSecrets(source, primary) => {
                require_matching_revision(source, primary)?;
                source.snapshot_revision()
            }
            Self::Primary(source) => source.snapshot_revision(),
        }
    }

    fn config_value(&mut self, key: &str) -> Option<String> {
        match self {
            Self::Daemon(source) => source.config_value(key),
            Self::DaemonWithSecrets(source, secrets) => source.config_value(key).or_else(|| {
                if !super::is_secret_reference_key(key)
                    || require_matching_revision(source, secrets).is_err()
                {
                    return None;
                }
                let value = secrets.config_value(key);
                require_matching_revision(source, secrets).ok()?;
                value
            }),
            Self::Primary(source) => source.config_value(key),
        }
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        match self {
            Self::Daemon(source) => source.resolve_value(value),
            Self::DaemonWithSecrets(source, secrets) => {
                if value.trim_start().starts_with("$secret:") {
                    require_matching_revision(source, secrets)?;
                    let resolved = secrets.resolve_value(value)?;
                    require_matching_revision(source, secrets)?;
                    Ok(resolved)
                } else {
                    source.resolve_value(value)
                }
            }
            Self::Primary(source) => source.resolve_value(value),
        }
    }
}

fn require_matching_revision(
    daemon: &DaemonServedConfig,
    primary: &mut impl ConfigSource,
) -> anyhow::Result<()> {
    let primary_revision = primary
        .snapshot_revision()?
        .ok_or_else(|| anyhow::anyhow!("primary configuration source has no snapshot revision"))?;
    anyhow::ensure!(
        primary_revision == daemon.revision(),
        "configuration revision mismatch: daemon={}, primary={primary_revision}",
        daemon.revision()
    );
    Ok(())
}

pub fn routing_overrides_only(config: StandaloneConfig) -> StandaloneConfig {
    let values = config
        .into_values()
        .into_iter()
        .filter(|(key, _)| is_routing_key(key))
        .collect();
    StandaloneConfig::new(values)
}

fn is_routing_key(key: &str) -> bool {
    key == "ai.routing" || key.ends_with(".routing")
}
