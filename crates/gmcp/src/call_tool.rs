//! Target routing, wait guard and progress for the public call_tool wrapper.

use crate::{
    canonical,
    contract::WRAPPER_CONTRACT,
    daemon::{Dialer, RequestOptions},
    tools,
};
use serde_json::Value;
use serde_json::json;
use std::future::Future;
use std::sync::Arc;
use std::time::Duration;

/// A validated target request with wrapper timing and response metadata.
pub struct PreparedCall {
    server_name: String,
    pub tool_name: String,
    pub path: String,
    pub body: Value,
    pub http_timeout: Duration,
    pub guard_timeout: Option<f64>,
    pub params: Vec<(String, String)>,
    pub session_id: Option<String>,
    pub project_id: Option<String>,
    pub preflight: bool,
    requested_timeout: Option<f64>,
}

impl PreparedCall {
    /// Attach wrapper-owned metadata to the daemon result.
    pub fn finish(&self, mut result: Value) -> Value {
        if let Some(requested) = self.requested_timeout {
            result["requested_timeout_seconds"] = json!(requested);
            result["effective_timeout_seconds"] = json!(self.guard_timeout);
            result["wait_timeout_capped_by_mcp_wrapper"] = json!(true);
        }
        result
    }
}

/// Canonicalize and prepare one target invocation.
pub fn prepare(input: &Value) -> anyhow::Result<PreparedCall> {
    let canonical = canonical::canonicalize(input).map_err(anyhow::Error::msg)?;
    let server = canonical["server_name"].as_str().unwrap_or_default();
    let tool = canonical["tool_name"].as_str().unwrap_or_default();
    anyhow::ensure!(
        !server.is_empty() && !tool.is_empty(),
        "Missing required parameters: server_name, tool_name"
    );
    anyhow::ensure!(
        ![server, tool]
            .iter()
            .any(|name| matches!(*name, "." | "..")),
        "server_name/tool_name must name one entry"
    );
    let contract = &WRAPPER_CONTRACT;
    let is_wait = contract.wait_tool_names.iter().any(|name| name == tool);
    let extended = contract
        .extended_timeout_tool_names
        .iter()
        .any(|name| name == tool);
    let mut arguments = canonical["arguments"].clone();
    let mut guard_timeout = extended.then_some(contract.extended_tool_timeout_seconds);
    let mut requested_timeout = None;
    if is_wait {
        let key = if arguments.get("timeout").is_some() {
            "timeout"
        } else {
            "timeout_seconds"
        };
        let raw = &arguments[key];
        guard_timeout = if raw.is_null() {
            Some(contract.wait_tool_timeout_seconds)
        } else {
            number(raw)
        };
        if guard_timeout.is_some_and(|timeout| timeout > contract.wait_tool_timeout_seconds) {
            requested_timeout = guard_timeout;
            guard_timeout = Some(contract.wait_tool_timeout_seconds);
            if !arguments.is_object() {
                arguments = json!({});
            }
            arguments[key] = json!(contract.wait_tool_timeout_seconds);
        }
    }
    let http_timeout = if is_wait {
        let raw = arguments
            .get("timeout")
            .filter(|value| !value.is_null())
            .or_else(|| arguments.get("timeout_seconds"));
        let seconds = raw
            .and_then(number)
            .unwrap_or(contract.wait_tool_timeout_seconds);
        anyhow::ensure!(
            seconds.is_finite() && seconds > 0.0,
            "Invalid wait timeout: {}",
            python_repr(raw.unwrap_or(&Value::Null))
        );
        Duration::try_from_secs_f64(seconds + contract.http_timeout_buffer_seconds)?
    } else if extended {
        Duration::try_from_secs_f64(contract.extended_tool_timeout_seconds)?
    } else {
        Duration::from_secs(30)
    };
    let intent = canonical["intent"].as_str();
    let mut params = Vec::new();
    let body;
    let path;
    if is_wait {
        path = "/api/mcp/tools/call".to_owned();
        body = {
            let mut body = json!({"server_name":server,"tool_name":tool,
                "arguments":if arguments.is_null() { json!({}) } else { arguments }});
            if let Some(intent) = intent {
                body["intent"] = json!(intent);
            }
            body
        };
    } else {
        path = format!(
            "/api/mcp/{}/tools/{}",
            tools::quote(server),
            tools::quote(tool)
        );
        body = if arguments.is_null() {
            json!({})
        } else {
            arguments
        };
        if let Some(intent) = intent {
            params.push(("intent".into(), intent.chars().take(1024).collect()));
        }
    }
    Ok(PreparedCall {
        server_name: server.to_owned(),
        tool_name: tool.to_owned(),
        path,
        body,
        http_timeout,
        guard_timeout,
        params,
        session_id: canonical["session_id"].as_str().map(str::to_owned),
        project_id: canonical["project_id"].as_str().map(str::to_owned),
        preflight: input["preflight_enabled"].as_bool().unwrap_or(true),
        requested_timeout,
    })
}

fn number(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(value) => Some(if *value { 1.0 } else { 0.0 }),
        Value::String(value) => value.trim().parse().ok(),
        _ => value.as_f64(),
    }
}

fn python_repr(value: &Value) -> String {
    match value {
        Value::String(value) => format!("'{value}'"),
        Value::Null => "None".into(),
        Value::Bool(value) => if *value { "True" } else { "False" }.into(),
        _ => value.to_string(),
    }
}

/// Run the prepared request through the shared daemon dialer.
pub async fn call<P, PF>(dialer: Arc<Dialer>, input: Value, progress: Option<P>) -> Value
where
    P: FnMut(Value) -> PF + Send + 'static,
    PF: Future<Output = ()> + Send,
{
    let prepared = match prepare(&input) {
        Ok(prepared) => Arc::new(prepared),
        Err(error) => return json!({"success":false,"error":error.to_string()}),
    };
    if prepared.server_name == "gobby-workflows" && prepared.tool_name == "wait_for_completion" {
        return json!({"success":false,"error_code":"TOOL_REMOVED",
            "server_name":"gobby-workflows","tool_name":"wait_for_completion",
            "error":"gobby-workflows.wait_for_completion was removed. Start the agent or pipeline, persist its run_id or execution_id, then resume from the daemon's durable completion notification and inspect get_task, get_agent_result, or get_pipeline_status."});
    }
    let request = Arc::clone(&prepared);
    let result = guarded_call(
        async move {
            match tokio::task::spawn_blocking(move || {
                let timeout = dialer
                    .tool_timeout(&request.tool_name)
                    .unwrap_or(request.http_timeout);
                let params: Vec<_> = request
                    .params
                    .iter()
                    .map(|(key, value)| (key.as_str(), value.as_str()))
                    .collect();
                dialer.request(
                    "POST",
                    &request.path,
                    Some(&request.body),
                    RequestOptions {
                        project_id: request.project_id.as_deref(),
                        session_id: request.session_id.as_deref(),
                        timeout,
                        preflight: request.preflight,
                        params: &params,
                    },
                )
            })
            .await
            {
                Ok(result) => result,
                Err(error) => json!({"success":false,"error":error.to_string()}),
            }
        },
        &prepared.tool_name,
        prepared.guard_timeout,
        progress,
    )
    .await;
    prepared.finish(result)
}

// The heartbeat belongs to the client wait, including cancellation of that wait.
struct HeartbeatTask(tokio::task::JoinHandle<()>);

impl Drop for HeartbeatTask {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// Bound the client wait without cancelling the daemon request.
pub async fn guarded_call<F, P, PF>(
    request: F,
    tool_name: &str,
    timeout: Option<f64>,
    progress: Option<P>,
) -> Value
where
    F: Future<Output = Value> + Send + 'static,
    P: FnMut(Value) -> PF + Send + 'static,
    PF: Future<Output = ()> + Send,
{
    let contract = &WRAPPER_CONTRACT;
    let guarded = contract
        .wait_tool_names
        .iter()
        .chain(&contract.extended_timeout_tool_names)
        .any(|name| name == tool_name);
    if !guarded {
        return request.await;
    }
    let name = tool_name.to_owned();
    let heartbeat = progress
        .map(|mut report| {
            tokio::spawn(async move {
                let period = Duration::from_secs_f64(WRAPPER_CONTRACT.heartbeat_interval_seconds);
                let mut interval =
                    tokio::time::interval_at(tokio::time::Instant::now() + period, period);
                interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
                let mut elapsed = 0.0;
                loop {
                    interval.tick().await;
                    elapsed += period.as_secs_f64();
                    report(
                        json!({"progress":timeout.map_or(elapsed, |timeout| elapsed.min(timeout)),
                "total":timeout,"message":format!("{name} still waiting for daemon result")}),
                    )
                    .await;
                }
            })
        })
        .map(HeartbeatTask);
    let mut request = tokio::spawn(request);
    let limit = timeout.and_then(|seconds| {
        Duration::try_from_secs_f64((seconds + contract.wrapper_grace_seconds).max(0.0)).ok()
    });
    let result = if let Some(limit) = limit {
        match tokio::time::timeout(limit, &mut request).await {
            Ok(result) => joined(result),
            Err(_) => json!({"success":true,"completed":false,"timeout_seconds":timeout,
                "effective_timeout_seconds":timeout,"mcp_wrapper_timeout":true,
                "background_call_continues":true,"tool_name":tool_name}),
        }
    } else {
        joined(request.await)
    };
    drop(heartbeat);
    result
}

fn joined(result: Result<Value, tokio::task::JoinError>) -> Value {
    result.unwrap_or_else(|error| json!({"success":false,"error":error.to_string()}))
}
