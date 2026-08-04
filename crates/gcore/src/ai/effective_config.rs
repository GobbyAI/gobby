use std::collections::BTreeMap;
use std::error::Error as StdError;
use std::path::Path;
use std::sync::OnceLock;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::Deserialize;
use thiserror::Error;

#[cfg(feature = "postgres")]
use crate::ai_context::PostgresAiConfigSource;
use crate::ai_context::{AiConfigSource, NoPrimaryAiConfigSource};
use crate::config::{ConfigSource, DaemonOrPrimary, DaemonServedConfig, routing_overrides_only};
use crate::provisioning::{StandaloneConfig, gcore_config_path};
use crate::runtime_mode::{RuntimeMode, RuntimeModeError, runtime_mode};

pub const EFFECTIVE_CONFIG_PATH: &str = "/api/config/effective";
pub const SERVICE_CAPABILITIES_PATH: &str = "/api/config/service-capabilities";

const EFFECTIVE_CONFIG_TIMEOUT: Duration = Duration::from_secs(5);
const POSTGRES_DSN_KEY: &str = "databases.postgres.dsn";
const MANAGED_EXECUTION_BOOTSTRAP_ENV: &str = "GOBBY_MANAGED_EXECUTION_BOOTSTRAP";
const MANAGED_CONFIG_KEYS: &[&str] = &[
    "ai.embeddings.dim",
    "ai.embeddings.model",
    "ai.embeddings.query_prefix",
    "ai.embeddings.routing",
    "ai.embeddings.timeout_seconds",
    "databases.falkordb.host",
    "databases.falkordb.port",
    "databases.qdrant.url",
    "indexing.respect_gitignore",
];

pub type EffectiveConfigLayers = (DaemonServedConfig, Option<StandaloneConfig>);
pub type EffectiveLocalAiSource = AiConfigSource<DaemonOrPrimary<NoPrimaryAiConfigSource>>;

#[cfg(feature = "postgres")]
pub type EffectivePostgresAiSource<'a> = AiConfigSource<
    DaemonOrPrimary<
        PostgresAiConfigSource<'a, fn(&str, &mut postgres::Client) -> anyhow::Result<String>>,
    >,
>;

#[derive(Debug, Clone, Error, PartialEq, Eq)]
pub enum EffectiveConfigError {
    #[error(transparent)]
    RuntimeMode(#[from] RuntimeModeError),
    #[error("daemon effective config request failed: {kind}")]
    Transport { kind: EffectiveConfigTransportKind },
    #[error("daemon effective config request failed with HTTP {status}")]
    HttpStatus { status: u16 },
    #[error("daemon effective config protocol failure at HTTP {status}: {reason}")]
    Protocol { status: u16, reason: &'static str },
    #[error("daemon effective config contract failure for key {key:?}: {reason}")]
    Contract { key: String, reason: &'static str },
    #[error("daemon effective config local configuration failure: {reason}")]
    LocalConfiguration { reason: &'static str },
}

#[derive(Debug, Clone, Copy, Error, PartialEq, Eq)]
pub enum EffectiveConfigTransportKind {
    #[error("daemon could not be reached (timeout)")]
    Timeout,
    #[error("daemon could not be reached (unreachable)")]
    Unreachable,
    #[error("daemon could not be reached (other transport error)")]
    Other,
}

#[derive(Debug, Clone)]
pub enum EffectiveConfigState {
    Available(EffectiveConfigLayers),
    Failed(EffectiveConfigError),
}

#[derive(Deserialize)]
struct EffectiveConfigEnvelope {
    config: BTreeMap<String, String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ServiceCapabilityBundle {
    version: u8,
    execution: ManagedExecutionBinding,
    config: BTreeMap<String, String>,
    services: ManagedServices,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ManagedExecutionBinding {
    agent_run_id: String,
    project_id: String,
    session_id: String,
    expires_at: u64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ManagedServices {
    embeddings: ManagedService,
    falkordb: ManagedService,
    qdrant: ManagedService,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ManagedService {
    mode: ManagedServiceMode,
    operations: Vec<ManagedBrokerOperation>,
}

#[derive(Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum ManagedServiceMode {
    Direct,
    Brokered,
    Unavailable,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ManagedBrokerOperation {
    name: ManagedBrokerName,
    method: ManagedBrokerMethod,
    path: String,
}

#[derive(Debug, Copy, Clone, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum ManagedBrokerName {
    Embed,
    ClearProjection,
    RebuildProjection,
    InvalidateProjection,
}

#[derive(Debug, Deserialize, PartialEq, Eq)]
enum ManagedBrokerMethod {
    #[serde(rename = "POST")]
    Post,
}

#[derive(Debug)]
struct ManagedExecutionIdentity {
    agent_run_id: String,
    project_id: String,
    session_id: String,
}

static EFFECTIVE_CONFIG_STATE: OnceLock<EffectiveConfigState> = OnceLock::new();

pub fn daemon_mode_layers() -> Result<Option<EffectiveConfigLayers>, EffectiveConfigError> {
    daemon_mode_layers_for(runtime_mode()?, || {
        layers_from_state(effective_config_state())
    })
}

pub fn daemon_mode_layers_at(
    base_url: &str,
    gobby_home: &Path,
) -> Result<EffectiveConfigLayers, EffectiveConfigError> {
    let token = crate::local_token::read_local_cli_token_for(gobby_home).ok();
    if managed_execution_enabled() {
        let identity = managed_execution_identity()?;
        let daemon = fetch_service_capabilities_at(
            base_url,
            token.as_deref(),
            &identity,
            EFFECTIVE_CONFIG_TIMEOUT,
        )?;
        return Ok((daemon, None));
    }
    let daemon = fetch_daemon_served_config_at(base_url, token.as_deref())?;
    let path = gcore_config_path(gobby_home);
    let routing = match StandaloneConfig::read_raw_at(&path) {
        Ok(config) => config.map(routing_overrides_only),
        Err(error) => {
            log::warn!(
                "daemon effective config is available; ignoring local routing overrides from {}: \
                 {error}",
                path.display()
            );
            None
        }
    };
    Ok((daemon, routing))
}

fn managed_execution_enabled() -> bool {
    std::env::var_os(MANAGED_EXECUTION_BOOTSTRAP_ENV).is_some()
}

fn managed_execution_identity() -> Result<ManagedExecutionIdentity, EffectiveConfigError> {
    fn required(name: &str) -> Result<String, EffectiveConfigError> {
        std::env::var(name)
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .ok_or(EffectiveConfigError::LocalConfiguration {
                reason: "managed execution identity is incomplete",
            })
    }

    Ok(ManagedExecutionIdentity {
        agent_run_id: required("GOBBY_AGENT_RUN_ID")?,
        project_id: required("GOBBY_PROJECT_ID")?,
        session_id: required("GOBBY_SESSION_ID")?,
    })
}

pub fn fetch_daemon_served_config_at(
    base_url: &str,
    token: Option<&str>,
) -> Result<DaemonServedConfig, EffectiveConfigError> {
    fetch_daemon_served_config_at_with_timeout(base_url, token, EFFECTIVE_CONFIG_TIMEOUT)
}

fn fetch_daemon_served_config_at_with_timeout(
    base_url: &str,
    token: Option<&str>,
    timeout: Duration,
) -> Result<DaemonServedConfig, EffectiveConfigError> {
    let (status, body) = fetch_config_body(base_url, EFFECTIVE_CONFIG_PATH, token, timeout, None)?;
    let envelope: EffectiveConfigEnvelope =
        serde_json::from_str(&body).map_err(|_| EffectiveConfigError::Protocol {
            status,
            reason: "response did not match the required config envelope",
        })?;
    validate_served_values(&envelope.config)?;
    Ok(DaemonServedConfig::new(envelope.config))
}

fn fetch_service_capabilities_at(
    base_url: &str,
    token: Option<&str>,
    identity: &ManagedExecutionIdentity,
    timeout: Duration,
) -> Result<DaemonServedConfig, EffectiveConfigError> {
    let (status, body) = fetch_config_body(
        base_url,
        SERVICE_CAPABILITIES_PATH,
        token,
        timeout,
        Some(identity),
    )?;
    let bundle: ServiceCapabilityBundle =
        serde_json::from_str(&body).map_err(|_| EffectiveConfigError::Protocol {
            status,
            reason: "response did not match the required service capability bundle",
        })?;
    validate_managed_bundle(&bundle, identity)?;
    Ok(DaemonServedConfig::new(bundle.config))
}

fn fetch_config_body(
    base_url: &str,
    path: &str,
    token: Option<&str>,
    timeout: Duration,
    identity: Option<&ManagedExecutionIdentity>,
) -> Result<(u16, String), EffectiveConfigError> {
    let url = format!("{}{}", base_url.trim_end_matches('/'), path);
    let mut request =
        crate::local_token::apply_bearer_header_with_token(ureq::get(&url).timeout(timeout), token);
    if let Some(identity) = identity {
        request = request
            .set("X-Gobby-Agent-Run-Id", &identity.agent_run_id)
            .set("X-Gobby-Caller-Project-Id", &identity.project_id)
            .set("X-Gobby-Session-Id", &identity.session_id);
    }
    let response = match request.call() {
        Ok(response) => response,
        Err(ureq::Error::Status(status, _)) => {
            return Err(EffectiveConfigError::HttpStatus { status });
        }
        Err(ureq::Error::Transport(transport)) => {
            return Err(EffectiveConfigError::Transport {
                kind: classify_transport_error(&transport),
            });
        }
    };
    let status = response.status();
    if !(200..300).contains(&status) {
        return Err(EffectiveConfigError::HttpStatus { status });
    }
    let body = response
        .into_string()
        .map_err(|_| EffectiveConfigError::Protocol {
            status,
            reason: "response body could not be read",
        })?;
    Ok((status, body))
}

fn validate_managed_bundle(
    bundle: &ServiceCapabilityBundle,
    identity: &ManagedExecutionIdentity,
) -> Result<(), EffectiveConfigError> {
    if bundle.version != 1 {
        return Err(EffectiveConfigError::Contract {
            key: "version".to_string(),
            reason: "unsupported service capability version",
        });
    }
    if bundle.execution.agent_run_id != identity.agent_run_id
        || bundle.execution.project_id != identity.project_id
        || bundle.execution.session_id != identity.session_id
    {
        return Err(EffectiveConfigError::Contract {
            key: "execution".to_string(),
            reason: "service capability identity does not match managed execution",
        });
    }
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    if bundle.execution.expires_at <= now {
        return Err(EffectiveConfigError::Contract {
            key: "execution.expires_at".to_string(),
            reason: "service capability is expired",
        });
    }
    for key in bundle.config.keys() {
        if !MANAGED_CONFIG_KEYS.contains(&key.as_str()) {
            return Err(EffectiveConfigError::Contract {
                key: key.clone(),
                reason: "configuration key is not allowed in managed execution",
            });
        }
    }
    validate_served_values(&bundle.config)?;
    validate_managed_services(&bundle.services)
}

fn validate_managed_services(services: &ManagedServices) -> Result<(), EffectiveConfigError> {
    if services.embeddings.mode != ManagedServiceMode::Brokered {
        return Err(EffectiveConfigError::Contract {
            key: "services.embeddings.mode".to_string(),
            reason: "managed embeddings must use the daemon broker",
        });
    }
    validate_broker_operations(
        "embeddings",
        &services.embeddings,
        &[(ManagedBrokerName::Embed, "/api/embeddings")],
    )?;
    validate_broker_operations(
        "falkordb",
        &services.falkordb,
        &[
            (
                ManagedBrokerName::ClearProjection,
                "/api/code-index/graph/clear",
            ),
            (
                ManagedBrokerName::RebuildProjection,
                "/api/code-index/graph/rebuild",
            ),
        ],
    )?;
    validate_broker_operations(
        "qdrant",
        &services.qdrant,
        &[(
            ManagedBrokerName::InvalidateProjection,
            "/api/code-index/invalidate",
        )],
    )?;
    Ok(())
}

fn validate_broker_operations(
    service_name: &str,
    service: &ManagedService,
    expected: &[(ManagedBrokerName, &str)],
) -> Result<(), EffectiveConfigError> {
    let valid = service.operations.len() == expected.len()
        && service
            .operations
            .iter()
            .zip(expected)
            .all(|(operation, (name, path))| {
                operation.name == *name
                    && operation.method == ManagedBrokerMethod::Post
                    && operation.path == *path
            });
    if valid {
        return Ok(());
    }
    Err(EffectiveConfigError::Contract {
        key: format!("services.{service_name}.operations"),
        reason: "broker operation does not match the typed capability contract",
    })
}

fn classify_transport_error(transport: &ureq::Transport) -> EffectiveConfigTransportKind {
    if transport_error_is_timeout(transport) {
        return EffectiveConfigTransportKind::Timeout;
    }
    match transport.kind() {
        ureq::ErrorKind::Dns
        | ureq::ErrorKind::ConnectionFailed
        | ureq::ErrorKind::Io
        | ureq::ErrorKind::ProxyConnect => EffectiveConfigTransportKind::Unreachable,
        _ => EffectiveConfigTransportKind::Other,
    }
}

fn transport_error_is_timeout(transport: &ureq::Transport) -> bool {
    let mut source = StdError::source(transport);
    while let Some(error) = source {
        if let Some(error) = error.downcast_ref::<std::io::Error>()
            && matches!(
                error.kind(),
                std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
            )
        {
            return true;
        }
        source = error.source();
    }
    false
}

pub fn daemon_dsn() -> Result<Option<String>, EffectiveConfigError> {
    match runtime_mode()? {
        RuntimeMode::Daemon => daemon_dsn_from_state(effective_config_state()),
        RuntimeMode::Standalone => Ok(None),
    }
}

pub fn ai_source_with_primary<P: ConfigSource>(
    primary: impl FnOnce() -> anyhow::Result<P>,
) -> anyhow::Result<AiConfigSource<DaemonOrPrimary<P>>> {
    match daemon_mode_layers()? {
        Some((daemon, routing)) => Ok(AiConfigSource::with_primary(
            DaemonOrPrimary::Daemon(daemon),
            routing,
        )),
        None => {
            let gobby_home = crate::gobby_home()?;
            AiConfigSource::with_primary_from_gobby_home(
                DaemonOrPrimary::Primary(primary()?),
                &gobby_home,
            )
        }
    }
}

pub fn ai_source_without_primary() -> anyhow::Result<EffectiveLocalAiSource> {
    ai_source_with_primary(|| Ok(NoPrimaryAiConfigSource))
}

#[cfg(feature = "postgres")]
pub fn ai_source_for_conn(
    conn: &mut postgres::Client,
) -> anyhow::Result<EffectivePostgresAiSource<'_>> {
    ai_source_with_primary(|| {
        Ok(PostgresAiConfigSource::new(
            conn,
            crate::secrets::resolve_config_value
                as fn(&str, &mut postgres::Client) -> anyhow::Result<String>,
        ))
    })
}

fn effective_config_state() -> &'static EffectiveConfigState {
    EFFECTIVE_CONFIG_STATE.get_or_init(|| {
        let Ok(gobby_home) = crate::gobby_home() else {
            return EffectiveConfigState::Failed(EffectiveConfigError::LocalConfiguration {
                reason: "Gobby home could not be resolved",
            });
        };
        match daemon_mode_layers_at(&crate::daemon_url::daemon_url(), &gobby_home) {
            Ok(layers) => EffectiveConfigState::Available(layers),
            Err(error) => EffectiveConfigState::Failed(error),
        }
    })
}

fn layers_from_state(
    state: &EffectiveConfigState,
) -> Result<EffectiveConfigLayers, EffectiveConfigError> {
    match state {
        EffectiveConfigState::Available(layers) => Ok(layers.clone()),
        EffectiveConfigState::Failed(error) => Err(error.clone()),
    }
}

fn daemon_mode_layers_for(
    mode: RuntimeMode,
    daemon_layers: impl FnOnce() -> Result<EffectiveConfigLayers, EffectiveConfigError>,
) -> Result<Option<EffectiveConfigLayers>, EffectiveConfigError> {
    match mode {
        RuntimeMode::Daemon => daemon_layers().map(Some),
        RuntimeMode::Standalone => Ok(None),
    }
}

fn daemon_dsn_from_state(
    state: &EffectiveConfigState,
) -> Result<Option<String>, EffectiveConfigError> {
    let (mut daemon, _) = layers_from_state(state)?;
    Ok(daemon
        .config_value(POSTGRES_DSN_KEY)
        .map(|dsn| dsn.trim().to_string())
        .filter(|dsn| !dsn.is_empty()))
}

fn validate_served_values(values: &BTreeMap<String, String>) -> Result<(), EffectiveConfigError> {
    for (key, value) in values {
        if value.contains("$secret:") {
            return Err(EffectiveConfigError::Contract {
                key: key.clone(),
                reason: "value contains an unresolved secret reference",
            });
        }
        if value.contains("${") {
            return Err(EffectiveConfigError::Contract {
                key: key.clone(),
                reason: "value contains an unresolved environment reference",
            });
        }
    }
    Ok(())
}

#[cfg(test)]
fn ai_source_with_primary_from_layers<P: ConfigSource>(
    layers: Result<Option<EffectiveConfigLayers>, EffectiveConfigError>,
    gobby_home: &Path,
    primary: impl FnOnce() -> anyhow::Result<P>,
) -> anyhow::Result<AiConfigSource<DaemonOrPrimary<P>>> {
    match layers? {
        Some((daemon, routing)) => Ok(AiConfigSource::with_primary(
            DaemonOrPrimary::Daemon(daemon),
            routing,
        )),
        None => AiConfigSource::with_primary_from_gobby_home(
            DaemonOrPrimary::Primary(primary()?),
            gobby_home,
        ),
    }
}

#[cfg(test)]
mod tests;
