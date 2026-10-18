//! Constants shared with the surviving Python wrapper/server modules.

use serde::Deserialize;
use std::sync::LazyLock;

#[derive(Debug, Deserialize)]
pub struct WrapperContract {
    pub protocol_version: String,
    pub protocol_version_header: String,
    pub stale_error_code: String,
    pub wait_tool_names: Vec<String>,
    pub wait_tool_timeout_seconds: f64,
    pub extended_timeout_tool_names: Vec<String>,
    pub extended_tool_timeout_seconds: f64,
    pub http_timeout_buffer_seconds: f64,
    pub wrapper_grace_seconds: f64,
    pub heartbeat_interval_seconds: f64,
    pub terminal_context_keys: Vec<String>,
    pub agent_identity_env_headers: Vec<(String, String)>,
}

pub static WRAPPER_CONTRACT: LazyLock<WrapperContract> = LazyLock::new(|| {
    serde_json::from_str(include_str!("../contracts/mcp_wrapper.v1.json"))
        .expect("the embedded wrapper contract must be valid JSON")
});
