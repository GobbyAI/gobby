use std::collections::HashMap;

use gobby_core::ai::effective_config::daemon_mode_layers;
use gobby_core::config::{ConfigSource, DaemonServedConfig};
use gobby_core::provisioning::StandaloneConfig;
use gobby_core::runtime_mode::{RuntimeMode, runtime_mode};
use postgres::Client;

use super::runtime_contract::{HubConfigSnapshot, capture_hub_snapshot};
use super::services::{ServiceConfigSource, read_standalone_config_optional, service_env_value};

#[derive(Debug, Clone)]
pub(crate) struct ConfigLayers {
    daemon: Option<DaemonServedConfig>,
    standalone: Option<StandaloneConfig>,
    mode: ConfigMode,
}

#[derive(Debug, Clone, Copy)]
enum ConfigMode {
    Daemon,
    Hub,
    Standalone,
}

pub(crate) fn read_config_layers() -> anyhow::Result<ConfigLayers> {
    if runtime_mode()? == RuntimeMode::Standalone {
        return Ok(ConfigLayers {
            daemon: None,
            standalone: read_standalone_config_optional(),
            mode: ConfigMode::Standalone,
        });
    }
    match daemon_mode_layers() {
        Ok(Some((daemon, _routing))) => Ok(ConfigLayers {
            daemon: Some(daemon),
            standalone: None,
            mode: ConfigMode::Daemon,
        }),
        Ok(None) => Ok(ConfigLayers {
            daemon: None,
            standalone: None,
            mode: ConfigMode::Hub,
        }),
        Err(error) => {
            log::warn!(
                "daemon runtime configuration unavailable; using PostgreSQL snapshot: {error}"
            );
            Ok(ConfigLayers {
                daemon: None,
                standalone: None,
                mode: ConfigMode::Hub,
            })
        }
    }
}

pub(super) enum ServiceSource {
    Daemon {
        served: DaemonServedConfig,
        hits: HashMap<String, &'static str>,
    },
    Hub(HubConfigSnapshot),
    Standalone {
        config: Option<StandaloneConfig>,
        hits: HashMap<String, &'static str>,
        last_value_from_yaml: bool,
    },
}

impl ServiceSource {
    pub(super) fn new(
        conn: &mut Client,
        layers: &ConfigLayers,
    ) -> anyhow::Result<(Self, Option<i64>)> {
        match layers.mode {
            ConfigMode::Daemon => {
                let Some(served) = layers.daemon.clone() else {
                    anyhow::bail!("daemon configuration mode is missing its served snapshot");
                };
                Ok((Self::daemon(served), None))
            }
            ConfigMode::Hub => {
                let snapshot = capture_hub_snapshot(conn)?;
                let revision = snapshot.revision();
                Ok((Self::hub(snapshot), Some(revision)))
            }
            ConfigMode::Standalone => Ok((Self::standalone(layers.standalone.clone()), None)),
        }
    }

    pub(super) fn daemon(served: DaemonServedConfig) -> Self {
        Self::Daemon {
            served,
            hits: HashMap::new(),
        }
    }

    pub(super) fn hub(snapshot: HubConfigSnapshot) -> Self {
        Self::Hub(snapshot)
    }

    pub(super) fn standalone(config: Option<StandaloneConfig>) -> Self {
        Self::Standalone {
            config,
            hits: HashMap::new(),
            last_value_from_yaml: false,
        }
    }
}

impl ServiceConfigSource for ServiceSource {
    fn config_value(&mut self, key: &str) -> anyhow::Result<Option<String>> {
        match self {
            Self::Daemon { served, hits } => {
                if !gobby_core::config::is_registered_runtime_key(key) {
                    // A served value for a key this binary does not register
                    // means the compiled contract predates the daemon: fail
                    // closed on the value, but never silently — the operator
                    // must know the installed gcode is stale.
                    if served.config_value(key).is_some() {
                        log::warn!(
                            "daemon served configuration key {key:?} that this gcode binary's \
                             compiled contract does not register; ignoring it — rebuild and \
                             reinstall gcode"
                        );
                    }
                    return Ok(None);
                }
                if let Some(value) = served.config_value(key) {
                    hits.insert(key.to_string(), "daemon");
                    return Ok(Some(value));
                }
                Ok(None)
            }
            Self::Hub(snapshot) => Ok(snapshot.value(key).map(str::to_string)),
            Self::Standalone {
                config,
                hits,
                last_value_from_yaml,
            } => {
                *last_value_from_yaml = false;
                if let Some(value) = service_env_value(key) {
                    hits.insert(key.to_string(), "env");
                    return Ok(Some(value));
                }
                let value = config
                    .as_mut()
                    .and_then(|standalone| standalone.config_value(key));
                if value.is_some() {
                    hits.insert(key.to_string(), "gcore.yaml");
                    *last_value_from_yaml = true;
                }
                Ok(value)
            }
        }
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        match self {
            Self::Standalone {
                config,
                last_value_from_yaml,
                ..
            } => {
                if std::mem::take(last_value_from_yaml)
                    && let Some(standalone) = config.as_mut()
                {
                    return standalone.resolve_value(value);
                }
                Ok(value.to_string())
            }
            Self::Daemon { .. } | Self::Hub(_) => Ok(value.to_string()),
        }
    }

    fn hit_source(&self, key: &str) -> Option<&'static str> {
        match self {
            Self::Daemon { hits, .. } => hits.get(key).copied(),
            Self::Hub(snapshot) => snapshot.value(key).map(|_| "config_store"),
            Self::Standalone { hits, .. } => hits.get(key).copied(),
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use super::ServiceSource;
    use crate::config::services::{
        ServiceConfigSource, resolve_embedding_config_details_from_service_source,
    };
    use gobby_core::config::{DaemonServedConfig, embedding_keys};

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
    fn daemon_service_source_uses_only_served_values() {
        temp_env::with_var("GOBBY_QDRANT_URL", Some("http://env.example:6333"), || {
            let mut source = ServiceSource::daemon(served([(
                "databases.qdrant.url",
                "http://daemon.example:6333",
            )]));
            assert_eq!(
                source
                    .config_value("databases.qdrant.url")
                    .expect("resolve daemon value")
                    .as_deref(),
                Some("http://daemon.example:6333")
            );
        });

        let mut source = ServiceSource::daemon(served([(
            "databases.qdrant.url",
            "http://daemon.example:6333",
        )]));
        assert_eq!(
            source
                .config_value("databases.qdrant.url")
                .expect("resolve daemon value")
                .as_deref(),
            Some("http://daemon.example:6333")
        );

        let mut source = ServiceSource::daemon(served([]));
        assert_eq!(
            source
                .config_value("databases.qdrant.url")
                .expect("ignore routing value")
                .as_deref(),
            None
        );
    }

    #[test]
    #[serial_test::serial]
    fn daemon_service_source_preserves_served_env_patterns() {
        let mut source =
            ServiceSource::daemon(served([("databases.qdrant.url", "${ROUTING_ENV}")]));
        let value = source
            .config_value("databases.qdrant.url")
            .expect("read daemon value")
            .expect("daemon value");

        temp_env::with_var("ROUTING_ENV", Some("direct"), || {
            assert_eq!(
                source.resolve_value(&value).expect("preserve daemon value"),
                "${ROUTING_ENV}"
            );
        });
    }

    #[test]
    #[serial_test::serial]
    fn daemon_service_source_fails_closed_for_missing_values() {
        temp_env::with_vars(
            [
                ("GOBBY_QDRANT_URL", Some("http://env.example:6333")),
                ("GOBBY_QDRANT_API_KEY", Some("shared-env-secret")),
            ],
            || {
                let mut source = ServiceSource::daemon(served([(
                    "databases.qdrant.url",
                    "http://bundle.example:6333",
                )]));

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

    static CAPTURED_WARNINGS: std::sync::Mutex<Vec<String>> = std::sync::Mutex::new(Vec::new());

    struct CaptureLogger;

    impl log::Log for CaptureLogger {
        fn enabled(&self, metadata: &log::Metadata<'_>) -> bool {
            metadata.level() <= log::Level::Warn
        }

        fn log(&self, record: &log::Record<'_>) {
            if record.level() == log::Level::Warn {
                captured_warnings().push(record.args().to_string());
            }
        }

        fn flush(&self) {}
    }

    fn captured_warnings() -> std::sync::MutexGuard<'static, Vec<String>> {
        CAPTURED_WARNINGS
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    fn install_capture_logger() {
        static INSTALL: std::sync::Once = std::sync::Once::new();
        INSTALL.call_once(|| {
            static LOGGER: CaptureLogger = CaptureLogger;
            log::set_logger(&LOGGER).expect("this test binary installs no other logger");
            log::set_max_level(log::LevelFilter::Warn);
        });
    }

    #[test]
    #[serial_test::serial]
    fn daemon_service_source_warns_for_served_keys_missing_from_the_compiled_contract() {
        install_capture_logger();
        captured_warnings().clear();

        let mut source = ServiceSource::daemon(served([("contract.unknown.key", "served-value")]));
        assert_eq!(
            source
                .config_value("contract.unknown.key")
                .expect("unregistered key fails closed"),
            None
        );
        let warning = captured_warnings()
            .iter()
            .find(|message| message.contains("contract.unknown.key"))
            .cloned()
            .expect("stale-binary warning names the served key");
        assert!(warning.contains("rebuild and reinstall gcode"));
        assert!(
            !warning.contains("served-value"),
            "warning must not leak the served value"
        );

        captured_warnings().clear();
        let mut source = ServiceSource::daemon(served([]));
        assert_eq!(
            source
                .config_value("contract.unknown.key")
                .expect("unserved unregistered key stays a plain miss"),
            None
        );
        assert!(captured_warnings().is_empty());
    }

    #[test]
    fn embedding_details_attribute_served_values_to_daemon() {
        let mut source = ServiceSource::daemon(served([
            (embedding_keys::AI_PROVIDER, "openai"),
            (embedding_keys::AI_API_BASE, "https://daemon.example/v1"),
            (embedding_keys::AI_MODEL, "served-embed"),
        ]));

        let details = resolve_embedding_config_details_from_service_source(&mut source)
            .expect("resolve embedding details")
            .expect("embedding details");

        assert_eq!(details.source, "daemon");
        assert_eq!(details.config.model, "served-embed");
    }
}
