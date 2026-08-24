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
use gobby_core::local_token::{AUTHORIZATION_HEADER, authorization_bearer, read_local_cli_token};
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::Duration;

const POST_TIMEOUT: Duration = Duration::from_secs(30);
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
    /// "retry".
    pub fn is_retry_backpressure(&self) -> bool {
        self.status_code == Some(503)
            && self
                .response_body
                .as_deref()
                .and_then(|body| serde_json::from_str::<serde_json::Value>(body).ok())
                .is_some_and(|v| v.get("status").and_then(|s| s.as_str()) == Some("retry"))
    }
}

/// Compute the inbox directory (`~/.gobby/hooks/inbox/`).
pub fn inbox_dir() -> Result<PathBuf> {
    Ok(gobby_core::gobby_home()?.join("hooks").join("inbox"))
}

/// Compute the quarantine directory (`~/.gobby/hooks/inbox/quarantine/`).
pub fn quarantine_dir() -> Result<PathBuf> {
    Ok(inbox_dir()?.join("quarantine"))
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
    let name = envelope_filename(envelope.critical);
    let path = inbox.join(&name);
    let bytes = serde_json::to_vec_pretty(envelope)?;
    atomic_write(&path, &bytes)?;
    Ok(path)
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
        .timeout(POST_TIMEOUT)
        .set("Content-Type", "application/json");
    for (k, v) in &envelope.headers {
        if !k.eq_ignore_ascii_case(AUTHORIZATION_HEADER) {
            req = req.set(k, v);
        }
    }
    if let Ok(token) = read_local_cli_token() {
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
        Ok(resp) => DeliveryReport {
            outcome: DeliveryOutcome::Enqueued,
            failure_kind: Some(DeliveryFailureKind::Http),
            status_code: Some(resp.status()),
            response_body: resp.into_string().ok(),
            transport_error: None,
        },
        Err(ureq::Error::Status(code, resp)) => DeliveryReport {
            outcome: DeliveryOutcome::Enqueued,
            failure_kind: Some(DeliveryFailureKind::Http),
            status_code: Some(code),
            response_body: resp.into_string().ok(),
            transport_error: None,
        },
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
    let dir = quarantine_dir()?;
    quarantine_malformed_at(&dir, stdin_bytes, json_error, critical)
}

/// Write a malformed-stdin quarantine envelope into `dir`.
///
/// Writes two files atomically:
///   - `<stem>.json`       — body containing base64 of the raw stdin bytes.
///   - `<stem>.meta.json`  — sidecar with `reason`, `json_error`, `stdin_bytes_b64`.
///
/// The drain never replays quarantined envelopes — they surface via
/// `gobby status` / logs.
pub fn quarantine_malformed_at(
    dir: &Path,
    stdin_bytes: &[u8],
    json_error: &str,
    critical: bool,
) -> Result<PathBuf> {
    use base64::Engine;

    let prefix = if critical { 'c' } else { 'n' };
    let ts = ts13();
    let uuid = uuid::Uuid::new_v4();
    let stem = format!("{prefix}-{ts}-{uuid}");
    let body_path = dir.join(format!("{stem}.json"));
    let meta_path = dir.join(format!("{stem}.meta.json"));

    fs::create_dir_all(dir).with_context(|| format!("create_dir_all {}", dir.display()))?;

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
mod tests {
    use super::*;
    use crate::test_http::read_http_request;
    use std::collections::BTreeMap;
    use std::io::Write;
    use std::net::TcpListener;
    use std::thread;
    use tempfile::tempdir;

    #[test]
    fn ts13_is_13_digits() {
        let s = ts13();
        assert_eq!(s.len(), 13);
        assert!(s.chars().all(|c| c.is_ascii_digit()));
    }

    #[test]
    fn filename_prefix_reflects_critical() {
        assert!(envelope_filename(true).starts_with('c'));
        assert!(envelope_filename(false).starts_with('n'));
    }

    fn report_with(status_code: Option<u16>, response_body: Option<&str>) -> DeliveryReport {
        DeliveryReport {
            outcome: DeliveryOutcome::Enqueued,
            failure_kind: Some(DeliveryFailureKind::Http),
            status_code,
            response_body: response_body.map(str::to_string),
            transport_error: None,
        }
    }

    #[test]
    fn retry_backpressure_requires_503_and_retry_status_body() {
        assert!(report_with(Some(503), Some(r#"{"status":"retry"}"#)).is_retry_backpressure());
        // Extra fields alongside "status" are fine.
        assert!(
            report_with(
                Some(503),
                Some(r#"{"status":"retry","detail":"missing run id"}"#)
            )
            .is_retry_backpressure()
        );
    }

    #[test]
    fn retry_backpressure_rejects_non_retry_outcomes() {
        // 503 without the retry body keeps failure semantics.
        assert!(
            !report_with(Some(503), Some(r#"{"error":"unavailable"}"#)).is_retry_backpressure()
        );
        assert!(!report_with(Some(503), Some(r#"{"status":"error"}"#)).is_retry_backpressure());
        assert!(!report_with(Some(503), Some("not json")).is_retry_backpressure());
        assert!(!report_with(Some(503), None).is_retry_backpressure());
        // The retry body only counts on 503.
        assert!(!report_with(Some(500), Some(r#"{"status":"retry"}"#)).is_retry_backpressure());
        assert!(!report_with(None, Some(r#"{"status":"retry"}"#)).is_retry_backpressure());
    }

    #[test]
    fn atomic_write_creates_parent_dirs() {
        let dir = tempdir().unwrap();
        let nested = dir.path().join("a/b/c/out.json");
        atomic_write(&nested, b"{}").unwrap();
        assert!(nested.exists());
        assert_eq!(fs::read(&nested).unwrap(), b"{}");
    }

    #[test]
    fn atomic_write_leaves_no_tmp_on_success() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("ok.json");
        atomic_write(&path, b"{}").unwrap();
        let tmp = dir.path().join("ok.json.tmp");
        assert!(!tmp.exists());
    }

    #[test]
    fn atomic_write_removes_its_tmp_when_the_rename_fails() {
        // A non-empty directory cannot be replaced by a rename, which fails the
        // write with the tmp file already created, fsynced and closed.
        let dir = tempdir().unwrap();
        let occupied = dir.path().join("taken.json");
        fs::create_dir(&occupied).unwrap();
        fs::write(occupied.join("child"), b"x").unwrap();

        let err = atomic_write(&occupied, b"{}").unwrap_err();

        assert!(
            format!("{err:#}").contains("rename"),
            "unexpected failure: {err:#}"
        );
        assert!(
            !dir.path().join("taken.json.tmp").exists(),
            "a failed write left its temp file behind"
        );
    }

    /// Which step of the tmp write this fake fails at.
    #[derive(Clone, Copy)]
    enum FailAt {
        Write,
        Sync,
    }

    /// A real on-disk tmp file that fails one step of the write.
    ///
    /// Neither failure is reachable from the filesystem -- a write to a local
    /// file fails on a full disk or a signal, an fsync on a device error -- so
    /// the guard is proven at those two steps by substituting the tmp file
    /// rather than by breaking the filesystem under it. Creation still goes
    /// through `File::create`, so the entry the guard has to remove is a real
    /// one on the real path.
    struct FailingTempFile {
        file: File,
        fail_at: FailAt,
    }

    impl FailingTempFile {
        fn open(path: &Path, fail_at: FailAt) -> std::io::Result<Self> {
            Ok(Self {
                file: File::create(path)?,
                fail_at,
            })
        }
    }

    impl Write for FailingTempFile {
        fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
            match self.fail_at {
                FailAt::Write => Err(std::io::Error::other("simulated write failure")),
                FailAt::Sync => self.file.write(buf),
            }
        }

        fn flush(&mut self) -> std::io::Result<()> {
            self.file.flush()
        }
    }

    impl TempWrite for FailingTempFile {
        fn sync(&self) -> std::io::Result<()> {
            match self.fail_at {
                FailAt::Sync => Err(std::io::Error::other("simulated fsync failure")),
                FailAt::Write => self.file.sync_all(),
            }
        }
    }

    #[test]
    fn atomic_write_removes_its_tmp_when_the_write_fails() {
        let dir = tempdir().unwrap();
        let final_path = dir.path().join("half-written.json");

        let err = atomic_write_with(&final_path, b"{}", |tmp| {
            FailingTempFile::open(tmp, FailAt::Write)
        })
        .unwrap_err();

        assert!(
            format!("{err:#}").contains("write tmp"),
            "unexpected failure: {err:#}"
        );
        assert!(
            !dir.path().join("half-written.json.tmp").exists(),
            "a failed write left its temp file behind"
        );
        assert!(!final_path.exists());
    }

    #[test]
    fn atomic_write_removes_its_tmp_when_the_fsync_fails() {
        let dir = tempdir().unwrap();
        let final_path = dir.path().join("unsynced.json");

        let err = atomic_write_with(&final_path, b"{}", |tmp| {
            FailingTempFile::open(tmp, FailAt::Sync)
        })
        .unwrap_err();

        assert!(
            format!("{err:#}").contains("fsync tmp"),
            "unexpected failure: {err:#}"
        );
        assert!(
            !dir.path().join("unsynced.json.tmp").exists(),
            "a failed write left its temp file behind"
        );
        assert!(!final_path.exists());
    }

    #[test]
    fn atomic_write_leaves_an_entry_it_did_not_create() {
        // The create is what arms the cleanup. When it fails there is nothing
        // this write owns, and whatever is sitting on the tmp path is not ours.
        let dir = tempdir().unwrap();
        let squatter = dir.path().join("blocked.json.tmp");
        fs::create_dir(&squatter).unwrap();

        assert!(atomic_write(&dir.path().join("blocked.json"), b"{}").is_err());
        assert!(
            squatter.is_dir(),
            "the cleanup removed an entry it did not create"
        );
    }

    #[test]
    fn enqueue_writes_envelope_to_inbox() {
        let dir = tempdir().unwrap();
        let env = Envelope::new(
            true,
            "session-start".into(),
            serde_json::json!({"session_id":"s"}),
            "claude".into(),
            BTreeMap::new(),
        );
        let path = enqueue_to(&env, dir.path()).unwrap();
        assert!(path.exists());
        let name = path.file_name().unwrap().to_str().unwrap();
        assert!(name.starts_with('c'));
        assert!(name.ends_with(".json"));
        assert!(!name.ends_with(".tmp.json"));
    }

    #[test]
    fn quarantine_writes_pair() {
        let dir = tempdir().unwrap();
        let body =
            quarantine_malformed_at(dir.path(), b"not json", "expected value", false).unwrap();
        let stem = body.file_stem().unwrap().to_str().unwrap().to_owned();
        let meta = body.with_file_name(format!("{stem}.meta.json"));
        assert!(body.exists());
        assert!(meta.exists());
        let meta_val: serde_json::Value =
            serde_json::from_slice(&fs::read(&meta).unwrap()).unwrap();
        assert_eq!(meta_val["reason"], "malformed_stdin");
        assert_eq!(meta_val["json_error"], "expected value");
        assert!(meta_val["stdin_bytes_b64"].is_string());
    }

    #[test]
    fn post_and_cleanup_captures_success_response_body() {
        let dir = tempdir().unwrap();
        let inbox = dir.path().join("inbox");
        let mut headers = BTreeMap::new();
        headers.insert("X-Gobby-Session-Id".to_string(), "s".to_string());
        let envelope = Envelope::new(
            false,
            "SessionStart".into(),
            serde_json::json!({"session_id":"s"}),
            "codex".into(),
            headers,
        );
        let path = enqueue_to(&envelope, &inbox).unwrap();
        let envelope_id = path
            .file_name()
            .and_then(|name| name.to_str())
            .and_then(|name| name.strip_suffix(".json"))
            .unwrap()
            .to_string();

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let request = read_http_request(&mut stream);
            assert!(request.contains("POST /api/hooks/execute HTTP/1.1"));
            assert!(request.contains(&format!("{ENVELOPE_ID_HEADER}: {envelope_id}")));
            assert!(request.contains("X-Gobby-Session-Id: s"));
            assert!(request.contains("\"hook_type\":\"SessionStart\""));
            assert!(request.contains("\"input_data\":{\"session_id\":\"s\"}"));
            assert!(request.contains("\"source\":\"codex\""));
            assert!(request.contains("\"schema_version\":1"));
            assert!(request.contains("\"enqueued_at\""));
            assert!(request.contains("\"critical\":false"));
            assert!(request.contains("\"headers\":{\"X-Gobby-Session-Id\":\"s\"}"));
            stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 35\r\n\r\n{\"decision\":\"accept\",\"reason\":\"ok\"}",
                )
                .unwrap();
        });

        let report = post_and_cleanup(&envelope, &path, &format!("http://{addr}"));
        handle.join().unwrap();

        assert_eq!(report.outcome, DeliveryOutcome::Delivered);
        assert_eq!(report.failure_kind, None);
        assert_eq!(report.status_code, Some(200));
        assert_eq!(
            report.response_body,
            Some("{\"decision\":\"accept\",\"reason\":\"ok\"}".to_string())
        );
        assert!(path.exists());
    }

    #[test]
    fn post_and_cleanup_sends_droid_source_to_unified_hooks_endpoint() {
        let dir = tempdir().unwrap();
        let inbox = dir.path().join("inbox");
        let envelope = Envelope::new(
            false,
            "PreToolUse".into(),
            serde_json::json!({
                "session_id": "droid-session",
                "hook_event_name": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": "src/main.rs"}
            }),
            "droid".into(),
            BTreeMap::new(),
        );
        let path = enqueue_to(&envelope, &inbox).unwrap();
        let envelope_id = path
            .file_name()
            .and_then(|name| name.to_str())
            .and_then(|name| name.strip_suffix(".json"))
            .unwrap()
            .to_string();

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let request = read_http_request(&mut stream);
            assert!(request.contains("POST /api/hooks/execute HTTP/1.1"));
            assert!(request.contains(&format!("{ENVELOPE_ID_HEADER}: {envelope_id}")));
            assert!(request.contains("\"hook_type\":\"PreToolUse\""));
            assert!(request.contains("\"source\":\"droid\""));
            assert!(request.contains("\"schema_version\":1"));
            assert!(request.contains("\"critical\":false"));
            assert!(request.contains("\"input_data\":{\"hook_event_name\":\"PreToolUse\""));
            assert!(request.contains("\"tool_input\":{\"file_path\":\"src/main.rs\"}"));
            stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}",
                )
                .unwrap();
        });

        let report = post_and_cleanup(&envelope, &path, &format!("http://{addr}"));
        handle.join().unwrap();

        assert_eq!(report.outcome, DeliveryOutcome::Delivered);
        assert_eq!(report.failure_kind, None);
        assert_eq!(report.status_code, Some(200));
        assert_eq!(report.response_body, Some("{}".to_string()));
        assert!(path.exists());
    }

    #[test]
    fn post_includes_bearer_when_token_present() {
        let home = tempdir().unwrap();
        fs::write(home.path().join("local_cli_token"), "ghook-test-token\n").unwrap();

        temp_env::with_var("GOBBY_HOME", Some(home.path()), || {
            let inbox = home.path().join("inbox");
            let mut headers = BTreeMap::new();
            headers.insert(
                AUTHORIZATION_HEADER.to_string(),
                "Bearer stale-token".to_string(),
            );
            let envelope = Envelope::new(
                false,
                "PreToolUse".into(),
                serde_json::json!({"session_id": "auth-test"}),
                "codex".into(),
                headers,
            );
            let path = enqueue_to(&envelope, &inbox).unwrap();
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let addr = listener.local_addr().unwrap();
            let handle = thread::spawn(move || {
                let (mut stream, _) = listener.accept().unwrap();
                let request = read_http_request(&mut stream);
                assert!(request.lines().any(|line| {
                    line.eq_ignore_ascii_case("Authorization: Bearer ghook-test-token")
                }));
                let request_headers = request.split("\r\n\r\n").next().unwrap();
                assert!(!request_headers.contains("Bearer stale-token"));
                stream
                    .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
                    .unwrap();
            });

            let report = post_and_cleanup(&envelope, &path, &format!("http://{addr}"));
            handle.join().unwrap();
            assert_eq!(report.outcome, DeliveryOutcome::Delivered);
        });
    }

    #[test]
    fn post_omits_authorization_when_token_missing() {
        let home = tempdir().unwrap();

        temp_env::with_var("GOBBY_HOME", Some(home.path()), || {
            let inbox = home.path().join("inbox");
            let mut headers = BTreeMap::new();
            headers.insert(
                AUTHORIZATION_HEADER.to_string(),
                "Bearer stale-token".to_string(),
            );
            let envelope = Envelope::new(
                false,
                "PreToolUse".into(),
                serde_json::json!({"session_id": "anonymous-test"}),
                "codex".into(),
                headers,
            );
            let path = enqueue_to(&envelope, &inbox).unwrap();
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let addr = listener.local_addr().unwrap();
            let handle = thread::spawn(move || {
                let (mut stream, _) = listener.accept().unwrap();
                let request = read_http_request(&mut stream);
                let request_headers = request.split("\r\n\r\n").next().unwrap();
                assert!(
                    !request_headers
                        .lines()
                        .any(|line| line.to_ascii_lowercase().starts_with("authorization:"))
                );
                stream
                    .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
                    .unwrap();
            });

            let report = post_and_cleanup(&envelope, &path, &format!("http://{addr}"));
            handle.join().unwrap();
            assert_eq!(report.outcome, DeliveryOutcome::Delivered);
        });
    }

    #[test]
    fn post_and_cleanup_captures_http_error_body() {
        let dir = tempdir().unwrap();
        let inbox = dir.path().join("inbox");
        let envelope = Envelope::new(
            true,
            "Stop".into(),
            serde_json::json!({"session_id":"s"}),
            "codex".into(),
            BTreeMap::new(),
        );
        let path = enqueue_to(&envelope, &inbox).unwrap();

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let _ = read_http_request(&mut stream);
            stream
                .write_all(
                    b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: text/plain\r\nContent-Length: 4\r\n\r\nnope",
                )
                .unwrap();
        });

        let report = post_and_cleanup(&envelope, &path, &format!("http://{addr}"));
        handle.join().unwrap();

        assert_eq!(report.outcome, DeliveryOutcome::Enqueued);
        assert_eq!(report.failure_kind, Some(DeliveryFailureKind::Http));
        assert_eq!(report.status_code, Some(500));
        assert_eq!(report.response_body, Some("nope".to_string()));
        assert!(path.exists());
    }

    #[test]
    fn classify_transport_error_text_recognizes_connect_and_timeout_failures() {
        assert_eq!(
            classify_transport_error_text("Connection refused (os error 61)"),
            DeliveryFailureKind::Connect
        );
        assert_eq!(
            classify_transport_error_text("request timed out while posting hook"),
            DeliveryFailureKind::Timeout
        );
        assert_eq!(
            classify_transport_error_text("TLS certificate is invalid"),
            DeliveryFailureKind::Other
        );
    }
}
