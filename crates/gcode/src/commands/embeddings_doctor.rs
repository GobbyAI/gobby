use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::config::{self, Context, EmbeddingConfig, EmbeddingConfigDetails};
use crate::db;
use crate::output;
use crate::utils::api_key_fingerprint;
use crate::vector::code_symbols::probe_embedding_dim;

const EXIT_HEALTHY: u8 = 0;
const EXIT_CONFIG_MISSING: u8 = 10;
const EXIT_DRIFT: u8 = 11;
const EXIT_TRANSPORT: u8 = 20;
const DAEMON_DOCTOR_PATH: &str = "/api/embeddings/doctor";

#[derive(Debug)]
pub struct EmbeddingsDoctorExit {
    payload: EmbeddingsDoctorReport,
    exit_code: u8,
}

impl EmbeddingsDoctorExit {
    pub fn exit_code(&self) -> u8 {
        self.exit_code
    }

    pub fn print(&self) -> anyhow::Result<()> {
        output::print_json(&self.payload)
    }
}

impl std::fmt::Display for EmbeddingsDoctorExit {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "embeddings doctor failed with exit {}", self.exit_code)
    }
}

impl std::error::Error for EmbeddingsDoctorExit {}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct EmbeddingsDoctorReport {
    pub endpoint: Option<String>,
    pub model: Option<String>,
    pub dim: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub probe_error: Option<String>,
    pub api_key_present: bool,
    pub api_key_fingerprint: Option<String>,
    pub namespace_resolved: Option<String>,
    pub source: Option<String>,
    pub agrees: Option<bool>,
    pub drift: Option<Vec<EmbeddingsDoctorDrift>>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct EmbeddingsDoctorDrift {
    pub field: String,
    #[serde(rename = "self")]
    pub self_value: Value,
    pub peer: Value,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
struct PeerDoctorReport {
    endpoint: Option<String>,
    model: Option<String>,
    dim: Option<usize>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum PeerDoctorOutcome {
    Absent,
    Present(PeerDoctorReport),
    TransportError(String),
}

pub fn run(ctx: &Context) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let resolution = config::resolve_embedding_config_details(
        &mut conn,
        config::read_standalone_config_optional(),
    );
    let peer = fetch_daemon_peer(ctx.daemon_url.as_deref());
    let (payload, exit_code) =
        build_doctor_report(resolution, ctx.code_vectors.vector_dim, probe_dim, peer);

    if exit_code == EXIT_HEALTHY {
        output::print_json(&payload)?;
        Ok(())
    } else {
        Err(EmbeddingsDoctorExit { payload, exit_code }.into())
    }
}

fn probe_dim(config: &EmbeddingConfig) -> Result<usize, String> {
    probe_embedding_dim(config).map_err(|error| error.to_string())
}

fn build_doctor_report(
    resolution: Option<EmbeddingConfigDetails>,
    configured_dim: Option<usize>,
    probe: impl FnOnce(&EmbeddingConfig) -> Result<usize, String>,
    peer: PeerDoctorOutcome,
) -> (EmbeddingsDoctorReport, u8) {
    let Some(resolution) = resolution else {
        return (
            EmbeddingsDoctorReport {
                endpoint: None,
                model: None,
                dim: None,
                probe_error: None,
                api_key_present: false,
                api_key_fingerprint: None,
                namespace_resolved: None,
                source: None,
                agrees: None,
                drift: None,
            },
            EXIT_CONFIG_MISSING,
        );
    };

    let dim = match configured_dim {
        Some(dim) => Some(dim),
        None => match probe(&resolution.config) {
            Ok(dim) => Some(dim),
            Err(error) => {
                let mut report = report_without_peer(&resolution, None);
                report.probe_error = Some(error);
                return (report, EXIT_TRANSPORT);
            }
        },
    };

    match peer {
        PeerDoctorOutcome::Absent => (report_without_peer(&resolution, dim), EXIT_HEALTHY),
        PeerDoctorOutcome::TransportError(_) => {
            (report_without_peer(&resolution, dim), EXIT_TRANSPORT)
        }
        PeerDoctorOutcome::Present(peer) => {
            let drift = drift_fields(&resolution.config, dim, &peer);
            if drift.is_empty() {
                (
                    EmbeddingsDoctorReport {
                        agrees: Some(true),
                        drift: None,
                        ..base_report(&resolution, dim)
                    },
                    EXIT_HEALTHY,
                )
            } else {
                (
                    EmbeddingsDoctorReport {
                        agrees: Some(false),
                        drift: Some(drift),
                        ..base_report(&resolution, dim)
                    },
                    EXIT_DRIFT,
                )
            }
        }
    }
}

fn report_without_peer(
    resolution: &EmbeddingConfigDetails,
    dim: Option<usize>,
) -> EmbeddingsDoctorReport {
    EmbeddingsDoctorReport {
        agrees: None,
        drift: None,
        ..base_report(resolution, dim)
    }
}

fn base_report(resolution: &EmbeddingConfigDetails, dim: Option<usize>) -> EmbeddingsDoctorReport {
    EmbeddingsDoctorReport {
        endpoint: Some(resolution.config.api_base.clone()),
        model: Some(resolution.config.model.clone()),
        dim,
        probe_error: None,
        api_key_present: resolution.config.api_key.is_some(),
        api_key_fingerprint: resolution
            .config
            .api_key
            .as_deref()
            .map(api_key_fingerprint),
        namespace_resolved: Some(resolution.namespace.to_string()),
        source: Some(resolution.source.to_string()),
        agrees: None,
        drift: None,
    }
}

fn drift_fields(
    config: &EmbeddingConfig,
    dim: Option<usize>,
    peer: &PeerDoctorReport,
) -> Vec<EmbeddingsDoctorDrift> {
    let mut drift = Vec::new();
    push_drift(
        &mut drift,
        "endpoint",
        Some(config.api_base.as_str()),
        peer.endpoint.as_deref(),
    );
    push_drift(
        &mut drift,
        "model",
        Some(config.model.as_str()),
        peer.model.as_deref(),
    );
    if dim != peer.dim {
        drift.push(EmbeddingsDoctorDrift {
            field: "dim".to_string(),
            self_value: dim.map_or(Value::Null, |value| json!(value)),
            peer: peer.dim.map_or(Value::Null, |value| json!(value)),
        });
    }
    drift
}

fn push_drift(
    drift: &mut Vec<EmbeddingsDoctorDrift>,
    field: &'static str,
    self_value: Option<&str>,
    peer_value: Option<&str>,
) {
    if self_value == peer_value {
        return;
    }
    drift.push(EmbeddingsDoctorDrift {
        field: field.to_string(),
        self_value: self_value.map_or(Value::Null, |value| json!(value)),
        peer: peer_value.map_or(Value::Null, |value| json!(value)),
    });
}

fn fetch_daemon_peer(daemon_url: Option<&str>) -> PeerDoctorOutcome {
    let Some(daemon_url) = daemon_url else {
        return PeerDoctorOutcome::Absent;
    };
    let url = format!("{}{}", daemon_url.trim_end_matches('/'), DAEMON_DOCTOR_PATH);
    let client = match reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
    {
        Ok(client) => client,
        Err(error) => {
            return PeerDoctorOutcome::TransportError(format!("build HTTP client: {error}"));
        }
    };
    let response = match client.get(url).send() {
        Ok(response) => response,
        Err(error) => {
            return PeerDoctorOutcome::TransportError(format!("send peer doctor request: {error}"));
        }
    };
    if response.status() == reqwest::StatusCode::NOT_FOUND {
        return PeerDoctorOutcome::Absent;
    }
    if !response.status().is_success() {
        return PeerDoctorOutcome::TransportError(format!(
            "peer doctor returned HTTP {}",
            response.status()
        ));
    }
    match response.json::<PeerDoctorReport>() {
        Ok(report) => PeerDoctorOutcome::Present(report),
        Err(error) => {
            PeerDoctorOutcome::TransportError(format!("parse peer doctor response: {error}"))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use gobby_core::config::embedding_keys;

    fn details(api_key: Option<&str>) -> EmbeddingConfigDetails {
        EmbeddingConfigDetails {
            config: EmbeddingConfig {
                api_base: "http://embeddings.local/v1".to_string(),
                model: "embed-small".to_string(),
                api_key: api_key.map(str::to_string),
                query_prefix: None,
                timeout_seconds: 10,
            },
            namespace: embedding_keys::AI_NAMESPACE,
            source: "config_store",
        }
    }

    #[test]
    fn doctor_json_and_exit_codes() {
        let (missing, code) = build_doctor_report(
            None,
            None,
            |_| unreachable!("no config should not probe"),
            PeerDoctorOutcome::Absent,
        );
        assert_eq!(code, EXIT_CONFIG_MISSING);
        assert_eq!(missing.namespace_resolved, None);

        let (healthy, code) = build_doctor_report(
            Some(details(Some("secret-key"))),
            Some(768),
            |_| unreachable!("configured dim should not probe"),
            PeerDoctorOutcome::Absent,
        );
        assert_eq!(code, EXIT_HEALTHY);
        assert_eq!(
            healthy.endpoint.as_deref(),
            Some("http://embeddings.local/v1")
        );
        assert_eq!(healthy.dim, Some(768));
        assert!(healthy.api_key_present);
        assert_eq!(
            healthy.api_key_fingerprint,
            Some(api_key_fingerprint("secret-key"))
        );
        assert_eq!(
            serde_json::to_value(&healthy).expect("doctor report serializes")["agrees"],
            Value::Null
        );

        let (drift, code) = build_doctor_report(
            Some(details(None)),
            None,
            |_| Ok(768),
            PeerDoctorOutcome::Present(PeerDoctorReport {
                endpoint: Some("http://other.local/v1".to_string()),
                model: Some("embed-small".to_string()),
                dim: Some(1536),
            }),
        );
        assert_eq!(code, EXIT_DRIFT);
        assert_eq!(drift.agrees, Some(false));
        assert_eq!(
            drift
                .drift
                .as_ref()
                .expect("drift fields")
                .iter()
                .map(|field| field.field.as_str())
                .collect::<Vec<_>>(),
            vec!["endpoint", "dim"]
        );

        let (transport, code) = build_doctor_report(
            Some(details(None)),
            None,
            |_| Err("probe failed".to_string()),
            PeerDoctorOutcome::Absent,
        );
        assert_eq!(code, EXIT_TRANSPORT);
        assert_eq!(transport.dim, None);
        assert_eq!(transport.probe_error.as_deref(), Some("probe failed"));
    }
}
