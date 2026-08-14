#![allow(dead_code)]

use std::collections::HashMap;

#[cfg(feature = "ai")]
use gobby_core::ai::effective_config::{
    EffectiveConfigError, EffectiveConfigLayers, daemon_mode_layers,
};
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
    hub_fallback_reason: Option<String>,
}

#[derive(Debug, Clone, Copy)]
enum ConfigMode {
    Daemon,
    Hub,
    Standalone,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum HubConfigCapture {
    DeferredBestEffort,
    ImmediateBestEffort,
    Required,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum HubConfigCaptureStatus {
    Complete,
    Degraded,
}

pub(crate) fn read_config_layers() -> anyhow::Result<ConfigLayers> {
    if runtime_mode()? == RuntimeMode::Standalone {
        return Ok(ConfigLayers {
            daemon: None,
            standalone: read_standalone_config_optional(),
            mode: ConfigMode::Standalone,
            hub_fallback_reason: None,
        });
    }
    #[cfg(feature = "ai")]
    {
        Ok(layers_from_daemon_result(daemon_mode_layers()))
    }
    #[cfg(not(feature = "ai"))]
    {
        return Ok(ConfigLayers {
            daemon: None,
            standalone: None,
            mode: ConfigMode::Hub,
            hub_fallback_reason: Some("gcode built without the ai feature".to_string()),
        });
    }
}

#[cfg(feature = "ai")]
fn layers_from_daemon_result(
    result: Result<Option<EffectiveConfigLayers>, EffectiveConfigError>,
) -> ConfigLayers {
    match result {
        Ok(Some((daemon, _routing))) => {
            warn_for_unregistered_served_keys(&daemon);
            ConfigLayers {
                daemon: Some(daemon),
                standalone: None,
                mode: ConfigMode::Daemon,
                hub_fallback_reason: None,
            }
        }
        Ok(None) => ConfigLayers {
            daemon: None,
            standalone: None,
            mode: ConfigMode::Hub,
            hub_fallback_reason: Some(
                "daemon runtime mode returned no served configuration".to_string(),
            ),
        },
        Err(error) => ConfigLayers {
            daemon: None,
            standalone: None,
            mode: ConfigMode::Hub,
            hub_fallback_reason: Some(format!("daemon runtime configuration unavailable: {error}")),
        },
    }
}

fn warn_for_unregistered_served_keys(served: &DaemonServedConfig) {
    for key in served.served_keys() {
        if !gobby_core::config::is_registered_runtime_key(key) {
            log::warn!(
                "daemon served configuration key {key:?} that this gcode binary's compiled \
                 contract does not register; ignoring it — rebuild and reinstall gcode"
            );
        }
    }
}

pub(super) enum ServiceSource {
    Daemon {
        served: DaemonServedConfig,
        /// Resolved hub values for secret-reference keys the daemon never
        /// serves in plaintext. Read commands without service dependencies
        /// leave this absent.
        hub_secrets: Option<HubConfigSnapshot>,
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
        hub_capture: HubConfigCapture,
    ) -> anyhow::Result<(Self, Option<i64>, HubConfigCaptureStatus)> {
        match layers.mode {
            ConfigMode::Daemon => {
                let Some(served) = layers.daemon.clone() else {
                    anyhow::bail!("daemon configuration mode is missing its served snapshot");
                };
                let daemon_revision = served.revision();
                if hub_capture != HubConfigCapture::DeferredBestEffort {
                    return match capture_hub_snapshot(conn) {
                        Ok(snapshot) => match Self::daemon_with_snapshot(served, snapshot) {
                            Ok((source, revision)) => {
                                Ok((source, Some(revision), HubConfigCaptureStatus::Complete))
                            }
                            Err(error) => capture_failure(error, None, hub_capture),
                        },
                        Err(error) => capture_failure(error, None, hub_capture),
                    };
                }
                Ok((
                    Self::Daemon {
                        served,
                        hub_secrets: None,
                        hits: HashMap::new(),
                    },
                    Some(daemon_revision),
                    HubConfigCaptureStatus::Complete,
                ))
            }
            ConfigMode::Hub => match capture_hub_snapshot(conn) {
                Ok(snapshot) => {
                    if let Some(reason) = &layers.hub_fallback_reason {
                        log::warn!("{reason}; using PostgreSQL snapshot");
                    }
                    let revision = snapshot.revision();
                    Ok((
                        Self::hub(snapshot),
                        Some(revision),
                        HubConfigCaptureStatus::Complete,
                    ))
                }
                Err(error) => {
                    capture_failure(error, layers.hub_fallback_reason.as_deref(), hub_capture)
                }
            },
            ConfigMode::Standalone => Ok((
                Self::standalone(layers.standalone.clone()),
                None,
                HubConfigCaptureStatus::Complete,
            )),
        }
    }

    pub(super) fn daemon_with_snapshot(
        served: DaemonServedConfig,
        snapshot: HubConfigSnapshot,
    ) -> anyhow::Result<(Self, i64)> {
        let daemon_revision = served.revision();
        anyhow::ensure!(
            snapshot.revision() == daemon_revision,
            "configuration revision mismatch: daemon={}, hub={}",
            daemon_revision,
            snapshot.revision()
        );
        Ok((
            Self::Daemon {
                served,
                hub_secrets: Some(snapshot),
                hits: HashMap::new(),
            },
            daemon_revision,
        ))
    }

    /// Served-only daemon source without a hub fall-through (tests).
    #[cfg(test)]
    pub(super) fn daemon(served: DaemonServedConfig) -> Self {
        Self::Daemon {
            served,
            hub_secrets: None,
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
            Self::Daemon {
                served,
                hub_secrets,
                hits,
            } => {
                if !gobby_core::config::is_registered_runtime_key(key) {
                    return Ok(None);
                }
                if let Some(value) = served.config_value(key) {
                    hits.insert(key.to_string(), "daemon");
                    return Ok(Some(value));
                }
                // The daemon never serves secret-bearing keys in plaintext;
                // resolve them datastore-side from a revision-coherent hub
                // snapshot captured on first need.
                if !gobby_core::config::is_secret_reference_key(key) {
                    return Ok(None);
                }
                let value = hub_secrets
                    .as_ref()
                    .and_then(|snapshot| snapshot.value(key))
                    .map(str::to_string);
                if value.is_some() {
                    hits.insert(key.to_string(), "config_store");
                }
                Ok(value)
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

fn capture_failure(
    error: anyhow::Error,
    daemon_reason: Option<&str>,
    hub_capture: HubConfigCapture,
) -> anyhow::Result<(ServiceSource, Option<i64>, HubConfigCaptureStatus)> {
    if hub_capture == HubConfigCapture::Required {
        return Err(required_capture_error(error));
    }
    warn_capture_failure(daemon_reason, &error);
    Ok((
        ServiceSource::standalone(read_standalone_config_optional()),
        None,
        HubConfigCaptureStatus::Degraded,
    ))
}

fn warn_capture_failure(daemon_reason: Option<&str>, error: &anyhow::Error) {
    if super::runtime_contract::hub_capture_permission_denied(error) {
        log::warn!(
            "runtime configuration is unavailable to this scoped database role; using \
             defaults/env/gcore.yaml because scoped roles intentionally cannot read config_store \
             values or secret material directly"
        );
    } else if let Some(reason) = daemon_reason {
        log::warn!(
            "{reason}; PostgreSQL runtime configuration capture also failed; using \
             defaults/env/gcore.yaml: {error:#}"
        );
    } else {
        log::warn!(
            "PostgreSQL runtime configuration capture failed; using defaults/env/gcore.yaml: \
             {error:#}"
        );
    }
}

fn required_capture_error(error: anyhow::Error) -> anyhow::Error {
    if super::runtime_contract::hub_capture_permission_denied(&error) {
        error.context(
            "required runtime configuration cannot be read directly by this scoped database role; \
             embedding-backed indexing and doctor require the Gobby daemon \
             runtime-configuration route",
        )
    } else {
        error.context(
            "required runtime configuration is unavailable; embedding-backed indexing and doctor \
             require a reachable Gobby daemon runtime-configuration route",
        )
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::fs;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;

    use super::{
        ConfigLayers, ConfigMode, HubConfigCapture, HubConfigCaptureStatus, ServiceSource,
        capture_failure, layers_from_daemon_result, warn_for_unregistered_served_keys,
    };
    use crate::config::services::{
        ServiceConfigSource, resolve_embedding_config_details_from_service_source,
    };
    use gobby_core::config::{DaemonServedConfig, embedding_keys};

    fn served(
        values: impl IntoIterator<Item = (&'static str, &'static str)>,
    ) -> DaemonServedConfig {
        DaemonServedConfig::new(
            7,
            values
                .into_iter()
                .map(|(key, value)| (key.to_string(), value.to_string()))
                .collect::<BTreeMap<_, _>>(),
        )
    }

    fn scoped_connection(purpose: &str) -> postgres::Client {
        let database_url = crate::test_env::postgres_test_database_url(purpose);
        let mut connection =
            gobby_core::postgres::connect_readwrite(&database_url).expect("test database");
        connection
            .batch_execute("SET ROLE gobby_gcode_capability")
            .expect("assume scoped gcode capability role");
        connection
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn scoped_hub_capture_degrades_once_for_best_effort_reads() {
        install_capture_logger();
        captured_warnings().clear();
        let mut connection = scoped_connection("best-effort scoped hub capture");
        let layers = ConfigLayers {
            daemon: None,
            standalone: None,
            mode: ConfigMode::Hub,
            hub_fallback_reason: Some("daemon runtime configuration unavailable".to_string()),
        };

        let (source, revision, status) = ServiceSource::new(
            &mut connection,
            &layers,
            HubConfigCapture::ImmediateBestEffort,
        )
        .expect("scoped reads fall back to standalone layers");

        assert!(matches!(source, ServiceSource::Standalone { .. }));
        assert_eq!(revision, None);
        assert_eq!(status, HubConfigCaptureStatus::Degraded);
        let warnings = captured_warnings();
        assert_eq!(warnings.len(), 1);
        assert!(warnings[0].contains("scoped database role"));
        assert!(warnings[0].contains("defaults/env/gcore.yaml"));
        assert!(!warnings[0].contains("database corruption"));
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn arbitrary_hub_capture_failure_degrades_with_one_warning() {
        install_capture_logger();
        captured_warnings().clear();

        let (source, revision, status) = capture_failure(
            anyhow::anyhow!("connection refused"),
            Some("daemon runtime configuration unavailable"),
            HubConfigCapture::ImmediateBestEffort,
        )
        .expect("best-effort reads tolerate every runtime capture failure");

        assert!(matches!(source, ServiceSource::Standalone { .. }));
        assert_eq!(revision, None);
        assert_eq!(status, HubConfigCaptureStatus::Degraded);
        let warnings = captured_warnings();
        assert_eq!(warnings.len(), 1);
        assert!(warnings[0].contains("daemon runtime configuration unavailable"));
        assert!(warnings[0].contains("connection refused"));
        assert!(warnings[0].contains("defaults/env/gcore.yaml"));
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn scoped_daemon_capture_discards_served_services_for_best_effort_reads() {
        install_capture_logger();
        captured_warnings().clear();
        let mut connection = scoped_connection("best-effort scoped daemon capture");
        let layers = ConfigLayers {
            daemon: Some(served([(
                "databases.qdrant.url",
                "http://daemon-qdrant.example",
            )])),
            standalone: None,
            mode: ConfigMode::Daemon,
            hub_fallback_reason: None,
        };

        let (source, revision, status) = ServiceSource::new(
            &mut connection,
            &layers,
            HubConfigCapture::ImmediateBestEffort,
        )
        .expect("hybrid reads fall back instead of mixing served and local config");

        assert!(matches!(source, ServiceSource::Standalone { .. }));
        assert_eq!(revision, None);
        assert_eq!(status, HubConfigCaptureStatus::Degraded);
        assert_eq!(captured_warnings().len(), 1);
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn scoped_required_capture_names_daemon_route() {
        install_capture_logger();
        captured_warnings().clear();
        let mut connection = scoped_connection("required scoped hub capture");
        let layers = ConfigLayers {
            daemon: None,
            standalone: None,
            mode: ConfigMode::Hub,
            hub_fallback_reason: Some("daemon runtime configuration unavailable".to_string()),
        };

        let error = match ServiceSource::new(&mut connection, &layers, HubConfigCapture::Required) {
            Ok(_) => panic!("required runtime config must reject direct scoped capture"),
            Err(error) => error,
        };
        let message = format!("{error:#}");

        assert!(message.contains("scoped database role"));
        assert!(message.contains("daemon runtime-configuration route"));
        assert!(captured_warnings().is_empty());
    }

    fn spawn_served_config() -> (String, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind served-config daemon");
        let address = listener.local_addr().expect("served-config daemon address");
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept served-config request");
            let mut request = [0_u8; 4096];
            let request_len = stream
                .read(&mut request)
                .expect("read served-config request");
            let request = String::from_utf8_lossy(&request[..request_len]);
            assert!(request.starts_with("GET /api/config/effective HTTP/1.1"));
            let body = r#"{"revision":7,"config":{"contract.unknown.key":"served-value"}}"#;
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            )
            .expect("write served-config response");
        });
        (format!("http://{address}"), handle)
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
    fn daemon_service_source_without_hub_keeps_secret_keys_absent() {
        let mut source =
            ServiceSource::daemon(served([("databases.falkordb.host", "falkordb.example")]));

        // Secret-reference keys are never served; without a hub fall-through
        // they stay absent instead of erroring or leaking a reference.
        assert_eq!(
            source
                .config_value("databases.falkordb.password")
                .expect("secret key without hub degrades to absent"),
            None
        );
        assert_eq!(
            source
                .config_value("ai.embeddings.api_key")
                .expect("secret key without hub degrades to absent"),
            None
        );
        // Non-secret served values keep resolving.
        assert_eq!(
            source
                .config_value("databases.falkordb.host")
                .expect("served value")
                .as_deref(),
            Some("falkordb.example")
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
    #[serial_test::serial(serial_db)]
    fn daemon_service_source_warns_for_served_keys_missing_from_the_compiled_contract() {
        install_capture_logger();
        captured_warnings().clear();

        let source = served([("contract.unknown.key", "served-value")]);
        warn_for_unregistered_served_keys(&source);
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
        warn_for_unregistered_served_keys(&served([]));
        assert!(
            captured_warnings()
                .iter()
                .all(|message| !message.contains("served configuration key"))
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn read_config_layers_warns_for_unregistered_served_keys() {
        install_capture_logger();
        captured_warnings().clear();

        let home = tempfile::tempdir().expect("temporary gobby home");
        fs::write(
            home.path()
                .join(gobby_core::local_token::LOCAL_CLI_TOKEN_FILENAME),
            "served-config-token",
        )
        .expect("write local CLI token");
        let (daemon_url, daemon) = spawn_served_config();
        // Fetch through the explicit-argument seam: read_config_layers()
        // consults gcore's process-global runtime-mode and effective-config
        // caches, which earlier tests in this binary may have initialized —
        // that would bypass the spawned server and hang its one-shot accept.
        let layers = temp_env::with_var("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", None::<&str>, || {
            gobby_core::ai::effective_config::daemon_mode_layers_at(&daemon_url, home.path())
                .expect("fetch daemon-served config layers")
        });
        daemon.join().expect("served-config daemon thread");
        layers_from_daemon_result(Ok(Some(layers)));

        let warning = captured_warnings()
            .iter()
            .find(|message| message.contains("contract.unknown.key"))
            .cloned()
            .expect("read_config_layers emits the stale-binary warning");
        assert!(warning.contains("rebuild and reinstall gcode"));
        assert!(!warning.contains("served-value"));
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
