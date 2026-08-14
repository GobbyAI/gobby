use std::collections::BTreeMap;
use std::error::Error as StdError;
use std::io;
use std::path::Path;
use std::sync::OnceLock;
use std::time::Duration;

use serde::Deserialize;
use thiserror::Error;

#[cfg(feature = "postgres")]
use crate::ai_context::PostgresAiConfigSource;
use crate::ai_context::{AiConfigSource, NoPrimaryAiConfigSource};
use crate::config::{ConfigSource, DaemonOrPrimary, DaemonServedConfig, routing_overrides_only};
use crate::provisioning::{StandaloneConfig, gcore_config_path};
use crate::runtime_mode::{RuntimeMode, RuntimeModeError, runtime_mode};

pub const EFFECTIVE_CONFIG_PATH: &str = "/api/config/effective";

const EFFECTIVE_CONFIG_TIMEOUT: Duration = Duration::from_secs(5);
const POSTGRES_DSN_KEY: &str = "databases.postgres.dsn";
const MANAGED_EXECUTION_BOOTSTRAP_ENV: &str = "GOBBY_MANAGED_EXECUTION_BOOTSTRAP";

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
    revision: i64,
    config: BTreeMap<String, String>,
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
    if let Some(path) = std::env::var_os(MANAGED_EXECUTION_BOOTSTRAP_ENV) {
        let grant = crate::grant::load_grant_file(Path::new(&path)).map_err(|_| {
            EffectiveConfigError::LocalConfiguration {
                reason: "managed grant file could not be loaded",
            }
        })?;
        let settings = crate::config::fetch_machine_config(
            base_url,
            &grant,
            token.as_deref(),
            EFFECTIVE_CONFIG_TIMEOUT,
        )
        .map_err(|error| match error {
            crate::grant::GrantError::Timeout => EffectiveConfigError::Transport {
                kind: EffectiveConfigTransportKind::Timeout,
            },
            crate::grant::GrantError::DaemonRequired => EffectiveConfigError::Transport {
                kind: EffectiveConfigTransportKind::Unreachable,
            },
            _ => EffectiveConfigError::LocalConfiguration {
                reason: "managed runtime config fetch failed",
            },
        })?;
        validate_served_values(&settings.settings)?;
        return Ok((
            DaemonServedConfig::new(settings.config_revision, settings.settings),
            None,
        ));
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
    let (status, body) = fetch_config_body(base_url, EFFECTIVE_CONFIG_PATH, token, timeout)?;
    let envelope: EffectiveConfigEnvelope =
        serde_json::from_str(&body).map_err(|_| EffectiveConfigError::Protocol {
            status,
            reason: "response did not match the required config envelope",
        })?;
    validate_served_values(&envelope.config)?;
    Ok(DaemonServedConfig::new(envelope.revision, envelope.config))
}

fn fetch_config_body(
    base_url: &str,
    path: &str,
    token: Option<&str>,
    timeout: Duration,
) -> Result<(u16, String), EffectiveConfigError> {
    let url = format!("{}{}", base_url.trim_end_matches('/'), path);
    let request =
        crate::local_token::apply_bearer_header_with_token(ureq::get(&url).timeout(timeout), token);
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
        .map_err(|error| EffectiveConfigError::Transport {
            kind: classify_response_body_error(&error),
        })?;
    Ok((status, body))
}

fn classify_response_body_error(error: &io::Error) -> EffectiveConfigTransportKind {
    match error.kind() {
        io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock => {
            EffectiveConfigTransportKind::Timeout
        }
        io::ErrorKind::ConnectionAborted
        | io::ErrorKind::ConnectionRefused
        | io::ErrorKind::ConnectionReset
        | io::ErrorKind::NotConnected
        | io::ErrorKind::UnexpectedEof => EffectiveConfigTransportKind::Unreachable,
        _ => EffectiveConfigTransportKind::Other,
    }
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

/// Like [`ai_source_with_primary`], but keeps the primary attached in daemon
/// mode so secret-reference keys (never served by the daemon) fall through to
/// it and `$secret:` values resolve datastore-side. The factory therefore runs
/// in both modes — use this only when constructing the primary is cheap and
/// side-effect free (an already-open connection, or a lazily-connecting
/// source); `ai_source_with_primary` keeps the historical guarantee that
/// daemon mode never invokes the factory.
pub fn ai_source_with_secret_primary<P: ConfigSource>(
    primary: impl FnOnce() -> anyhow::Result<P>,
) -> anyhow::Result<AiConfigSource<DaemonOrPrimary<P>>> {
    match daemon_mode_layers()? {
        Some((daemon, routing)) => Ok(AiConfigSource::with_primary(
            DaemonOrPrimary::DaemonWithSecrets(daemon, primary()?),
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

#[cfg(feature = "postgres")]
pub fn ai_source_for_conn(
    conn: &mut postgres::Client,
) -> anyhow::Result<EffectivePostgresAiSource<'_>> {
    ai_source_with_secret_primary(|| {
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
