//! Planned daemon shutdown handling for Stop hooks.
//!
//! During intentional daemon stop/restart windows, the Stop hook may fire after
//! the daemon has already gone away. In that case blocking the host CLI is
//! wrong: the shutdown was requested by Gobby itself. This module recognizes
//! the daemon's short-lived shutdown markers and suppresses only daemon
//! unreachable races for Stop hooks.

use crate::transport::DeliveryFailureKind;
use serde_json::Value;
use std::io;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const DEFAULT_ALLOW_SECONDS: f64 = 120.0;
const HEALTH_TIMEOUT: Duration = Duration::from_millis(350);
const HEALTH_ENDPOINT: &str = "/api/admin/health";
const ACTIVE_MARKER: &str = "shutdown_intent_active.json";
const LEGACY_MARKER: &str = "shutdown_source.json";
const ALLOWED_SOURCES: [&str; 4] = ["cli_", "http_", "service_", "mcp_"];

pub fn should_continue_before_dispatch(hook_type: &str) -> bool {
    should_continue_before_dispatch_with(
        hook_type,
        || marker_home().is_some_and(|home| fresh_shutdown_marker(&home)),
        || daemon_is_reachable(&daemon_url()),
    )
}

pub fn suppress_after_failed_post(
    hook_type: &str,
    failure_kind: Option<DeliveryFailureKind>,
    enqueued_path: &Path,
) -> bool {
    suppress_after_failed_post_with_marker(hook_type, failure_kind, enqueued_path, || {
        marker_home().is_some_and(|home| fresh_shutdown_marker(&home))
    })
}

fn suppress_after_failed_post_with_marker(
    hook_type: &str,
    failure_kind: Option<DeliveryFailureKind>,
    enqueued_path: &Path,
    marker_active: impl FnOnce() -> bool,
) -> bool {
    if !should_suppress_failed_post(hook_type, failure_kind, marker_active) {
        return false;
    }

    delete_enqueued(enqueued_path)
}

pub fn daemon_url() -> String {
    daemon_url_from_env(std::env::var("GOBBY_DAEMON_URL").ok().as_deref(), || {
        gobby_core::daemon_url::daemon_url()
    })
}

fn daemon_url_from_env(value: Option<&str>, fallback: impl FnOnce() -> String) -> String {
    match value {
        Some(value) if !value.is_empty() => value.to_string(),
        _ => fallback(),
    }
}

pub fn is_stop_hook(hook_type: &str) -> bool {
    hook_type.eq_ignore_ascii_case("stop")
}

fn should_continue_before_dispatch_with(
    hook_type: &str,
    marker_active: impl FnOnce() -> bool,
    daemon_reachable: impl FnOnce() -> bool,
) -> bool {
    is_stop_hook(hook_type) && marker_active() && !daemon_reachable()
}

fn should_suppress_failed_post(
    hook_type: &str,
    failure_kind: Option<DeliveryFailureKind>,
    marker_active: impl FnOnce() -> bool,
) -> bool {
    is_stop_hook(hook_type)
        && matches!(
            failure_kind,
            Some(DeliveryFailureKind::Connect | DeliveryFailureKind::Timeout)
        )
        && marker_active()
}

fn fresh_shutdown_marker(home: &Path) -> bool {
    fresh_shutdown_marker_at(home, now_secs(), allow_seconds())
}

fn fresh_shutdown_marker_at(home: &Path, now: f64, allow_seconds: f64) -> bool {
    [ACTIVE_MARKER, LEGACY_MARKER]
        .into_iter()
        .filter_map(|name| read_marker(&home.join(name)))
        .any(|marker| marker_is_allowed_and_fresh(&marker, now, allow_seconds))
}

fn marker_is_allowed_and_fresh(marker: &Value, now: f64, allow_seconds: f64) -> bool {
    let timestamp = marker.get("timestamp").and_then(optional_float);
    let Some(timestamp) = timestamp else {
        return false;
    };

    if now - timestamp > allow_seconds {
        return false;
    }

    let intent = marker
        .get("intent")
        .and_then(value_as_text)
        .unwrap_or_default()
        .to_ascii_lowercase();
    if matches!(intent.as_str(), "stop" | "restart") {
        return true;
    }

    let source = marker
        .get("source")
        .and_then(value_as_text)
        .unwrap_or_default()
        .to_ascii_lowercase();
    ALLOWED_SOURCES
        .into_iter()
        .any(|prefix| source.starts_with(prefix))
}

fn read_marker(path: &Path) -> Option<Value> {
    let bytes = std::fs::read(path).ok()?;
    let value: Value = serde_json::from_slice(&bytes).ok()?;
    value.is_object().then_some(value)
}

fn daemon_is_reachable(daemon_url: &str) -> bool {
    let endpoint = format!("{}{}", daemon_url.trim_end_matches('/'), HEALTH_ENDPOINT);
    match ureq::get(&endpoint).timeout(HEALTH_TIMEOUT).call() {
        Ok(_) | Err(ureq::Error::Status(_, _)) => true,
        Err(ureq::Error::Transport(_)) => false,
    }
}

fn marker_home() -> Option<PathBuf> {
    if let Some(home) = std::env::var_os("GOBBY_HOME")
        && !home.is_empty()
    {
        return Some(PathBuf::from(home));
    }

    dirs::home_dir().map(|home| home.join(".gobby"))
}

fn allow_seconds() -> f64 {
    allow_seconds_from_env(
        std::env::var("GOBBY_SHUTDOWN_HOOK_ALLOW_SECONDS")
            .ok()
            .as_deref(),
    )
}

fn allow_seconds_from_env(value: Option<&str>) -> f64 {
    let Some(value) = value else {
        return DEFAULT_ALLOW_SECONDS;
    };
    match value.parse::<f64>() {
        Ok(seconds) if seconds.is_finite() && seconds > 0.0 => seconds,
        _ => DEFAULT_ALLOW_SECONDS,
    }
}

fn optional_float(value: &Value) -> Option<f64> {
    match value {
        Value::Number(number) => number.as_f64(),
        Value::String(text) => text.parse::<f64>().ok(),
        _ => None,
    }
}

fn value_as_text(value: &Value) -> Option<String> {
    match value {
        Value::String(text) => Some(text.to_string()),
        Value::Number(number) => Some(number.to_string()),
        Value::Bool(flag) => Some(flag.to_string()),
        _ => None,
    }
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

fn delete_enqueued(enqueued_path: &Path) -> bool {
    match std::fs::remove_file(enqueued_path) {
        Ok(()) => true,
        Err(err) if err.kind() == io::ErrorKind::NotFound => true,
        Err(_) => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;
    use tempfile::tempdir;

    fn write_marker(home: &Path, name: &str, value: Value) {
        std::fs::create_dir_all(home).unwrap();
        std::fs::write(home.join(name), serde_json::to_vec(&value).unwrap()).unwrap();
    }

    #[test]
    fn stop_hook_matching_is_case_insensitive() {
        assert!(is_stop_hook("stop"));
        assert!(is_stop_hook("Stop"));
        assert!(is_stop_hook("STOP"));
        assert!(!is_stop_hook("SubagentStop"));
    }

    #[test]
    fn marker_accepts_fresh_allowed_intents() {
        let dir = tempdir().unwrap();
        let now = 1_000.0;
        write_marker(
            dir.path(),
            ACTIVE_MARKER,
            json!({"source": "unknown", "intent": "restart", "timestamp": now - 10.0}),
        );

        assert!(fresh_shutdown_marker_at(dir.path(), now, 120.0));
    }

    #[test]
    fn marker_accepts_allowed_source_prefixes() {
        let now = 1_000.0;
        for source in ["cli_stop", "http_shutdown", "service_restart", "mcp_stop"] {
            let dir = tempdir().unwrap();
            write_marker(
                dir.path(),
                ACTIVE_MARKER,
                json!({"source": source, "timestamp": now - 10.0}),
            );

            assert!(
                fresh_shutdown_marker_at(dir.path(), now, 120.0),
                "{source} should be allowed"
            );
        }
    }

    #[test]
    fn marker_checks_active_then_legacy_marker() {
        let dir = tempdir().unwrap();
        let now = 1_000.0;
        write_marker(
            dir.path(),
            ACTIVE_MARKER,
            json!({"source": "unknown", "timestamp": now - 10.0}),
        );
        write_marker(
            dir.path(),
            LEGACY_MARKER,
            json!({"source": "cli_stop", "timestamp": now - 10.0}),
        );

        assert!(fresh_shutdown_marker_at(dir.path(), now, 120.0));
    }

    #[test]
    fn marker_rejects_stale_missing_invalid_and_disallowed_markers() {
        let now = 1_000.0;

        let stale = tempdir().unwrap();
        write_marker(
            stale.path(),
            ACTIVE_MARKER,
            json!({"source": "cli_stop", "timestamp": now - 121.0}),
        );
        assert!(!fresh_shutdown_marker_at(stale.path(), now, 120.0));

        let missing_timestamp = tempdir().unwrap();
        write_marker(
            missing_timestamp.path(),
            ACTIVE_MARKER,
            json!({"source": "cli_stop"}),
        );
        assert!(!fresh_shutdown_marker_at(
            missing_timestamp.path(),
            now,
            120.0
        ));

        let invalid_json = tempdir().unwrap();
        std::fs::write(invalid_json.path().join(ACTIVE_MARKER), "{not json").unwrap();
        assert!(!fresh_shutdown_marker_at(invalid_json.path(), now, 120.0));

        let disallowed = tempdir().unwrap();
        write_marker(
            disallowed.path(),
            ACTIVE_MARKER,
            json!({"source": "external_sigterm", "intent": "kill", "timestamp": now - 1.0}),
        );
        assert!(!fresh_shutdown_marker_at(disallowed.path(), now, 120.0));
    }

    #[test]
    fn env_freshness_override_uses_positive_parseable_values() {
        assert_eq!(allow_seconds_from_env(None), DEFAULT_ALLOW_SECONDS);
        assert_eq!(allow_seconds_from_env(Some("2.5")), 2.5);
        assert_eq!(allow_seconds_from_env(Some("0")), DEFAULT_ALLOW_SECONDS);
        assert_eq!(allow_seconds_from_env(Some("-1")), DEFAULT_ALLOW_SECONDS);
        assert_eq!(allow_seconds_from_env(Some("nope")), DEFAULT_ALLOW_SECONDS);
    }

    #[test]
    fn daemon_url_prefers_non_empty_env_override() {
        assert_eq!(
            daemon_url_from_env(Some("http://example.invalid:1234"), || {
                "http://fallback.invalid:60887".to_string()
            }),
            "http://example.invalid:1234"
        );
        assert_eq!(
            daemon_url_from_env(Some(""), || "http://fallback.invalid:60887".to_string()),
            "http://fallback.invalid:60887"
        );
        assert_eq!(
            daemon_url_from_env(None, || "http://fallback.invalid:60887".to_string()),
            "http://fallback.invalid:60887"
        );
    }

    #[test]
    fn preflight_shortcut_requires_stop_marker_and_unreachable_daemon() {
        assert!(should_continue_before_dispatch_with(
            "Stop",
            || true,
            || false
        ));
        assert!(should_continue_before_dispatch_with(
            "stop",
            || true,
            || false
        ));
        assert!(!should_continue_before_dispatch_with(
            "PreToolUse",
            || true,
            || false
        ));
        assert!(!should_continue_before_dispatch_with(
            "Stop",
            || false,
            || { panic!("daemon probe should not run without a marker") }
        ));
        assert!(!should_continue_before_dispatch_with(
            "Stop",
            || true,
            || true
        ));
    }

    #[test]
    fn daemon_probe_treats_http_responses_as_reachable() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut buf = [0_u8; 1024];
            let n = stream.read(&mut buf).unwrap();
            let request = String::from_utf8_lossy(&buf[..n]);
            assert!(request.contains("GET /api/admin/health HTTP/1.1"));
            stream
                .write_all(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
                .unwrap();
        });

        assert!(daemon_is_reachable(&format!("http://{addr}")));
        handle.join().unwrap();
    }

    #[test]
    fn daemon_probe_treats_transport_failure_as_unreachable() {
        assert!(!daemon_is_reachable("file:///tmp/not-a-daemon"));
    }

    #[test]
    fn post_enqueue_suppression_deletes_stop_envelope_for_connect_or_timeout() {
        let dir = tempdir().unwrap();
        let connect = dir.path().join("connect.json");
        let timeout = dir.path().join("timeout.json");
        std::fs::write(&connect, "{}").unwrap();
        std::fs::write(&timeout, "{}").unwrap();

        assert!(suppress_after_failed_post_with_marker(
            "Stop",
            Some(DeliveryFailureKind::Connect),
            &connect,
            || true
        ));
        assert!(!connect.exists());

        assert!(suppress_after_failed_post_with_marker(
            "STOP",
            Some(DeliveryFailureKind::Timeout),
            &timeout,
            || true
        ));
        assert!(!timeout.exists());
    }

    #[test]
    fn post_enqueue_suppression_accepts_already_deleted_envelope() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("missing.json");

        assert!(suppress_after_failed_post_with_marker(
            "Stop",
            Some(DeliveryFailureKind::Connect),
            &path,
            || true
        ));
    }

    #[test]
    fn post_enqueue_suppression_rejects_http_non_stop_and_stale_marker() {
        let dir = tempdir().unwrap();
        let http = dir.path().join("http.json");
        let non_stop = dir.path().join("non-stop.json");
        let stale = dir.path().join("stale.json");
        std::fs::write(&http, "{}").unwrap();
        std::fs::write(&non_stop, "{}").unwrap();
        std::fs::write(&stale, "{}").unwrap();

        assert!(!suppress_after_failed_post_with_marker(
            "Stop",
            Some(DeliveryFailureKind::Http),
            &http,
            || true
        ));
        assert!(http.exists());
        assert!(!suppress_after_failed_post_with_marker(
            "PreToolUse",
            Some(DeliveryFailureKind::Connect),
            &non_stop,
            || true
        ));
        assert!(non_stop.exists());
        assert!(!suppress_after_failed_post_with_marker(
            "Stop",
            Some(DeliveryFailureKind::Connect),
            &stale,
            || false
        ));
        assert!(stale.exists());
    }

    #[test]
    fn post_enqueue_suppression_rejects_delete_failure() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("not-a-file");
        std::fs::create_dir(&path).unwrap();

        assert!(!delete_enqueued(&path));
    }
}
