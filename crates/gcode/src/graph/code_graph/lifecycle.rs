use std::fmt;

use anyhow::Context as _;
use reqwest::StatusCode;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::config::Context;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GraphLifecycleAction {
    Clear,
    Rebuild,
}

impl GraphLifecycleAction {
    pub fn cli_command(self) -> &'static str {
        match self {
            Self::Clear => "gcode graph clear",
            Self::Rebuild => "gcode graph rebuild",
        }
    }

    pub fn endpoint_path(self) -> &'static str {
        match self {
            Self::Clear => "/api/code-index/graph/clear",
            Self::Rebuild => "/api/code-index/graph/rebuild",
        }
    }

    pub fn success_prefix(self) -> &'static str {
        match self {
            Self::Clear => "Cleared code-index graph",
            Self::Rebuild => "Rebuilt code-index graph",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GraphLifecycleRequest {
    pub project_id: String,
    pub daemon_url: Option<String>,
}

impl GraphLifecycleRequest {
    pub fn from_context(ctx: &Context) -> Self {
        Self {
            project_id: ctx.project_id.clone(),
            daemon_url: ctx.daemon_url.clone(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GraphLifecycleOutput {
    pub project_id: String,
    pub action: GraphLifecycleAction,
    pub summary: String,
    pub payload: Value,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GraphReadRequest {
    pub project_id: String,
    pub symbol_id: String,
    pub offset: usize,
    pub limit: usize,
    pub depth: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GraphReadError {
    NotConfigured,
    Unreachable { message: String },
    QueryFailed { message: String },
    InvalidTarget { message: String },
}

impl fmt::Display for GraphReadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotConfigured => {
                f.write_str("FalkorDB is not configured; graph read APIs require FalkorDB")
            }
            Self::Unreachable { message } => {
                write!(
                    f,
                    "FalkorDB is unreachable; graph read APIs require FalkorDB: {message}"
                )
            }
            Self::QueryFailed { message } => {
                write!(f, "FalkorDB graph read failed: {message}")
            }
            Self::InvalidTarget { message } => f.write_str(message),
        }
    }
}

impl std::error::Error for GraphReadError {}

pub fn require_daemon_url(
    daemon_url: Option<&str>,
    action: GraphLifecycleAction,
) -> anyhow::Result<&str> {
    daemon_url.ok_or_else(|| {
        anyhow::anyhow!(
            "Gobby daemon URL is not configured. `{}` requires the Gobby daemon.",
            action.cli_command()
        )
    })
}

pub(crate) fn build_lifecycle_url(
    base_url: &str,
    action: GraphLifecycleAction,
    project_id: &str,
) -> anyhow::Result<reqwest::Url> {
    let base = base_url.trim_end_matches('/');
    let mut url = reqwest::Url::parse(&format!("{base}{}", action.endpoint_path()))
        .with_context(|| format!("invalid Gobby daemon URL: {base_url}"))?;
    url.query_pairs_mut().append_pair("project_id", project_id);
    Ok(url)
}

fn compact_detail(body: &str) -> String {
    let detail = body.split_whitespace().collect::<Vec<_>>().join(" ");
    let detail = detail.trim();
    if detail.len() > 240 {
        format!("{}...", &detail[..237])
    } else {
        detail.to_string()
    }
}

pub(crate) fn format_http_error(
    action: GraphLifecycleAction,
    url: &reqwest::Url,
    status: StatusCode,
    body: &str,
) -> String {
    let detail = compact_detail(body);
    if detail.is_empty() {
        format!(
            "`{}` failed: daemon returned HTTP {status} from {url}",
            action.cli_command()
        )
    } else {
        format!(
            "`{}` failed: daemon returned HTTP {status} from {url}: {detail}",
            action.cli_command()
        )
    }
}

pub(crate) fn parse_success_payload(
    action: GraphLifecycleAction,
    status: StatusCode,
    body: &str,
) -> anyhow::Result<Value> {
    serde_json::from_str(body).map_err(|err| {
        let detail = compact_detail(body);
        if detail.is_empty() {
            anyhow::anyhow!(
                "`{}` failed: daemon returned HTTP {status} with invalid JSON: {err}",
                action.cli_command()
            )
        } else {
            anyhow::anyhow!(
                "`{}` failed: daemon returned HTTP {status} with invalid JSON: {err}. Response: {detail}",
                action.cli_command()
            )
        }
    })
}

pub(crate) fn extract_summary_text(payload: &Value) -> Option<String> {
    match payload {
        Value::String(text) => {
            let text = text.trim();
            (!text.is_empty()).then(|| text.to_string())
        }
        Value::Object(map) => ["summary", "message", "detail", "status"]
            .iter()
            .find_map(|key| map.get(*key).and_then(Value::as_str))
            .map(str::trim)
            .filter(|text| !text.is_empty())
            .map(ToOwned::to_owned),
        _ => None,
    }
}

pub fn run_lifecycle_action(
    request: &GraphLifecycleRequest,
    action: GraphLifecycleAction,
) -> anyhow::Result<GraphLifecycleOutput> {
    let daemon_url = require_daemon_url(request.daemon_url.as_deref(), action)?;
    let url = build_lifecycle_url(daemon_url, action, &request.project_id)?;
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .build()
        .context("failed to build HTTP client")?;

    let response = client
        .post(url.clone())
        .header("Accept", "application/json")
        .send()
        .with_context(|| {
            format!(
                "Failed to reach Gobby daemon at {daemon_url} for `{}`",
                action.cli_command()
            )
        })?;

    let status = response.status();
    let body = response.text().unwrap_or_default();
    if !status.is_success() {
        anyhow::bail!("{}", format_http_error(action, &url, status, &body));
    }

    let payload = parse_success_payload(action, status, &body)?;
    let summary = extract_summary_text(&payload).unwrap_or_else(|| payload.to_string());
    Ok(GraphLifecycleOutput {
        project_id: request.project_id.clone(),
        action,
        summary,
        payload,
    })
}
