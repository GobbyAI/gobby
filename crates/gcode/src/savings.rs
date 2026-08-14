//! Daemon-based savings tracking for gcode.
//!
//! Reports token savings to the Gobby daemon via HTTP POST when gcode returns
//! compact symbol/outline data instead of full file contents.

/// Calculate savings percentage.
#[cfg(test)]
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
pub fn report_savings(
    base_url: &str,
    original_chars: usize,
    actual_chars: usize,
    grant: Option<&gobby_core::grant::GrantBundle>,
) {
    let url = format!("{}/api/admin/savings/record", base_url);
    let payload = serde_json::json!({
        "category": "code_index",
        "original_chars": original_chars,
        "actual_chars": actual_chars,
        "metadata": { "strategy": "outline" }
    });
    let mut request = ureq::post(&url).timeout(std::time::Duration::from_secs(1));
    if let Some(grant) = grant
        && let Ok(header) = gobby_core::grant::encode_grant_header(grant)
    {
        request = request.set(gobby_core::grant::GRANT_HEADER, &header);
        request = request.set(
            gobby_core::grant::MACHINE_HEADER,
            &grant.principal.machine_id,
        );
        request = request.set(
            gobby_core::grant::CALLER_PROJECT_HEADER,
            &grant.principal.project_id,
        );
        request = request.set(
            gobby_core::grant::TARGET_PROJECT_HEADER,
            &grant.principal.project_id,
        );
        if let Some(session_id) = &grant.principal.session_id {
            request = request.set(gobby_core::grant::SESSION_HEADER, session_id);
        }
        if let Ok(token) = gobby_core::local_token::read_local_cli_token() {
            request = request.set(
                gobby_core::local_token::AUTHORIZATION_HEADER,
                &gobby_core::local_token::authorization_bearer(&token),
            );
        }
    }
    let _ = request.send_json(payload);
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
}
