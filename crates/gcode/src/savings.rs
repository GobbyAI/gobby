//! Daemon-based savings tracking for gcode.
//!
//! Reports token savings to the Gobby daemon via HTTP POST when gcode returns
//! compact symbol/outline data instead of full file contents.
//!
//! Display output follows the gsqz pattern: stderr prefix showing savings.

/// Calculate savings percentage.
pub fn savings_pct(original_chars: usize, actual_chars: usize) -> f64 {
    if original_chars == 0 {
        return 0.0;
    }
    (1.0 - actual_chars as f64 / original_chars as f64) * 100.0
}

/// Report a savings event to the Gobby daemon via HTTP POST.
///
/// Best-effort: all errors are silently ignored. The daemon being down
/// should never break gcode functionality.
pub fn report_savings(base_url: &str, original_chars: usize, actual_chars: usize) {
    let url = format!("{}/api/admin/savings/record", base_url);
    let payload = serde_json::json!({
        "category": "code_index",
        "original_chars": original_chars,
        "actual_chars": actual_chars,
        "metadata": { "strategy": "outline" }
    });
    let _ = ureq::post(&url)
        .timeout(std::time::Duration::from_secs(1))
        .send_json(payload);
}

/// Resolve the daemon URL from config or environment.
///
/// Resolution order: config `daemon_url` → `GOBBY_PORT` env → default port 60887
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

/// Print savings info to stderr in gsqz-style format.
pub fn print_savings(label: &str, original_chars: usize, actual_chars: usize) {
    if actual_chars == 0 || original_chars <= actual_chars {
        return;
    }
    let pct = savings_pct(original_chars, actual_chars);
    eprintln!("[gcode \u{2014} {label}, saved {pct:.0}% ({actual_chars}B vs {original_chars}B)]");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_savings_pct_basic() {
        let pct = savings_pct(1000, 200);
        assert!((pct - 80.0).abs() < 0.01);
    }

    #[test]
    fn test_savings_pct_zero_original() {
        assert_eq!(savings_pct(0, 0), 0.0);
    }

    #[test]
    fn test_savings_pct_no_savings() {
        assert!((savings_pct(100, 100)).abs() < 0.01);
    }

    #[test]
    fn test_resolve_daemon_url_config_value() {
        let url = resolve_daemon_url(Some("http://custom:9999"));
        assert_eq!(url, Some("http://custom:9999".to_string()));
    }

    #[test]
    fn test_resolve_daemon_url_env_var() {
        unsafe { std::env::set_var("GOBBY_PORT", "12345") };
        let url = resolve_daemon_url(None);
        assert_eq!(url, Some("http://localhost:12345".to_string()));
        unsafe { std::env::remove_var("GOBBY_PORT") };
    }

    #[test]
    fn test_resolve_daemon_url_default() {
        unsafe { std::env::remove_var("GOBBY_PORT") };
        let url = resolve_daemon_url(None);
        assert_eq!(url, Some("http://localhost:60887".to_string()));
    }

    #[test]
    fn test_resolve_daemon_url_expand_port() {
        unsafe { std::env::set_var("GOBBY_PORT", "54321") };
        let url = resolve_daemon_url(Some("http://myhost:${GOBBY_PORT}"));
        assert_eq!(url, Some("http://myhost:54321".to_string()));
        unsafe { std::env::remove_var("GOBBY_PORT") };
    }
}
