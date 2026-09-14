//! gcore grant client: handshake, cache, renewal, and typed errors.

mod acquisition;
mod bundle;
mod cache;
pub mod fixture;
mod handshake;
mod inspection;

use std::fmt;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub use bundle::{
    AiCapability, BrokerOperation, EXPECTED_API_CONTRACT, FalkorCapability, GRANT_VERSION,
    GrantBundle, GrantCapabilities, GrantDeployment, GrantPrincipal, GrantSchemaIdentity,
    PostgresCapability, PrincipalKind, QdrantCapability, canonical_payload_bytes,
    expected_schema_identity, parse_grant_json, payload_checksum, validate_for_construction,
    verify_payload_checksum,
};
pub use cache::{
    CachedSettings, GrantFileLock, TrustedBinding, binding_path, grant_lock_path,
    interactive_cache_path, load_binding, load_grant_file, settings_cache_path, try_lock,
    write_binding, write_coherent_pair, write_grant_file,
};
pub use fixture::{DirectConnections, managed_direct_grant, write_managed_bootstrap};
pub use handshake::{
    AGENT_RUN_HEADER, CALLER_PROJECT_HEADER, CapabilityClaims, GRANT_HEADER, MACHINE_HEADER,
    MANAGED_BOOTSTRAP_ENV, MANAGED_EXECUTION_HEADER, SESSION_HEADER, TARGET_PROJECT_HEADER,
    daemon_reachable, encode_grant_header, parse_capability_token, reject_remote_endpoint,
};
pub use inspection::{CachedGrantInspection, inspect_cached_grant, inspect_cached_grant_at};

#[cfg(test)]
use acquisition::{AcquireCtx, refresh_or_fail};
pub use acquisition::{
    AcquireRequest, acquire, acquire_with, present_bundle_with_single_retry,
    present_with_single_retry, rehandshake,
};
use handshake::{
    deployment_token as derived_deployment_token, parse_capability_token as parse_envelope,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GrantError {
    DaemonRequired,
    Expired,
    SchemaMismatch {
        grant_version: i64,
        binary_version: i64,
    },
    DeploymentMismatch,
    ApiContractMismatch {
        grant_contract: Option<i64>,
        binary_contract: i64,
        source: Option<String>,
    },
    PayloadSkew {
        detail: String,
    },
    RemoteEndpoint,
    ConfigRevisionMismatch,
    Revoked,
    Timeout,
    Malformed(String),
    Io(String),
}

impl fmt::Display for GrantError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DaemonRequired => f.write_str("daemon required"),
            Self::Expired => f.write_str("grant expired"),
            Self::SchemaMismatch {
                grant_version,
                binary_version,
            } => write!(
                f,
                "daemon-issued grant schema identity v{grant_version} does not match binary-embedded schema identity v{binary_version}"
            ),
            Self::DeploymentMismatch => f.write_str("deployment mismatch"),
            Self::ApiContractMismatch {
                grant_contract,
                binary_contract,
                source,
            } => f.write_str(&api_contract_mismatch_display(
                grant_contract,
                binary_contract,
                source,
            )),
            Self::PayloadSkew { detail } => write!(f, "grant payload skew: {detail}"),
            Self::RemoteEndpoint => f.write_str("remote daemon endpoint refused"),
            Self::ConfigRevisionMismatch => f.write_str("config revision mismatch"),
            Self::Revoked => f.write_str("grant revoked"),
            Self::Timeout => f.write_str("grant operation timed out"),
            Self::Malformed(message) => write!(f, "malformed grant: {message}"),
            Self::Io(message) => write!(f, "grant io error: {message}"),
        }
    }
}

impl std::error::Error for GrantError {}

impl GrantError {
    pub fn cli_code(&self) -> &'static str {
        match self {
            Self::DaemonRequired => "daemon_required",
            Self::Expired => "expired",
            Self::SchemaMismatch { .. } => "schema_mismatch",
            Self::DeploymentMismatch => "deployment_mismatch",
            Self::ApiContractMismatch { .. } => "api_contract_mismatch",
            Self::PayloadSkew { .. } => "payload_skew",
            Self::RemoteEndpoint => "remote_endpoint",
            Self::ConfigRevisionMismatch => "config_revision_mismatch",
            Self::Revoked => "revoked",
            Self::Timeout => "timeout",
            Self::Malformed(_) => "malformed",
            Self::Io(_) => "io",
        }
    }

    pub fn exit_status(&self) -> i32 {
        match self {
            Self::Io(_) => 1,
            _ => 2,
        }
    }

    pub fn is_stale_epoch(&self) -> bool {
        matches!(self, Self::Malformed(message) if message == "stale_epoch")
    }

    pub fn is_retryable_presentation(&self) -> bool {
        self.is_stale_epoch()
            || matches!(self, Self::Malformed(message) if message == "invalid_signature")
    }

    pub fn from_presentation_http(status: u16, body: &str) -> Option<Self> {
        if (200..300).contains(&status) {
            return None;
        }
        if body.contains("revoked") {
            return Some(Self::Revoked);
        }
        if status == 409 || body.contains("stale_epoch") {
            return Some(Self::Malformed("stale_epoch".to_string()));
        }
        if body.contains("invalid_signature") {
            return Some(Self::Malformed("invalid_signature".to_string()));
        }
        None
    }
}

fn api_contract_mismatch_display(
    grant_contract: &Option<i64>,
    binary_contract: &i64,
    source: &Option<String>,
) -> String {
    let grant_contract = grant_contract
        .map(|value| value.to_string())
        .unwrap_or_else(|| "unknown".to_string());
    let message = format!(
        "grant api contract {grant_contract} does not match this binary's supported contract {binary_contract}"
    );
    match source {
        Some(source) => format!("{source}: {message}"),
        None => message,
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GrantSource {
    ManagedFile,
    Cache,
    Handshake,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AcquiredGrant {
    pub bundle: GrantBundle,
    pub source: GrantSource,
    pub settings: Option<CachedSettings>,
    pub daemon_reachable: bool,
    now: i64,
}

impl AcquiredGrant {
    pub fn permits_datastore(&self) -> bool {
        !self.bundle.is_expired(self.now)
    }

    pub fn permits_ai(&self) -> bool {
        self.permits_datastore() && self.daemon_reachable
    }
}

pub fn deployment_token(data_root: &Path) -> String {
    derived_deployment_token(data_root)
}

pub const RUNTIME_CONFIG_PATH: &str = "/api/runtime/config";

pub fn fetch_runtime_config(
    base_url: &str,
    grant: &GrantBundle,
    bearer: Option<&str>,
    timeout: Duration,
) -> Result<CachedSettings, GrantError> {
    let url = format!(
        "{}{}",
        cache::normalize_endpoint(base_url),
        RUNTIME_CONFIG_PATH
    );
    // The config route binds identity like the handshake: a managed bearer must
    // carry the caller-project, session, and owner headers from its capability
    // claims or the daemon rejects it. Operator tokens carry none.
    let claims = bearer.and_then(|token| parse_envelope(token).ok());
    let identity_headers = claims
        .as_ref()
        .map(handshake::managed_identity_headers)
        .unwrap_or_default();
    let response = handshake::http_json(
        "GET",
        &url,
        None,
        bearer,
        Some(grant),
        &identity_headers,
        timeout,
    )?;
    if !(200..300).contains(&response.status) {
        if let Some(error) = GrantError::from_presentation_http(response.status, &response.body) {
            return Err(error);
        }
        if response.status == 401 {
            return Err(GrantError::Expired);
        }
        return Err(GrantError::Malformed(format!(
            "runtime config failed with HTTP {}",
            response.status
        )));
    }
    #[derive(serde::Deserialize)]
    struct Envelope {
        config_revision: i64,
        settings: std::collections::BTreeMap<String, String>,
    }
    let envelope: Envelope = serde_json::from_str(&response.body)
        .map_err(|error| GrantError::Malformed(error.to_string()))?;
    Ok(CachedSettings {
        config_revision: envelope.config_revision,
        settings: envelope.settings,
    })
}

fn resolve_home(home: Option<&Path>) -> Result<PathBuf, GrantError> {
    match home {
        Some(home) => Ok(home.to_path_buf()),
        None => crate::gobby_home().map_err(|error| GrantError::Io(error.to_string())),
    }
}

fn unix_now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or(0)
}

fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 0x0f) as usize] as char);
    }
    out
}

fn sha256(bytes: &[u8]) -> [u8; 32] {
    openssl::sha::sha256(bytes)
}

#[cfg(test)]
#[path = "tests.rs"]
mod tests;
