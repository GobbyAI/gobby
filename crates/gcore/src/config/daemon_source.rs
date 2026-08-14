use std::collections::BTreeMap;

use super::{ConfigSource, reject_secret_marker};

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
        reject_secret_marker(value)?;
        Ok(value.to_string())
    }
}

#[derive(Debug, Clone)]
pub enum DaemonOrPrimary<P> {
    Daemon(DaemonServedConfig),
    /// Daemon-served values plus a revision-coherent primary for keys the
    /// daemon never serves. Unresolved secret markers are grant-issuance bugs
    /// and fail typed instead of unwrapping client-side.
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
        reject_secret_marker(value)?;
        match self {
            Self::Daemon(source) => source.resolve_value(value),
            Self::DaemonWithSecrets(source, _secrets) => source.resolve_value(value),
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

fn is_routing_key(key: &str) -> bool {
    key == "ai.routing" || key.ends_with(".routing")
}
