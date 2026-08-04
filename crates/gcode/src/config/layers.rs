use std::collections::HashMap;

use gobby_core::ai::effective_config::daemon_mode_layers;
use gobby_core::config::{ConfigSource, DaemonServedConfig};
use gobby_core::provisioning::StandaloneConfig;
use postgres::Client;

use super::services::{
    FallbackConfigSource, ServiceConfigSource, read_standalone_config_optional, service_env_value,
};

const MANAGED_EXECUTION_BOOTSTRAP_ENV: &str = "GOBBY_MANAGED_EXECUTION_BOOTSTRAP";

#[derive(Debug, Clone)]
pub(crate) struct ConfigLayers {
    daemon: Option<DaemonServedConfig>,
    standalone: Option<StandaloneConfig>,
    managed: bool,
}

pub(crate) fn read_config_layers() -> anyhow::Result<ConfigLayers> {
    let managed = std::env::var_os(MANAGED_EXECUTION_BOOTSTRAP_ENV).is_some();
    match daemon_mode_layers()? {
        Some((daemon, routing)) => Ok(ConfigLayers {
            daemon: Some(daemon),
            standalone: routing,
            managed,
        }),
        None => Ok(ConfigLayers {
            daemon: None,
            standalone: read_standalone_config_optional(),
            managed: false,
        }),
    }
}

pub(super) enum ServiceSource<'a> {
    Daemon {
        served: DaemonServedConfig,
        routing: Option<StandaloneConfig>,
        managed: bool,
        hits: HashMap<String, &'static str>,
        last_value_from_routing: bool,
    },
    Hub(FallbackConfigSource<'a>),
}

impl<'a> ServiceSource<'a> {
    pub(super) fn new(conn: &'a mut Client, layers: &ConfigLayers) -> Self {
        match layers.daemon.clone() {
            Some(served) => Self::daemon(served, layers.standalone.clone(), layers.managed),
            None => Self::Hub(FallbackConfigSource::new(conn, layers.standalone.clone())),
        }
    }

    fn daemon(
        served: DaemonServedConfig,
        routing: Option<StandaloneConfig>,
        managed: bool,
    ) -> Self {
        Self::Daemon {
            served,
            routing,
            managed,
            hits: HashMap::new(),
            last_value_from_routing: false,
        }
    }
}

impl ServiceConfigSource for ServiceSource<'_> {
    fn config_value(&mut self, key: &str) -> anyhow::Result<Option<String>> {
        match self {
            Self::Daemon {
                served,
                routing,
                managed,
                hits,
                last_value_from_routing,
            } => {
                *last_value_from_routing = false;
                if !*managed && let Some(value) = service_env_value(key) {
                    hits.insert(key.to_string(), "env");
                    return Ok(Some(value));
                }
                if let Some(value) = served.config_value(key) {
                    hits.insert(key.to_string(), "daemon");
                    return Ok(Some(value));
                }
                let value = if *managed {
                    None
                } else {
                    routing
                        .as_mut()
                        .and_then(|routing| routing.config_value(key))
                };
                if value.is_some() {
                    hits.insert(key.to_string(), "gcore.yaml");
                    *last_value_from_routing = true;
                }
                Ok(value)
            }
            Self::Hub(source) => source.config_value(key),
        }
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        match self {
            Self::Daemon {
                routing,
                last_value_from_routing,
                ..
            } => {
                if std::mem::take(last_value_from_routing)
                    && let Some(routing) = routing.as_mut()
                {
                    return routing.resolve_value(value);
                }
                Ok(value.to_string())
            }
            Self::Hub(source) => source.resolve_value(value),
        }
    }

    fn hit_source(&self, key: &str) -> Option<&'static str> {
        match self {
            Self::Daemon { hits, .. } => hits.get(key).copied(),
            Self::Hub(source) => source.hit_source(key),
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use gobby_core::config::{DaemonServedConfig, embedding_keys};
    use gobby_core::provisioning::StandaloneConfig;

    use super::ServiceSource;
    use crate::config::services::{
        ServiceConfigSource, resolve_embedding_config_details_from_service_source,
    };

    fn served(
        values: impl IntoIterator<Item = (&'static str, &'static str)>,
    ) -> DaemonServedConfig {
        DaemonServedConfig::new(
            values
                .into_iter()
                .map(|(key, value)| (key.to_string(), value.to_string()))
                .collect::<BTreeMap<_, _>>(),
        )
    }

    #[test]
    #[serial_test::serial]
    fn daemon_service_source_orders_env_served_and_routing() {
        let routing = StandaloneConfig::from_yaml_str_raw(
            "databases.qdrant.url: http://routing.example:6333\n",
        )
        .expect("parse standalone config");

        temp_env::with_var("GOBBY_QDRANT_URL", Some("http://env.example:6333"), || {
            let mut source = ServiceSource::daemon(
                served([("databases.qdrant.url", "http://daemon.example:6333")]),
                Some(routing.clone()),
                false,
            );
            assert_eq!(
                source
                    .config_value("databases.qdrant.url")
                    .expect("resolve env value")
                    .as_deref(),
                Some("http://env.example:6333")
            );
        });

        let mut source = ServiceSource::daemon(
            served([("databases.qdrant.url", "http://daemon.example:6333")]),
            Some(routing.clone()),
            false,
        );
        assert_eq!(
            source
                .config_value("databases.qdrant.url")
                .expect("resolve daemon value")
                .as_deref(),
            Some("http://daemon.example:6333")
        );

        let mut source = ServiceSource::daemon(served([]), Some(routing), false);
        assert_eq!(
            source
                .config_value("databases.qdrant.url")
                .expect("resolve routing value")
                .as_deref(),
            Some("http://routing.example:6333")
        );
    }

    #[test]
    #[serial_test::serial]
    fn daemon_service_source_delegates_routing_interpolation() {
        let routing =
            StandaloneConfig::from_yaml_str_raw("ai.embeddings.routing: ${ROUTING_ENV}\n")
                .expect("parse routing config");
        let mut source = ServiceSource::daemon(served([]), Some(routing), false);
        let value = source
            .config_value("ai.embeddings.routing")
            .expect("read routing value")
            .expect("routing value");

        temp_env::with_var("ROUTING_ENV", Some("direct"), || {
            assert_eq!(
                source.resolve_value(&value).expect("interpolate routing"),
                "direct"
            );
        });
    }

    #[test]
    #[serial_test::serial]
    fn managed_service_source_uses_only_bundle_values() {
        let routing =
            StandaloneConfig::from_yaml_str_raw("databases.qdrant.url: $secret:qdrant_url\n")
                .expect("parse standalone config");

        temp_env::with_vars(
            [
                ("GOBBY_QDRANT_URL", Some("http://env.example:6333")),
                ("GOBBY_QDRANT_API_KEY", Some("shared-env-secret")),
            ],
            || {
                let mut source = ServiceSource::daemon(
                    served([("databases.qdrant.url", "http://bundle.example:6333")]),
                    Some(routing),
                    true,
                );

                assert_eq!(
                    source
                        .config_value("databases.qdrant.url")
                        .expect("resolve bundle value")
                        .as_deref(),
                    Some("http://bundle.example:6333")
                );
                assert_eq!(
                    source
                        .config_value("databases.qdrant.api_key")
                        .expect("secret key fails closed"),
                    None
                );
            },
        );
    }

    #[test]
    fn embedding_details_attribute_served_values_to_daemon() {
        let routing = StandaloneConfig::from_yaml_str_raw("ai.embeddings.routing: direct\n")
            .expect("parse routing config");
        let mut source = ServiceSource::daemon(
            served([
                (embedding_keys::AI_PROVIDER, "openai"),
                (embedding_keys::AI_API_BASE, "https://daemon.example/v1"),
                (embedding_keys::AI_MODEL, "served-embed"),
            ]),
            Some(routing),
            false,
        );

        let details = resolve_embedding_config_details_from_service_source(&mut source)
            .expect("resolve embedding details")
            .expect("embedding details");

        assert_eq!(details.source, "daemon");
        assert_eq!(details.config.model, "served-embed");
    }
}
