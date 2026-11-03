//! Which calls ride out a planned daemon restart instead of failing fast.
//!
//! A seat in a restart hold keeps its handoff, renews its coordination wait and
//! reports to its coordinator. Those calls retry while a fresh restart marker is on
//! disk; every other call still reports DAEMON_UNAVAILABLE at once.

use serde_json::Value;
use std::time::{SystemTime, UNIX_EPOCH};

const MARKER: &str = "shutdown_intent_active.json";
const DEFAULT_ALLOW_SECONDS: f64 = 120.0;

/// Tool calls a held seat still makes, as `/api/mcp/<server>/tools/<tool>/` paths.
const HOLD_CALLS: [(&str, &str); 5] = [
    ("gobby-sessions", "set_handoff"),
    ("gobby-sessions", "get_handoff"),
    ("gobby-sessions", "feedback"),
    ("gobby-agents", "send_message"),
    ("gobby-agents", "wait_for_coordination"),
];

/// True when a call to `path` should wait for the daemon instead of failing.
pub(crate) fn retries(path: &str) -> bool {
    is_hold_path(path) && restart_in_progress()
}

fn is_hold_path(path: &str) -> bool {
    let trimmed = path.trim_matches('/');
    HOLD_CALLS
        .iter()
        .any(|(server, tool)| trimmed == format!("api/mcp/{server}/tools/{tool}"))
}

/// A restart marker written within the freshness window, as ghook reads it.
fn restart_in_progress() -> bool {
    let Ok(home) = gobby_core::gobby_home() else {
        return false;
    };
    let Ok(raw) = std::fs::read(home.join(MARKER)) else {
        return false;
    };
    let Ok(marker) = serde_json::from_slice::<Value>(&raw) else {
        return false;
    };
    let restart = marker["intent"]
        .as_str()
        .is_some_and(|intent| intent.eq_ignore_ascii_case("restart"));
    let Some(written) = marker["timestamp"].as_f64() else {
        return false;
    };
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs_f64())
        .unwrap_or(0.0);
    restart && written <= now && now - written <= allow_seconds()
}

fn allow_seconds() -> f64 {
    std::env::var("GOBBY_SHUTDOWN_HOOK_ALLOW_SECONDS")
        .ok()
        .and_then(|value| value.trim().parse::<f64>().ok())
        .filter(|seconds| seconds.is_finite() && *seconds > 0.0)
        .unwrap_or(DEFAULT_ALLOW_SECONDS)
}

#[cfg(test)]
mod tests {
    use super::is_hold_path;

    #[test]
    fn only_hold_tool_paths_retry() {
        assert!(is_hold_path(
            "/api/mcp/gobby-agents/tools/wait_for_coordination/"
        ));
        assert!(is_hold_path("/api/mcp/gobby-sessions/tools/set_handoff"));
        assert!(!is_hold_path("/api/mcp/gobby-tasks/tools/claim_task/"));
        assert!(!is_hold_path("/api/mcp/gobby-agents/tools/spawn_agent/"));
        assert!(!is_hold_path("/api/health"));
    }
}
