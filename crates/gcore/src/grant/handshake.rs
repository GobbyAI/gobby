//! Loopback gate, challenge proof, and authenticated handshake.

use std::net::{IpAddr, TcpStream, ToSocketAddrs};
use std::path::Path;
use std::time::{Duration, Instant};

use serde::Deserialize;
use serde_json::{Value, json};

use super::bundle::{GrantBundle, GrantPrincipal, PrincipalKind, parse_grant_json};
use super::cache::normalize_endpoint;
use super::{GrantError, hex_encode, hmac_sha256, sha256};

pub const GRANT_HEADER: &str = "X-Gobby-Runtime-Grant";
pub const MACHINE_HEADER: &str = "X-Gobby-Machine-Id";
pub const CALLER_PROJECT_HEADER: &str = "X-Gobby-Caller-Project-Id";
pub const TARGET_PROJECT_HEADER: &str = "X-Gobby-Project-Id";
pub const SESSION_HEADER: &str = "X-Gobby-Session-Id";
pub const AGENT_RUN_HEADER: &str = "X-Gobby-Agent-Run-Id";
pub const MANAGED_EXECUTION_HEADER: &str = "X-Gobby-Managed-Execution-Id";
pub const CHALLENGE_PATH: &str = "/api/runtime/handshake/challenge";
pub const HANDSHAKE_PATH: &str = "/api/runtime/handshake";
pub const MANAGED_BOOTSTRAP_ENV: &str = "GOBBY_MANAGED_EXECUTION_BOOTSTRAP";
const AGENT_TOKEN_VERSION: &str = "gobby-agent-v1";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CapabilityClaims {
    pub session_id: String,
    pub project_id: String,
    pub machine_id: String,
    pub iat: i64,
    pub exp: i64,
    pub agent_run_id: Option<String>,
    pub managed_execution_id: Option<String>,
    pub signature: Vec<u8>,
}

impl CapabilityClaims {
    pub fn execution_id(&self) -> Option<&str> {
        self.agent_run_id
            .as_deref()
            .or(self.managed_execution_id.as_deref())
    }

    pub fn principal_kind(&self) -> PrincipalKind {
        if self.agent_run_id.is_some() {
            PrincipalKind::AgentRun
        } else {
            PrincipalKind::ToolChat
        }
    }

    pub fn matches_principal(&self, principal: &GrantPrincipal) -> bool {
        self.machine_id == principal.machine_id
            && self.project_id == principal.project_id
            && self.execution_id() == principal.execution_id.as_deref()
            && self.principal_kind() == principal.kind
    }
}

#[derive(Deserialize)]
struct HandshakeEnvelope {
    grant: Value,
    #[allow(dead_code)]
    deployment_token: Option<String>,
    #[allow(dead_code)]
    fencing_epoch: Option<i64>,
}

#[derive(Deserialize)]
struct ChallengeEnvelope {
    proof: String,
}

pub fn reject_remote_endpoint(daemon_url: &str) -> Result<(), GrantError> {
    if is_loopback_url(daemon_url)? {
        Ok(())
    } else {
        Err(GrantError::RemoteEndpoint)
    }
}

pub fn is_loopback_url(daemon_url: &str) -> Result<bool, GrantError> {
    let host = url_host(daemon_url)?;
    Ok(host_is_loopback(&host))
}

pub fn is_default_local_endpoint(daemon_url: &str) -> Result<bool, GrantError> {
    let (host, port) = url_host_port(daemon_url)?;
    Ok(host_is_loopback(&host) && port == crate::bootstrap::DEFAULT_DAEMON_PORT)
}

pub fn daemon_reachable(daemon_url: &str, timeout: Duration) -> bool {
    let Ok((_, port)) = url_host_port(daemon_url) else {
        return false;
    };
    let Ok(host) = url_host(daemon_url) else {
        return false;
    };
    let host = match host.as_str() {
        "localhost" => "127.0.0.1".to_string(),
        other => other.to_string(),
    };
    let Ok(mut addrs) = (host.as_str(), port).to_socket_addrs() else {
        return false;
    };
    let Some(addr) = addrs.next() else {
        return false;
    };
    TcpStream::connect_timeout(&addr, timeout).is_ok()
}

pub fn encode_grant_header(grant: &GrantBundle) -> Result<String, GrantError> {
    Ok(b64url_encode(&grant.model_dump_canonical()?))
}

pub fn deployment_token(data_root: &Path) -> String {
    let root = resolve_path(data_root);
    hex_encode(&sha256(root.to_string_lossy().as_bytes()))[..16].to_string()
}

pub fn parse_capability_token(token: &str) -> Result<CapabilityClaims, GrantError> {
    let mut parts = token.splitn(3, '.');
    let version = parts.next().unwrap_or_default();
    let payload = parts.next().unwrap_or_default();
    let signature = parts.next().unwrap_or_default();
    if version != AGENT_TOKEN_VERSION || payload.is_empty() || signature.is_empty() {
        return Err(GrantError::Malformed(
            "managed envelope token is malformed".to_string(),
        ));
    }
    let payload_bytes = b64url_decode(payload)?;
    let signature_bytes = b64url_decode(signature)?;
    let value: Value = serde_json::from_slice(&payload_bytes)
        .map_err(|error| GrantError::Malformed(error.to_string()))?;
    let session_id = required_str(&value, "session_id")?;
    let project_id = required_str(&value, "project_id")?;
    let machine_id = required_str(&value, "machine_id")?;
    let iat = required_i64(&value, "iat")?;
    let exp = required_i64(&value, "exp")?;
    Ok(CapabilityClaims {
        session_id,
        project_id,
        machine_id,
        iat,
        exp,
        agent_run_id: optional_str(&value, "agent_run_id"),
        managed_execution_id: optional_str(&value, "managed_execution_id"),
        signature: signature_bytes,
    })
}

pub fn challenge_and_handshake(
    daemon_url: &str,
    bearer: &str,
    machine_id: &str,
    project_id: &str,
    session_id: Option<&str>,
    managed: Option<&CapabilityClaims>,
    deadline: Instant,
) -> Result<GrantBundle, GrantError> {
    reject_remote_endpoint(daemon_url)?;
    let remaining = remaining_timeout(deadline)?;
    let nonce = random_nonce()?;
    let kind = if managed.is_some() {
        "managed"
    } else {
        "interactive"
    };
    let mut challenge_body = json!({
        "nonce": b64url_encode(&nonce),
        "kind": kind,
    });
    if let Some(claims) = managed {
        challenge_body["claims"] = json!({
            "session_id": claims.session_id,
            "project_id": claims.project_id,
            "machine_id": claims.machine_id,
            "iat": claims.iat,
            "exp": claims.exp,
            "agent_run_id": claims.agent_run_id,
            "managed_execution_id": claims.managed_execution_id,
        });
    }
    let challenge = http_json(
        "POST",
        &format!("{}{CHALLENGE_PATH}", trim_url(daemon_url)),
        Some(challenge_body.to_string()),
        None,
        None,
        remaining,
    )?;
    if challenge.status == 401 {
        if challenge.body.contains("credential_before_proof") {
            return Err(GrantError::Malformed(
                "challenge rejected attached credentials".to_string(),
            ));
        }
        return Err(GrantError::Expired);
    }
    if !(200..300).contains(&challenge.status) {
        return classify_http(challenge.status, &challenge.body);
    }
    let envelope: ChallengeEnvelope = serde_json::from_str(&challenge.body)
        .map_err(|error| GrantError::Malformed(error.to_string()))?;
    let expected_key = managed
        .map(|claims| claims.signature.as_slice())
        .unwrap_or(bearer.as_bytes());
    let expected = hex_encode(&hmac_sha256(expected_key, &nonce));
    if !constant_time_eq(expected.as_bytes(), envelope.proof.as_bytes()) {
        return Err(GrantError::Malformed(
            "challenge proof did not match the local credential secret".to_string(),
        ));
    }

    let remaining = remaining_timeout(deadline)?;
    let handshake_body = json!({
        "machine_id": machine_id,
        "project_id": project_id,
        "session_id": session_id,
    });
    let handshake = http_json(
        "POST",
        &format!("{}{HANDSHAKE_PATH}", trim_url(daemon_url)),
        Some(handshake_body.to_string()),
        Some(bearer),
        None,
        remaining,
    )?;
    grant_from_handshake(handshake)
}

pub fn grant_from_handshake(response: HttpResponse) -> Result<GrantBundle, GrantError> {
    if response.status == 409 {
        return Err(GrantError::Malformed("stale_epoch".to_string()));
    }
    if response.status == 401 {
        return Err(GrantError::Expired);
    }
    if response.status == 403 && response.body.contains("revoked") {
        return Err(GrantError::Revoked);
    }
    if !(200..300).contains(&response.status) {
        return classify_http(response.status, &response.body);
    }
    let envelope: HandshakeEnvelope = serde_json::from_str(&response.body)
        .map_err(|error| GrantError::Malformed(error.to_string()))?;
    let raw = serde_json::to_vec(&envelope.grant)
        .map_err(|error| GrantError::Malformed(error.to_string()))?;
    parse_grant_json(&raw)
}

#[derive(Debug)]
pub struct HttpResponse {
    pub status: u16,
    pub body: String,
}

pub fn http_json(
    method: &str,
    url: &str,
    body: Option<String>,
    bearer: Option<&str>,
    grant: Option<&GrantBundle>,
    timeout: Duration,
) -> Result<HttpResponse, GrantError> {
    let mut request = match method {
        "GET" => ureq::get(url),
        "POST" => ureq::post(url),
        _ => ureq::request(method, url),
    }
    .timeout(timeout);
    if let Some(token) = bearer {
        request = request.set(
            crate::local_token::AUTHORIZATION_HEADER,
            &crate::local_token::authorization_bearer(token),
        );
    }
    if let Some(grant) = grant {
        request = request.set(GRANT_HEADER, &encode_grant_header(grant)?);
    }
    if body.is_some() {
        request = request.set("content-type", "application/json");
    }
    let result = match body {
        Some(body) => request.send_string(&body),
        None => request.call(),
    };
    match result {
        Ok(response) => read_response(response),
        Err(ureq::Error::Status(status, response)) => {
            let mut http = read_response(response)?;
            http.status = status;
            Ok(http)
        }
        Err(ureq::Error::Transport(transport)) => {
            if transport_is_timeout(&transport) {
                Err(GrantError::Timeout)
            } else {
                Err(GrantError::DaemonRequired)
            }
        }
    }
}

fn read_response(response: ureq::Response) -> Result<HttpResponse, GrantError> {
    let status = response.status();
    let body = response
        .into_string()
        .map_err(|error| GrantError::Io(error.to_string()))?;
    Ok(HttpResponse { status, body })
}

fn classify_http(status: u16, body: &str) -> Result<GrantBundle, GrantError> {
    if body.contains("revoked") {
        return Err(GrantError::Revoked);
    }
    if status == 401 || body.contains("expired") {
        return Err(GrantError::Expired);
    }
    if status == 409 || body.contains("stale_epoch") {
        return Err(GrantError::Malformed("stale_epoch".to_string()));
    }
    Err(GrantError::Malformed(format!(
        "handshake failed with HTTP {status}"
    )))
}

fn remaining_timeout(deadline: Instant) -> Result<Duration, GrantError> {
    let remaining = deadline.saturating_duration_since(Instant::now());
    if remaining.is_zero() {
        Err(GrantError::Timeout)
    } else {
        Ok(remaining)
    }
}

fn url_host(daemon_url: &str) -> Result<String, GrantError> {
    Ok(url_host_port(daemon_url)?.0)
}

fn url_host_port(daemon_url: &str) -> Result<(String, u16), GrantError> {
    let trimmed = normalize_endpoint(daemon_url);
    let rest = trimmed
        .strip_prefix("http://")
        .or_else(|| trimmed.strip_prefix("https://"))
        .ok_or(GrantError::RemoteEndpoint)?;
    let (hostport, _) = rest.split_once('/').unwrap_or((rest, ""));
    if let Some(host) = hostport.strip_prefix('[') {
        let (host, port) = host
            .split_once("]:")
            .ok_or(GrantError::Malformed("invalid daemon url".to_string()))?;
        let port = port
            .parse::<u16>()
            .map_err(|_| GrantError::Malformed("invalid daemon url".to_string()))?;
        return Ok((host.to_string(), port));
    }
    let (host, port) = match hostport.rsplit_once(':') {
        Some((host, port)) => (
            host.to_string(),
            port.parse::<u16>()
                .map_err(|_| GrantError::Malformed("invalid daemon url".to_string()))?,
        ),
        None => (hostport.to_string(), crate::bootstrap::DEFAULT_DAEMON_PORT),
    };
    if host.is_empty() {
        return Err(GrantError::Malformed("invalid daemon url".to_string()));
    }
    Ok((host, port))
}

fn host_is_loopback(host: &str) -> bool {
    if host.eq_ignore_ascii_case("localhost") {
        return true;
    }
    if let Ok(ip) = host.parse::<IpAddr>() {
        return ip.is_loopback();
    }
    false
}

fn trim_url(url: &str) -> String {
    normalize_endpoint(url)
}

fn resolve_path(path: &Path) -> std::path::PathBuf {
    path.canonicalize().unwrap_or_else(|_| {
        if path.is_absolute() {
            path.to_path_buf()
        } else {
            std::env::current_dir()
                .unwrap_or_else(|_| std::path::PathBuf::from("."))
                .join(path)
        }
    })
}

fn random_nonce() -> Result<[u8; 32], GrantError> {
    let mut nonce = [0_u8; 32];
    openssl::rand::rand_bytes(&mut nonce).map_err(|error| GrantError::Io(error.to_string()))?;
    Ok(nonce)
}

fn b64url_encode(bytes: &[u8]) -> String {
    openssl::base64::encode_block(bytes)
        .replace('+', "-")
        .replace('/', "_")
        .trim_end_matches('=')
        .to_string()
}

fn b64url_decode(value: &str) -> Result<Vec<u8>, GrantError> {
    let mut padded = value.replace('-', "+").replace('_', "/");
    while !padded.len().is_multiple_of(4) {
        padded.push('=');
    }
    openssl::base64::decode_block(&padded).map_err(|error| GrantError::Malformed(error.to_string()))
}

fn required_str(value: &Value, key: &str) -> Result<String, GrantError> {
    value
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
        .ok_or_else(|| GrantError::Malformed(format!("capability token missing {key}")))
}

fn required_i64(value: &Value, key: &str) -> Result<i64, GrantError> {
    value
        .get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| GrantError::Malformed(format!("capability token missing {key}")))
}

fn optional_str(value: &Value, key: &str) -> Option<String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    left.len() == right.len()
        && left
            .iter()
            .zip(right)
            .fold(0_u8, |acc, (a, b)| acc | (a ^ b))
            == 0
}

fn transport_is_timeout(transport: &ureq::Transport) -> bool {
    let mut source = std::error::Error::source(transport);
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
