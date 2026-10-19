//! The MCP server surface that `gmcp` serves over stdio.

use std::future::Future;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use rmcp::model::{
    CallToolRequestParams, CallToolResponse, Implementation, InitializeResult, ListToolsResult,
    PaginatedRequestParams, PromptsCapability, ProtocolVersion, ResourcesCapability,
    ServerCapabilities, ServerConfig, ToolsCapability,
};
use rmcp::service::RequestContext;
use rmcp::{ErrorData, RoleServer, ServerHandler};
use serde_json::{Value, json};

use crate::daemon::{Dialer, RequestOptions};
use crate::tools;

/// The bundled progressive-discovery prompt, front matter included.
const BUNDLED_PROMPT: &str = include_str!("../assets/progressive-discovery.md");

/// The tool list the wrapper answers `tools/list` with.
const TOOLS_LIST: &str = include_str!("../contracts/tools_list.v1.json");

/// Error codes the wrapper retries. Any other failure ends the readiness report.
pub const RETRYABLE_ERROR_CODES: [&str; 3] =
    ["SESSION_REQUIRED", "DAEMON_UNAVAILABLE", "REQUEST_TIMEOUT"];

/// The wait before each readiness attempt: the first post is immediate, then the wrapper backs off.
const READY_DELAYS: [Duration; 9] = [
    Duration::ZERO,
    Duration::from_millis(500),
    Duration::from_secs(1),
    Duration::from_secs(2),
    Duration::from_secs(4),
    Duration::from_secs(8),
    Duration::from_secs(15),
    Duration::from_secs(30),
    Duration::from_secs(30),
];

/// The instructions `initialize` returns: the prompt body after its front matter, trimmed.
pub fn instructions() -> &'static str {
    BUNDLED_PROMPT
        .strip_prefix("---")
        .and_then(|rest| rest.split_once("---").map(|(_, body)| body))
        .unwrap_or(BUNDLED_PROMPT)
        .trim()
}

/// The MCP surface served over stdio.
#[derive(Clone)]
pub struct WrapperServer {
    dialer: Arc<Dialer>,
    readiness_started: Arc<AtomicBool>,
}

impl WrapperServer {
    pub fn new(dialer: Dialer) -> Self {
        Self {
            dialer: Arc::new(dialer),
            readiness_started: Arc::new(AtomicBool::new(false)),
        }
    }
}

impl ServerHandler for WrapperServer {
    fn get_info(&self) -> ServerConfig {
        let mut tools = ToolsCapability::default();
        tools.list_changed = Some(false);
        let mut prompts = PromptsCapability::default();
        prompts.list_changed = Some(false);
        let mut resources = ResourcesCapability::default();
        resources.subscribe = Some(false);
        resources.list_changed = Some(false);
        let mut capabilities = ServerCapabilities::default();
        capabilities.experimental = Some(Default::default());
        capabilities.prompts = Some(prompts);
        capabilities.resources = Some(resources);
        capabilities.tools = Some(tools);
        InitializeResult::new(capabilities)
            .with_protocol_version(ProtocolVersion::V_2025_11_25)
            .with_server_info(Implementation::new("gobby", env!("CARGO_PKG_VERSION")))
            .with_instructions(instructions())
    }

    async fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, ErrorData> {
        if claim_readiness(&self.readiness_started) {
            tokio::spawn(report_bridge_ready(Arc::clone(&self.dialer)));
        }
        serde_json::from_str(TOOLS_LIST)
            .map_err(|error| ErrorData::internal_error(error.to_string(), None))
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        _context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, ErrorData> {
        let dialer = Arc::clone(&self.dialer);
        let name = request.name.into_owned();
        let arguments = request.arguments.unwrap_or_default();
        let answer = tokio::task::spawn_blocking(move || tools::call(&dialer, &name, &arguments))
            .await
            .map_err(|error| ErrorData::internal_error(error.to_string(), None))??;
        Ok(CallToolResponse::from(tools::envelope(answer)))
    }
}

/// A readiness post the daemon did not accept: the error code it returned and its message.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReadyFailure {
    pub code: String,
    pub message: String,
}

/// Runs `attempt` after each delay until it succeeds or fails outside the retry set.
///
/// `attempt` yields `Ok` on success and the failure otherwise. Failures in the retry set are
/// retried silently. As the Python `DaemonProxy.report_bridge_ready` does, `log` receives the
/// failure that ends the run: one outside the set at once, or the last retried failure once
/// the schedule runs out. A give-up on `SESSION_REQUIRED` stays silent, since a client without
/// Gobby hooks never registers a session. Returns the number of attempts made.
pub async fn report_ready<F, Fut>(
    delays: &[Duration],
    mut attempt: F,
    mut log: impl FnMut(&str),
) -> usize
where
    F: FnMut() -> Fut,
    Fut: Future<Output = Result<(), ReadyFailure>>,
{
    let mut attempts = 0;
    let mut last = None;
    for delay in delays {
        tokio::time::sleep(*delay).await;
        attempts += 1;
        match attempt().await {
            Ok(()) => return attempts,
            Err(failure) if RETRYABLE_ERROR_CODES.contains(&failure.code.as_str()) => {
                last = Some(failure);
            }
            Err(failure) => {
                log(&format!(
                    "gmcp: bridge readiness report failed: {}",
                    failure.message
                ));
                return attempts;
            }
        }
    }
    if let Some(failure) = last.filter(|failure| failure.code != "SESSION_REQUIRED") {
        let retries = attempts.saturating_sub(1);
        log(&format!(
            "gmcp: bridge readiness report gave up after {retries} retries: {}",
            failure.message
        ));
    }
    attempts
}

/// Whether this call is the first claim on `started`, so the readiness report starts once.
fn claim_readiness(started: &AtomicBool) -> bool {
    !started.swap(true, Ordering::AcqRel)
}

/// Reports bridge readiness once the first `tools/list` has been answered.
async fn report_bridge_ready(dialer: Arc<Dialer>) {
    report_ready(
        &READY_DELAYS,
        || post_ready(Arc::clone(&dialer)),
        |line| eprintln!("{line}"),
    )
    .await;
}

/// One readiness post: `Ok` when the daemon accepted it, otherwise the failure it reported.
async fn post_ready(dialer: Arc<Dialer>) -> Result<(), ReadyFailure> {
    let answer = tokio::task::spawn_blocking(move || {
        let body = json!({});
        dialer.request(
            "POST",
            "/api/mcp/bridge/ready",
            Some(&body),
            RequestOptions::default(),
        )
    })
    .await
    .map_err(|error| ReadyFailure {
        code: String::new(),
        message: error.to_string(),
    })?;
    if answer.get("success").and_then(Value::as_bool) == Some(true) {
        return Ok(());
    }
    Err(ReadyFailure {
        code: answer
            .get("error_code")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned(),
        message: answer
            .get("error")
            .and_then(Value::as_str)
            .map_or_else(|| answer.to_string(), str::to_owned),
    })
}

#[cfg(test)]
#[path = "server/tests.rs"]
mod tests;
