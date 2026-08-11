use std::collections::BTreeMap;

use crate::provisioning::StandaloneConfig;

use super::ConfigSource;

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DaemonServedConfig {
    values: BTreeMap<String, String>,
}

impl DaemonServedConfig {
    pub fn new(values: BTreeMap<String, String>) -> Self {
        Self { values }
    }

    pub fn served_keys(&self) -> impl Iterator<Item = &str> {
        self.values.keys().map(String::as_str)
    }
}

impl ConfigSource for DaemonServedConfig {
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
    Primary(P),
}

impl<P: ConfigSource> ConfigSource for DaemonOrPrimary<P> {
    fn config_value(&mut self, key: &str) -> Option<String> {
        match self {
            Self::Daemon(source) => source.config_value(key),
            Self::Primary(source) => source.config_value(key),
        }
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        match self {
            Self::Daemon(source) => source.resolve_value(value),
            Self::Primary(source) => source.resolve_value(value),
        }
    }
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
