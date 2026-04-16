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
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::Duration;

const POST_TIMEOUT: Duration = Duration::from_secs(30);
const HOOKS_ENDPOINT: &str = "/api/hooks/execute";

/// Result of `enqueue_and_post` — the main CLI needs to know whether the
/// daemon ACKed (delete the inbox file and return early) or not (keep the
/// file; the drain will handle it).
#[derive(Debug)]
pub enum DeliveryOutcome {
    /// Daemon returned 2xx — inbox file already deleted.
    Delivered,
    /// Daemon did not 2xx — inbox file persists for drain replay.
    Enqueued,
}

/// Compute the inbox directory (`~/.gobby/hooks/inbox/`).
pub fn inbox_dir() -> Result<PathBuf> {
    let home = dirs::home_dir().context("no home directory")?;
    Ok(home.join(".gobby").join("hooks").join("inbox"))
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

/// Atomically write `bytes` to `final_path` via tmp + fsync + rename.
///
/// Creates the parent directory if missing. The tmp file lives next to
/// the final path with a `.tmp` suffix — the drain ignores `*.tmp`.
pub fn atomic_write(final_path: &Path, bytes: &[u8]) -> Result<()> {
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

    {
        let mut f = File::create(&tmp).with_context(|| format!("create tmp {}", tmp.display()))?;
        f.write_all(bytes)
            .with_context(|| format!("write tmp {}", tmp.display()))?;
        f.sync_all()
            .with_context(|| format!("fsync tmp {}", tmp.display()))?;
    }
    fs::rename(&tmp, final_path)
        .with_context(|| format!("rename {} -> {}", tmp.display(), final_path.display()))?;
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

/// POST the envelope to the daemon. On 2xx, delete the inbox file and
/// return `Delivered`. On any other outcome, leave the file and return
/// `Enqueued`.
///
/// `daemon_url` is the base URL (e.g. `http://127.0.0.1:60887`). The
/// endpoint path is appended here.
pub fn post_and_cleanup(
    envelope: &Envelope,
    enqueued_path: &Path,
    daemon_url: &str,
) -> DeliveryOutcome {
    let endpoint = format!("{daemon_url}{HOOKS_ENDPOINT}");
    let mut req = ureq::post(&endpoint)
        .timeout(POST_TIMEOUT)
        .set("Content-Type", "application/json");
    for (k, v) in &envelope.headers {
        req = req.set(k, v);
    }

    let body = match serde_json::to_string(envelope) {
        Ok(s) => s,
        Err(_) => return DeliveryOutcome::Enqueued,
    };

    match req.send_string(&body) {
        Ok(resp) if (200..300).contains(&resp.status()) => {
            let _ = fs::remove_file(enqueued_path);
            DeliveryOutcome::Delivered
        }
        _ => DeliveryOutcome::Enqueued,
    }
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
    use std::collections::BTreeMap;
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
}
