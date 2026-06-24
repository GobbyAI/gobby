use crate::diagnose::GHOOK_VERSION;
use crate::envelope::Envelope;
use crate::transport;
use anyhow::{Context, Result};
use serde::Serialize;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};
use uuid::Uuid;

const FAILURE_SCHEMA_VERSION: u32 = 1;
const RESPONSE_BODY_MAX_BYTES: usize = 8192;
const RECENT_FAILURE_LIMIT: usize = 10;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum FailureKind {
    InvalidSuccessJson,
    SuccessResponseMapping,
    Http,
    Connect,
    Timeout,
    DirectPostAfterEnqueueFailure,
    Other,
}

impl FailureKind {
    fn as_str(self) -> &'static str {
        match self {
            Self::InvalidSuccessJson => "invalid_success_json",
            Self::SuccessResponseMapping => "success_response_mapping",
            Self::Http => "http",
            Self::Connect => "connect",
            Self::Timeout => "timeout",
            Self::DirectPostAfterEnqueueFailure => "direct_post_after_enqueue_failure",
            Self::Other => "other",
        }
    }
}

impl From<transport::DeliveryFailureKind> for FailureKind {
    fn from(value: transport::DeliveryFailureKind) -> Self {
        match value {
            transport::DeliveryFailureKind::Http => Self::Http,
            transport::DeliveryFailureKind::Connect => Self::Connect,
            transport::DeliveryFailureKind::Timeout => Self::Timeout,
            transport::DeliveryFailureKind::Other => Self::Other,
        }
    }
}

pub(crate) struct FailureContext<'a> {
    pub envelope: &'a Envelope,
    pub envelope_id: Option<&'a str>,
    pub failure_kind: FailureKind,
    pub status_code: Option<u16>,
    pub error: Option<&'a str>,
    pub response_body: Option<&'a str>,
    pub transport_error: Option<&'a str>,
    pub daemon_url: &'a str,
}

#[derive(Debug, Serialize)]
struct FailureArtifact {
    schema_version: u32,
    written_at: String,
    ghook_version: String,
    envelope_id: Option<String>,
    hook_type: String,
    source: String,
    critical: bool,
    failure_kind: FailureKind,
    status_code: Option<u16>,
    error: Option<String>,
    response_body_preview: Option<String>,
    response_body_truncated: bool,
    response_body_length: Option<usize>,
    response_body_hash: Option<String>,
    transport_error: Option<String>,
    daemon_url: String,
    daemon_host: String,
    daemon_port: u16,
}

#[derive(Debug, Clone, Serialize)]
pub struct RecentFailureMetadata {
    pub path: PathBuf,
    pub modified_at_unix_ms: Option<u64>,
}

pub(crate) struct FailureInventory {
    pub failure_dir: PathBuf,
    pub recent_failure_count: usize,
    pub recent_failures: Vec<RecentFailureMetadata>,
}

struct FailureEntry {
    path: PathBuf,
    modified_at: Option<SystemTime>,
}

pub(crate) fn failure_dir() -> Result<PathBuf> {
    Ok(gobby_core::gobby_home()?
        .join("hooks")
        .join("inbox")
        .join("failures"))
}

pub(crate) fn failure_inventory() -> FailureInventory {
    let failure_dir = failure_dir().unwrap_or_else(|_| {
        PathBuf::from(".gobby")
            .join("hooks")
            .join("inbox")
            .join("failures")
    });
    let mut entries = read_failure_entries(&failure_dir);
    let recent_failure_count = entries.len();
    entries.sort_by(|a, b| b.modified_at.cmp(&a.modified_at).then(a.path.cmp(&b.path)));
    entries.truncate(RECENT_FAILURE_LIMIT);
    let recent_failures = entries
        .into_iter()
        .map(|entry| RecentFailureMetadata {
            path: entry.path,
            modified_at_unix_ms: entry.modified_at.and_then(unix_ms),
        })
        .collect();

    FailureInventory {
        failure_dir,
        recent_failure_count,
        recent_failures,
    }
}

pub(crate) fn record_failure(ctx: FailureContext<'_>) -> Result<PathBuf> {
    let dir = failure_dir()?;
    record_failure_to_dir(&dir, ctx)
}

fn record_failure_to_dir(dir: &Path, ctx: FailureContext<'_>) -> Result<PathBuf> {
    let artifact = FailureArtifact::from_context(ctx);
    let file_name = format!(
        "{}-{}-{}-{}.json",
        transport::ts13(),
        if artifact.critical { "c" } else { "n" },
        artifact.failure_kind.as_str(),
        Uuid::new_v4()
    );
    let path = dir.join(file_name);
    let bytes = serde_json::to_vec_pretty(&artifact).context("serialize ghook failure artifact")?;
    transport::atomic_write(&path, &bytes).context("write ghook failure artifact")?;
    Ok(path)
}

impl FailureArtifact {
    fn from_context(ctx: FailureContext<'_>) -> Self {
        let (response_body_preview, response_body_truncated, response_body_length) =
            match ctx.response_body {
                Some(body) => {
                    let (preview, truncated) = cap_response_body(body);
                    (Some(preview), truncated, Some(body.len()))
                }
                None => (None, false, None),
            };
        let response_body_hash = ctx.response_body.map(|body| fnv1a64(body.as_bytes()));
        let (daemon_host, daemon_port) = daemon_host_port(ctx.daemon_url);

        Self {
            schema_version: FAILURE_SCHEMA_VERSION,
            written_at: chrono::Utc::now().to_rfc3339(),
            ghook_version: GHOOK_VERSION.to_string(),
            envelope_id: ctx.envelope_id.map(ToOwned::to_owned),
            hook_type: ctx.envelope.hook_type.clone(),
            source: ctx.envelope.source.clone(),
            critical: ctx.envelope.critical,
            failure_kind: ctx.failure_kind,
            status_code: ctx.status_code,
            error: ctx.error.map(ToOwned::to_owned),
            response_body_preview,
            response_body_truncated,
            response_body_length,
            response_body_hash,
            transport_error: ctx.transport_error.map(ToOwned::to_owned),
            daemon_url: ctx.daemon_url.to_string(),
            daemon_host,
            daemon_port,
        }
    }
}

fn read_failure_entries(dir: &Path) -> Vec<FailureEntry> {
    let Ok(entries) = fs::read_dir(dir) else {
        return Vec::new();
    };
    entries
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let path = entry.path();
            if path.extension().and_then(|ext| ext.to_str()) != Some("json") {
                return None;
            }
            let modified_at = entry.metadata().ok().and_then(|meta| meta.modified().ok());
            Some(FailureEntry { path, modified_at })
        })
        .collect()
}

fn cap_response_body(body: &str) -> (String, bool) {
    if body.len() <= RESPONSE_BODY_MAX_BYTES {
        return (body.to_string(), false);
    }

    let mut end = RESPONSE_BODY_MAX_BYTES;
    while !body.is_char_boundary(end) {
        end -= 1;
    }
    (body[..end].to_string(), true)
}

fn fnv1a64(bytes: &[u8]) -> String {
    let mut hash = 0xcbf2_9ce4_8422_2325u64;
    for byte in bytes {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    format!("fnv1a64:{hash:016x}")
}

fn daemon_host_port(daemon_url: &str) -> (String, u16) {
    let endpoint = gobby_core::bootstrap::read_daemon_endpoint();
    let trimmed = daemon_url.trim();
    let scheme = trimmed.split_once("://").map(|(scheme, _)| scheme);
    let authority = trimmed
        .split_once("://")
        .map(|(_, rest)| rest)
        .unwrap_or(trimmed)
        .split('/')
        .next()
        .unwrap_or_default();
    let authority = authority
        .rsplit_once('@')
        .map(|(_, host)| host)
        .unwrap_or(authority);

    if let Some(rest) = authority.strip_prefix('[')
        && let Some((host, after_host)) = rest.split_once(']')
    {
        let port = after_host
            .strip_prefix(':')
            .and_then(|p| p.parse::<u16>().ok())
            .unwrap_or(endpoint.port);
        return (host.to_string(), port);
    }

    if let Some((host, port)) = authority.rsplit_once(':')
        && let Ok(port) = port.parse::<u16>()
    {
        return (host.to_string(), port);
    }

    let fallback_port = match scheme {
        Some("https") => 443,
        Some("http") => 80,
        _ => endpoint.port,
    };
    (
        if authority.is_empty() {
            endpoint.host
        } else {
            authority.to_string()
        },
        fallback_port,
    )
}

fn unix_ms(time: SystemTime) -> Option<u64> {
    time.duration_since(UNIX_EPOCH)
        .ok()
        .and_then(|duration| duration.as_millis().try_into().ok())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};
    use std::collections::BTreeMap;
    use tempfile::tempdir;

    fn envelope() -> Envelope {
        Envelope {
            schema_version: 1,
            enqueued_at: "2026-06-24T00:00:00Z".to_string(),
            critical: true,
            hook_type: "SessionStart".to_string(),
            input_data: json!({"prompt": "must not be copied"}),
            source: "codex".to_string(),
            headers: BTreeMap::new(),
        }
    }

    #[test]
    fn record_failure_writes_expected_shape_without_payload() {
        let dir = tempdir().unwrap();
        let envelope = envelope();
        let path = record_failure_to_dir(
            dir.path(),
            FailureContext {
                envelope: &envelope,
                envelope_id: Some("c-123-envelope"),
                failure_kind: FailureKind::InvalidSuccessJson,
                status_code: Some(200),
                error: Some("expected value at line 1 column 1"),
                response_body: Some("not json"),
                transport_error: None,
                daemon_url: "http://127.0.0.1:60887",
            },
        )
        .unwrap();

        let value: Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
        assert_eq!(value["schema_version"], 1);
        assert_eq!(value["ghook_version"], GHOOK_VERSION);
        assert_eq!(value["envelope_id"], "c-123-envelope");
        assert_eq!(value["hook_type"], "SessionStart");
        assert_eq!(value["source"], "codex");
        assert_eq!(value["critical"], true);
        assert_eq!(value["failure_kind"], "invalid_success_json");
        assert_eq!(value["status_code"], 200);
        assert_eq!(value["response_body_preview"], "not json");
        assert_eq!(value["response_body_truncated"], false);
        assert_eq!(value["response_body_length"], 8);
        assert_eq!(value["daemon_host"], "127.0.0.1");
        assert_eq!(value["daemon_port"], 60887);
        assert!(value.get("input_data").is_none());
    }

    #[test]
    fn record_failure_truncates_response_body_and_hashes_full_body() {
        let dir = tempdir().unwrap();
        let envelope = envelope();
        let body = "x".repeat(RESPONSE_BODY_MAX_BYTES + 10);
        let path = record_failure_to_dir(
            dir.path(),
            FailureContext {
                envelope: &envelope,
                envelope_id: None,
                failure_kind: FailureKind::Http,
                status_code: Some(500),
                error: None,
                response_body: Some(&body),
                transport_error: None,
                daemon_url: "http://localhost:60887",
            },
        )
        .unwrap();

        let value: Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
        assert_eq!(
            value["response_body_preview"].as_str().unwrap().len(),
            RESPONSE_BODY_MAX_BYTES
        );
        assert_eq!(value["response_body_truncated"], true);
        assert_eq!(value["response_body_length"], RESPONSE_BODY_MAX_BYTES + 10);
        assert!(
            value["response_body_hash"]
                .as_str()
                .unwrap()
                .starts_with("fnv1a64:")
        );
    }

    #[test]
    fn record_failure_uses_atomic_write_without_leftover_tmp_files() {
        let dir = tempdir().unwrap();
        let envelope = envelope();
        let path = record_failure_to_dir(
            &dir.path().join("nested"),
            FailureContext {
                envelope: &envelope,
                envelope_id: Some("env"),
                failure_kind: FailureKind::Connect,
                status_code: None,
                error: Some("connection refused"),
                response_body: None,
                transport_error: Some("connection refused"),
                daemon_url: "http://localhost:60887",
            },
        )
        .unwrap();

        assert!(path.exists());
        let tmp_files = fs::read_dir(path.parent().unwrap())
            .unwrap()
            .filter_map(Result::ok)
            .filter(|entry| entry.path().extension().and_then(|ext| ext.to_str()) == Some("tmp"))
            .count();
        assert_eq!(tmp_files, 0);
    }
}
