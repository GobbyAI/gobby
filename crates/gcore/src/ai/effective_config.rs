use std::collections::BTreeMap;
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

pub const EFFECTIVE_CONFIG_PATH: &str = "/api/config/effective";

const EFFECTIVE_CONFIG_TIMEOUT: Duration = Duration::from_secs(2);
const POSTGRES_DSN_KEY: &str = "databases.postgres.dsn";

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
    #[error("daemon effective config protocol failure at HTTP {status}: {reason}")]
    Protocol { status: u16, reason: &'static str },
    #[error("daemon effective config contract failure for key {key:?}: {reason}")]
    Contract { key: String, reason: &'static str },
}

#[derive(Debug, Clone)]
pub enum EffectiveConfigState {
    Available(EffectiveConfigLayers),
    Unavailable,
    Failed(EffectiveConfigError),
}

#[derive(Deserialize)]
struct EffectiveConfigEnvelope {
    config: BTreeMap<String, String>,
}

static EFFECTIVE_CONFIG_STATE: OnceLock<EffectiveConfigState> = OnceLock::new();

pub fn daemon_mode_layers() -> Result<Option<EffectiveConfigLayers>, EffectiveConfigError> {
    layers_from_state(effective_config_state())
}

pub fn daemon_mode_layers_at(
    base_url: &str,
    gobby_home: &Path,
) -> Result<Option<EffectiveConfigLayers>, EffectiveConfigError> {
    let Some(daemon) = fetch_daemon_served_config_at(base_url)? else {
        return Ok(None);
    };
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
    Ok(Some((daemon, routing)))
}

pub fn fetch_daemon_served_config_at(
    base_url: &str,
) -> Result<Option<DaemonServedConfig>, EffectiveConfigError> {
    let url = format!(
        "{}{}",
        base_url.trim_end_matches('/'),
        EFFECTIVE_CONFIG_PATH
    );
    let mut request = ureq::get(&url).timeout(EFFECTIVE_CONFIG_TIMEOUT);
    if let Ok(token) = crate::local_token::read_local_cli_token() {
        request = request.set(
            crate::local_token::AUTHORIZATION_HEADER,
            &crate::local_token::authorization_bearer(&token),
        );
    }
    let response = match request.call() {
        Ok(response) => response,
        Err(ureq::Error::Status(_, _)) | Err(ureq::Error::Transport(_)) => return Ok(None),
    };
    let status = response.status();
    if !(200..300).contains(&status) {
        return Ok(None);
    }
    let body = response
        .into_string()
        .map_err(|_| EffectiveConfigError::Protocol {
            status,
            reason: "response body could not be read",
        })?;
    let envelope: EffectiveConfigEnvelope =
        serde_json::from_str(&body).map_err(|_| EffectiveConfigError::Protocol {
            status,
            reason: "response did not match the required config envelope",
        })?;
    validate_served_values(&envelope.config)?;
    Ok(Some(DaemonServedConfig::new(envelope.config)))
}

pub fn daemon_dsn() -> Result<Option<String>, EffectiveConfigError> {
    daemon_dsn_from_state(effective_config_state())
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
            return EffectiveConfigState::Unavailable;
        };
        match daemon_mode_layers_at(&crate::daemon_url::daemon_url(), &gobby_home) {
            Ok(Some(layers)) => EffectiveConfigState::Available(layers),
            Ok(None) => EffectiveConfigState::Unavailable,
            Err(error) => EffectiveConfigState::Failed(error),
        }
    })
}

fn layers_from_state(
    state: &EffectiveConfigState,
) -> Result<Option<EffectiveConfigLayers>, EffectiveConfigError> {
    match state {
        EffectiveConfigState::Available(layers) => Ok(Some(layers.clone())),
        EffectiveConfigState::Unavailable => Ok(None),
        EffectiveConfigState::Failed(error) => Err(error.clone()),
    }
}

fn daemon_dsn_from_state(
    state: &EffectiveConfigState,
) -> Result<Option<String>, EffectiveConfigError> {
    let Some((mut daemon, _)) = layers_from_state(state)? else {
        return Ok(None);
    };
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
