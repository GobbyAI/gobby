//! Enqueue-first transport.
//!
//! Every invocation of `ghook --gobby-owned` writes an envelope to
//! `~/.gobby/hooks/inbox/<p>-<ts13>-<uuid>.json` (atomic `tmp` → `fsync` →
//! rename) *before* attempting the daemon POST. On 2xx we delete the
//! inbox file; on any other outcome (timeout, connection refused, 5xx) we
//! leave it for the daemon's drain worker to replay.
//!
//! Filename shape (see plan Q4):
//!   `<prefix>-<ts13>-<uuid>.json`
//!     prefix = 'c' (critical) | 'n' (non-critical)
//!     ts13   = 13-digit zero-padded milliseconds since epoch (lex-sort)
//!     uuid   = random v4
//! `.tmp` suffix on the intermediate write — never a valid replay target.

use crate::envelope::Envelope;
use anyhow::{Context, Result};
use gobby_core::local_token::{AUTHORIZATION_HEADER, authorization_bearer, read_api_key};
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::Duration;

const POST_TIMEOUT: Duration = Duration::from_secs(30);
const PERMISSION_REQUEST_POST_TIMEOUT: Duration = Duration::from_secs(115);

fn post_timeout(hook_type: &str) -> Duration {
    match hook_type {
        "permission-request" => PERMISSION_REQUEST_POST_TIMEOUT,
        _ => POST_TIMEOUT,
    }
}
const HOOKS_ENDPOINT: &str = "/api/hooks/execute";
const ENVELOPE_ID_HEADER: &str = "X-Gobby-Envelope-Id";

/// Result of `enqueue_and_post` — the main CLI needs to know whether the
/// daemon ACKed (caller parses the body before deleting the inbox file) or not
/// (keep the file; the drain will handle it).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeliveryOutcome {
    /// Daemon returned 2xx; caller owns cleanup after response parsing.
    Delivered,
    /// Daemon did not 2xx — inbox file persists for drain replay.
    Enqueued,
}

/// Classification of a failed live daemon POST.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeliveryFailureKind {
    Http,
    Connect,
    Timeout,
    Other,
}

/// Delivery details from the live daemon POST attempt.
#[derive(Debug, PartialEq, Eq)]
pub struct DeliveryReport {
    pub outcome: DeliveryOutcome,
    pub failure_kind: Option<DeliveryFailureKind>,
    pub status_code: Option<u16>,
    pub response_body: Option<String>,
    pub transport_error: Option<String>,
}

impl DeliveryReport {
    /// True when the daemon deliberately deferred ingestion: HTTP 503 with a
    /// JSON body whose `status` field is `"retry"` (the hook-ingress gate's
    /// retryable signal). The envelope stays in the inbox for drain replay and
    /// the host CLI must be allowed to continue — blocking on this outcome
    /// would live-lock critical hooks against a daemon that keeps saying
    /// "retry". `adapter_timeout` is a different retry class and is excluded.
    pub fn is_retry_backpressure(&self) -> bool {
        self.retry_payload().is_some() && !self.is_adapter_timeout()
    }

    /// Adapter evaluation timed out. Unlike ingress backpressure, this honors
    /// the 2.3 criticality action: critical hooks fail closed; AGY and other
    /// noncritical hooks continue with the host-legal skip body.
    pub fn is_adapter_timeout(&self) -> bool {
        self.retry_kind().as_deref() == Some("adapter_timeout")
    }

    fn retry_payload(&self) -> Option<serde_json::Value> {
        if self.status_code != Some(503) {
            return None;
        }
        let value =
            serde_json::from_str::<serde_json::Value>(self.response_body.as_deref()?).ok()?;
        if value.get("status").and_then(|s| s.as_str()) == Some("retry") {
            Some(value)
        } else {
            None
        }
    }

    fn retry_kind(&self) -> Option<String> {
        self.retry_payload()?
            .get("retry_kind")?
            .as_str()
            .map(str::to_owned)
    }
}

/// Compute the inbox directory (`~/.gobby/hooks/inbox/`).
pub fn inbox_dir() -> Result<PathBuf> {
    Ok(gobby_core::gobby_home()?.join("hooks").join("inbox"))
}

/// Zero-padded 13-digit ms-since-epoch timestamp for lex-sortable filenames.
pub fn ts13() -> String {
    let ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    format!("{ms:013}")
}

/// Build the envelope filename for the given critical flag.
pub fn envelope_filename(critical: bool) -> String {
    let prefix = if critical { 'c' } else { 'n' };
    let uuid = uuid::Uuid::new_v4();
    format!("{prefix}-{}-{uuid}.json", ts13())
}

/// Removes a temp file unless the write that created it completed.
///
/// The drain ignores `*.tmp` and nothing else reaps one, so a bare `?` between
/// the create and the rename leaked a file that lived forever: 59 had built up
/// in one inbox, the oldest four months old (#20854). Armed only after the
/// create succeeds, so it never removes an entry this write did not make.
struct TempFileGuard<'a> {
    path: &'a Path,
    armed: bool,
}

impl<'a> TempFileGuard<'a> {
    fn arm(path: &'a Path) -> Self {
        Self { path, armed: true }
    }

    fn disarm(&mut self) {
        self.armed = false;
    }
}

impl Drop for TempFileGuard<'_> {
    fn drop(&mut self) {
        if self.armed {
            // Best-effort: the write is already failing and the reaper on the
            // daemon side covers whatever a dying process leaves behind.
            let _ = fs::remove_file(self.path);
        }
    }
}

/// The tmp file [`atomic_write`] fills before the rename.
///
/// `File::sync_all` is an inherent method, so the fsync step needs a name of
/// its own for the write to be generic over its temp file. Production always
/// uses a real `File`; tests substitute one that fails a chosen step.
trait TempWrite: Write {
    fn sync(&self) -> std::io::Result<()>;
}

impl TempWrite for File {
    fn sync(&self) -> std::io::Result<()> {
        self.sync_all()
    }
}

/// Atomically write `bytes` to `final_path` via tmp + fsync + rename.
///
/// Creates the parent directory if missing. The tmp file lives next to
/// the final path with a `.tmp` suffix — the drain ignores `*.tmp` — and is
/// removed again if any step after its creation fails.
pub fn atomic_write(final_path: &Path, bytes: &[u8]) -> Result<()> {
    atomic_write_with(final_path, bytes, |tmp: &Path| File::create(tmp))
}

/// [`atomic_write`] over a caller-supplied temp file.
///
/// The seam exists for the failure modes: a write or an fsync to a local file
/// fails on a full disk, a signal, or a device error, none of which a test can
/// arrange, and #20854 needs the cleanup proven at each of them.
fn atomic_write_with<F: TempWrite>(
    final_path: &Path,
    bytes: &[u8],
    open_tmp: impl FnOnce(&Path) -> std::io::Result<F>,
) -> Result<()> {
    if let Some(parent) = final_path.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("create_dir_all {}", parent.display()))?;
    }
    let mut tmp = final_path.to_path_buf();
    let mut name = tmp
        .file_name()
        .context("final path has no file name")?
        .to_owned();
    name.push(".tmp");
    tmp.set_file_name(name);

    let mut file = open_tmp(&tmp).with_context(|| format!("create tmp {}", tmp.display()))?;
    let mut guard = TempFileGuard::arm(&tmp);
    file.write_all(bytes)
        .with_context(|| format!("write tmp {}", tmp.display()))?;
    file.sync()
        .with_context(|| format!("fsync tmp {}", tmp.display()))?;
    drop(file);
    fs::rename(&tmp, final_path)
        .with_context(|| format!("rename {} -> {}", tmp.display(), final_path.display()))?;
    guard.disarm();
    if let Some(parent) = final_path.parent() {
        // Directory fsync is best-effort because some platforms/filesystems reject it.
        let _ = File::open(parent).and_then(|dir| dir.sync_all());
    }
    Ok(())
}

/// Serialize `envelope` to the given inbox directory and return the path.
///
/// Caller can then call [`post_and_cleanup`] to attempt delivery.
pub fn enqueue_to(envelope: &Envelope, inbox: &Path) -> Result<PathBuf> {
    ensure_owner_only_dir(inbox)?;
    let name = envelope_filename(envelope.critical);
    let path = inbox.join(&name);
    let bytes = serde_json::to_vec_pretty(envelope)?;
    atomic_write(&path, &bytes)?;
    Ok(path)
}

/// Protect hook payloads before writing beneath this directory.
#[cfg(unix)]
fn ensure_owner_only_dir(dir: &Path) -> Result<()> {
    use std::os::unix::fs::{DirBuilderExt, PermissionsExt};

    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(dir)
        .with_context(|| format!("create_dir_all {}", dir.display()))?;
    let permissions = fs::metadata(dir)
        .with_context(|| format!("read directory permissions {}", dir.display()))?
        .permissions();
    if permissions.mode() & 0o7777 != 0o700 {
        fs::set_permissions(dir, fs::Permissions::from_mode(0o700))
            .with_context(|| format!("set owner-only directory permissions {}", dir.display()))?;
    }
    Ok(())
}

#[cfg(not(unix))]
fn ensure_owner_only_dir(_dir: &Path) -> Result<()> {
    Ok(())
}

pub(crate) fn envelope_id_from_path(enqueued_path: &Path) -> Option<&str> {
    enqueued_path.file_stem()?.to_str()
}

/// POST the current hook envelope to the daemon. On 2xx, return `Delivered`
/// without deleting the inbox file. On any other outcome, leave the file and return
/// `Enqueued`.
///
/// `daemon_url` is the base URL (e.g. `http://127.0.0.1:60887`). The
/// endpoint path is appended here.
pub fn post_and_cleanup(
    envelope: &Envelope,
    enqueued_path: &Path,
    daemon_url: &str,
) -> DeliveryReport {
    let endpoint = format!("{daemon_url}{HOOKS_ENDPOINT}");
    let mut req = ureq::post(&endpoint)
        .timeout(post_timeout(&envelope.hook_type))
        .set("Content-Type", "application/json");
    for (k, v) in &envelope.headers {
        if !k.eq_ignore_ascii_case(AUTHORIZATION_HEADER) {
            req = req.set(k, v);
        }
    }
    if let Ok(token) = read_api_key() {
        req = req.set(AUTHORIZATION_HEADER, &authorization_bearer(&token));
    }
    if let Some(envelope_id) = envelope_id_from_path(enqueued_path) {
        req = req.set(ENVELOPE_ID_HEADER, envelope_id);
    }

    let body = match serde_json::to_string(envelope) {
        Ok(s) => s,
        Err(e) => {
            return DeliveryReport {
                outcome: DeliveryOutcome::Enqueued,
                failure_kind: Some(DeliveryFailureKind::Other),
                status_code: None,
                response_body: None,
                transport_error: Some(e.to_string()),
            };
        }
    };

    match req.send_string(&body) {
        Ok(resp) if (200..300).contains(&resp.status()) => {
            let status_code = Some(resp.status());
            let response_body = resp.into_string().ok();
            DeliveryReport {
                outcome: DeliveryOutcome::Delivered,
                failure_kind: None,
                status_code,
                response_body,
                transport_error: None,
            }
        }
        Ok(resp) => http_failure_report(resp.status(), resp.into_string().ok()),
        Err(ureq::Error::Status(code, resp)) => http_failure_report(code, resp.into_string().ok()),
        Err(ureq::Error::Transport(err)) => {
            let transport_error = err.to_string();
            DeliveryReport {
                outcome: DeliveryOutcome::Enqueued,
                failure_kind: Some(classify_transport_error(&err, &transport_error)),
                status_code: None,
                response_body: None,
                transport_error: Some(transport_error),
            }
        }
    }
}

fn http_failure_report(status_code: u16, response_body: Option<String>) -> DeliveryReport {
    DeliveryReport {
        outcome: DeliveryOutcome::Enqueued,
        failure_kind: Some(classify_http_failure(status_code, response_body.as_deref())),
        status_code: Some(status_code),
        response_body,
        transport_error: None,
    }
}

/// The gdaemon front door answers `503 {"status":"unavailable",...}` while the Python
/// backend is down or starting; that is the daemon being unreachable, not an HTTP error.
fn classify_http_failure(status_code: u16, response_body: Option<&str>) -> DeliveryFailureKind {
    let typed_unavailable = status_code == 503
        && response_body
            .and_then(|body| serde_json::from_str::<serde_json::Value>(body).ok())
            .is_some_and(|value| {
                value.get("status").and_then(|s| s.as_str()) == Some("unavailable")
            });
    if typed_unavailable {
        DeliveryFailureKind::Connect
    } else {
        DeliveryFailureKind::Http
    }
}

fn classify_transport_error(err: &ureq::Transport, error_text: &str) -> DeliveryFailureKind {
    use ureq::ErrorKind;

    if matches!(
        err.kind(),
        ErrorKind::ConnectionFailed | ErrorKind::Dns | ErrorKind::ProxyConnect
    ) {
        return DeliveryFailureKind::Connect;
    }

    classify_transport_error_text(error_text)
}

fn classify_transport_error_text(error_text: &str) -> DeliveryFailureKind {
    let error_text = error_text.to_ascii_lowercase();
    if error_text.contains("timed out") || error_text.contains("timeout") {
        return DeliveryFailureKind::Timeout;
    }
    if [
        "connection refused",
        "connection reset",
        "connection aborted",
        "connection failed",
        "failed to connect",
        "could not connect",
        "tcp connect error",
        "dns error",
        "nodename nor servname",
    ]
    .iter()
    .any(|pattern| error_text.contains(pattern))
    {
        return DeliveryFailureKind::Connect;
    }

    DeliveryFailureKind::Other
}

/// Quarantine malformed stdin under the default `~/.gobby/hooks/inbox/quarantine/`.
/// Errors if the home directory cannot be resolved.
pub fn quarantine_malformed(
    stdin_bytes: &[u8],
    json_error: &str,
    critical: bool,
) -> Result<PathBuf> {
    let inbox = inbox_dir()?;
    quarantine_malformed_at(&inbox, stdin_bytes, json_error, critical)
}

/// Write a malformed-stdin quarantine envelope into `inbox/quarantine`.
///
/// Writes two files atomically:
///   - `<stem>.json`       — body containing base64 of the raw stdin bytes.
///   - `<stem>.meta.json`  — sidecar with `reason`, `json_error`, `stdin_bytes_b64`.
///
/// The drain never replays quarantined envelopes — they surface via
/// `gobby status` / logs.
pub fn quarantine_malformed_at(
    inbox: &Path,
    stdin_bytes: &[u8],
    json_error: &str,
    critical: bool,
) -> Result<PathBuf> {
    use base64::Engine;

    ensure_owner_only_dir(inbox)?;
    let dir = inbox.join("quarantine");
    ensure_owner_only_dir(&dir)?;

    let prefix = if critical { 'c' } else { 'n' };
    let ts = ts13();
    let uuid = uuid::Uuid::new_v4();
    let stem = format!("{prefix}-{ts}-{uuid}");
    let body_path = dir.join(format!("{stem}.json"));
    let meta_path = dir.join(format!("{stem}.meta.json"));

    let b64 = base64::engine::general_purpose::STANDARD.encode(stdin_bytes);
    let body = serde_json::json!({
        "quarantined": true,
        "stdin_bytes_b64": b64,
    });
    atomic_write(&body_path, &serde_json::to_vec_pretty(&body)?)?;

    let meta = serde_json::json!({
        "reason": "malformed_stdin",
        "json_error": json_error,
        "stdin_bytes_b64": b64,
    });
    atomic_write(&meta_path, &serde_json::to_vec_pretty(&meta)?)?;
    Ok(body_path)
}

#[cfg(test)]
#[path = "transport/tests.rs"]
mod tests;
