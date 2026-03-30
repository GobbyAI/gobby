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

/// Resolve the daemon URL from config or environment.
///
/// Resolution order: config `daemon_url` → `GOBBY_PORT` env → default port 60887
/// (gobby feature only). The well-known default ensures savings reporting works
/// even when GOBBY_PORT isn't in the shell environment.
pub fn resolve_daemon_url(config_url: Option<&str>) -> Option<String> {
    if let Some(url) = config_url {
        // Expand ${GOBBY_PORT} if present
        if url.contains("${GOBBY_PORT}") {
            if let Ok(port) = std::env::var("GOBBY_PORT") {
                return Some(url.replace("${GOBBY_PORT}", &port));
            }
            // Fall through to defaults if GOBBY_PORT not set
        } else {
            return Some(url.to_string());
        }
    }

    // Fall back to GOBBY_PORT env var
    if let Ok(port) = std::env::var("GOBBY_PORT") {
        return Some(format!("http://localhost:{}", port));
    }

    // Default to well-known Gobby daemon (matches bootstrap.yaml defaults)
    Some("http://localhost:60887".to_string())
}
