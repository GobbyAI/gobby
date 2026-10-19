use super::{discovery_options, parse_discovery_timeouts};
use serde_json::json;
use std::time::Duration;

#[test]
fn discovery_budgets_preserve_server_scope_floor_and_fallback() {
    let timeouts = parse_discovery_timeouts(&json!({
        "success": true,
        "servers": [
            {"name": "slow/../server", "transport": "stdio", "discovery_timeout": 165.5},
            {"name": "fast", "discovery_timeout": 2.0},
            {"name": "internal", "transport": "internal", "discovery_timeout": 300},
            {"name": "zero", "discovery_timeout": 0},
            {"name": "negative", "discovery_timeout": -1},
            {"name": "text", "discovery_timeout": "180"},
            {"name": "null", "discovery_timeout": null},
            {"discovery_timeout": 180},
            "legacy"
        ]
    }));
    assert_eq!(timeouts.len(), 2);
    let options = discovery_options(&timeouts, "slow/../server", Some("#123"));
    assert_eq!(options.timeout, Duration::from_secs_f64(165.5));
    assert_eq!(options.session_id, Some("#123"));
    for name in [
        "fast", "internal", "zero", "negative", "text", "null", "missing",
    ] {
        assert_eq!(
            discovery_options(&timeouts, name, None).timeout,
            Duration::from_secs(30)
        );
    }
}

#[test]
fn failed_or_malformed_inventory_keeps_default_budget() {
    for inventory in [
        json!({"success": false, "servers": [{"name": "slow", "discovery_timeout": 180}]}),
        json!({"success": true, "servers": {"slow": 180}}),
        json!({"servers": []}),
        json!(null),
    ] {
        assert!(parse_discovery_timeouts(&inventory).is_empty());
    }
}
