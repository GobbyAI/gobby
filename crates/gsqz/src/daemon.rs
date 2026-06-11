//! Gobby daemon integration (feature-gated behind "gobby").
//!
//! Best-effort HTTP calls to the daemon for config fetch and savings reporting.
//! All errors are silently ignored — daemon being down should never break compression.

#[cfg(feature = "gobby")]
use serde_json::json;

/// Fetch compression settings from the daemon. Returns (min_length, max_lines) on success.
#[cfg(feature = "gobby")]
pub fn fetch_daemon_config(base_url: &str) -> Option<(usize, usize)> {
    let url = format!("{}/api/config/values", base_url);
    let body: serde_json::Value = ureq::get(&url)
        .timeout(std::time::Duration::from_secs(1))
        .call()
        .ok()?
        .into_json()
        .ok()?;
    let cfg = body.get("output_compression")?;
    let min_length = cfg.get("min_output_length")?.as_u64()? as usize;
    let max_lines = cfg.get("max_compressed_lines")?.as_u64()? as usize;
    Some((min_length, max_lines))
}

#[cfg(not(feature = "gobby"))]
pub fn fetch_daemon_config(_base_url: &str) -> Option<(usize, usize)> {
    None
}

/// Report compression savings to the daemon.
#[cfg(feature = "gobby")]
pub fn report_savings(base_url: &str, strategy: &str, original_chars: usize, actual_chars: usize) {
    let url = format!("{}/api/admin/savings/record", base_url);
    let payload = json!({
        "category": "compression",
        "original_chars": original_chars,
        "actual_chars": actual_chars,
        "metadata": { "strategy": strategy }
    });
    let _ = ureq::post(&url)
        .timeout(std::time::Duration::from_secs(1))
        .send_json(payload);
}

#[cfg(not(feature = "gobby"))]
pub fn report_savings(
    _base_url: &str,
    _strategy: &str,
    _original_chars: usize,
    _actual_chars: usize,
) {
    // No-op without gobby feature
}

/// Resolve the daemon URL from config or the shared gobby-core resolver.
///
/// Resolution order: config `daemon_url` (with `${GOBBY_PORT}` expansion) →
/// `gobby_core::daemon_url` (GOBBY_DAEMON_URL → GOBBY_PORT → bootstrap.yaml).
/// Reading bootstrap.yaml means a custom `daemon_port` reaches config-override
/// fetch and savings reporting without any env vars in the shell.
#[cfg(feature = "gobby")]
pub fn resolve_daemon_url(config_url: Option<&str>) -> Option<String> {
    if let Some(url) = config_url {
        // Expand ${GOBBY_PORT} if present
        if url.contains("${GOBBY_PORT}") {
            if let Ok(port) = std::env::var("GOBBY_PORT") {
                return Some(url.replace("${GOBBY_PORT}", &port));
            }
            // Fall through to the shared resolver if GOBBY_PORT not set
        } else {
            return Some(url.to_string());
        }
    }

    Some(gobby_core::daemon_url::daemon_url())
}

#[cfg(not(feature = "gobby"))]
pub fn resolve_daemon_url(_config_url: Option<&str>) -> Option<String> {
    // Without the gobby feature the daemon calls are no-ops, so there is
    // no URL to resolve.
    None
}

#[cfg(all(test, feature = "gobby"))]
mod tests {
    use super::*;

    #[test]
    fn config_url_passes_through() {
        assert_eq!(
            resolve_daemon_url(Some("http://custom:9999")),
            Some("http://custom:9999".to_string())
        );
    }

    #[test]
    fn config_url_expands_gobby_port() {
        temp_env::with_var("GOBBY_PORT", Some("54321"), || {
            assert_eq!(
                resolve_daemon_url(Some("http://myhost:${GOBBY_PORT}")),
                Some("http://myhost:54321".to_string())
            );
        });
    }

    #[test]
    fn no_config_reads_bootstrap_daemon_port() {
        let temp = tempfile::tempdir().expect("tempdir");
        std::fs::write(temp.path().join("bootstrap.yaml"), "daemon_port: 61234\n")
            .expect("write bootstrap");

        temp_env::with_vars(
            [
                ("GOBBY_DAEMON_URL", None::<&str>),
                ("GOBBY_PORT", None::<&str>),
                ("GOBBY_HOME", Some(temp.path().to_str().expect("utf8 path"))),
            ],
            || {
                assert_eq!(
                    resolve_daemon_url(None),
                    Some("http://127.0.0.1:61234".to_string())
                );
            },
        );
    }
}
