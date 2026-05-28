use std::collections::HashMap;

use anyhow::Context as _;
use reqwest::StatusCode;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::config::Context;
use crate::falkor::{self, Row};
use crate::graph::typed_query;
use crate::models::GraphResult;

const CALL_TARGET_PREDICATE: &str =
    "target:CodeSymbol OR target:UnresolvedCallee OR target:ExternalSymbol";
const MAX_GRAPH_LIMIT: usize = 100;

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

fn extract_summary_text(payload: &Value) -> Option<String> {
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

pub(crate) fn row_to_graph_result(row: &Row) -> GraphResult {
    GraphResult {
        id: row
            .get("caller_id")
            .or_else(|| row.get("callee_id"))
            .or_else(|| row.get("source_id"))
            .or_else(|| row.get("node_id"))
            .or_else(|| row.get("symbol_id"))
            .or_else(|| row.get("id"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        name: row
            .get("caller_name")
            .or_else(|| row.get("callee_name"))
            .or_else(|| row.get("source_name"))
            .or_else(|| row.get("node_name"))
            .or_else(|| row.get("symbol_name"))
            .or_else(|| row.get("name"))
            .or_else(|| row.get("module_name"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        file_path: row
            .get("file")
            .or_else(|| row.get("file_path"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        line: row.get("line").and_then(|v| v.as_u64()).unwrap_or(0) as usize,
        relation: row
            .get("relation")
            .or_else(|| row.get("rel_type"))
            .and_then(|v| v.as_str())
            .map(String::from),
        distance: row
            .get("distance")
            .and_then(|v| v.as_u64())
            .map(|d| d as usize),
    }
}

fn clamp_limit(limit: usize) -> usize {
    typed_query::clamp_limit(limit, MAX_GRAPH_LIMIT)
}

fn clamp_offset(offset: usize) -> usize {
    typed_query::clamp_offset(offset, MAX_GRAPH_LIMIT)
}

pub(crate) fn count_callers_query(
    project_id: &str,
    symbol_id: &str,
) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (caller:CodeSymbol {{project: $project}})-[:CALLS]->(target {{id: $id, project: $project}}) \
             WHERE {CALL_TARGET_PREDICATE} \
             RETURN count(caller) AS cnt"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

pub(crate) fn count_usages_query(
    project_id: &str,
    symbol_id: &str,
) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (source:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{id: $id, project: $project}}) \
             WHERE {CALL_TARGET_PREDICATE} \
             RETURN count(source) AS cnt"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

pub(crate) fn find_callers_query(
    project_id: &str,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let offset = clamp_offset(offset);
    let limit = clamp_limit(limit);
    (
        format!(
            "MATCH (caller:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{id: $id, project: $project}}) \
             WHERE {CALL_TARGET_PREDICATE} \
             RETURN caller.id AS caller_id, caller.name AS caller_name, \
                    r.file AS file, r.line AS line \
             SKIP {offset} LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

pub(crate) fn find_usages_query(
    project_id: &str,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let offset = clamp_offset(offset);
    let limit = clamp_limit(limit);
    (
        format!(
            "MATCH (source:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{id: $id, project: $project}}) \
             WHERE {CALL_TARGET_PREDICATE} \
             RETURN source.id AS source_id, source.name AS source_name, \
                    'CALLS' AS rel_type, r.file AS file, r.line AS line \
             SKIP {offset} LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

pub(crate) fn find_callers_batch_query(
    project_id: &str,
    symbol_ids: &[String],
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let ids = typed_query::id_list_literal(symbol_ids);
    (
        format!(
            "MATCH (caller:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{project: $project}}) \
             WHERE ({CALL_TARGET_PREDICATE}) AND target.id IN [{ids}] \
             RETURN caller.id AS caller_id, caller.name AS caller_name, \
                    r.file AS file, r.line AS line \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

pub(crate) fn find_callees_batch_query(
    project_id: &str,
    symbol_ids: &[String],
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let ids = typed_query::id_list_literal(symbol_ids);
    (
        format!(
            "MATCH (src:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{project: $project}}) \
             WHERE src.id IN [{ids}] AND ({CALL_TARGET_PREDICATE}) \
             RETURN target.id AS callee_id, target.name AS callee_name, \
                    r.file AS file, r.line AS line \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

pub(crate) fn get_imports_query(
    project_id: &str,
    file_path: &str,
) -> (String, HashMap<String, String>) {
    (
        "MATCH (f:CodeFile {path: $path, project: $project})-[:IMPORTS]->(m:CodeModule) \
         RETURN m.name AS module_name"
            .to_string(),
        typed_query::string_params(&[("project", project_id), ("path", file_path)]),
    )
}

pub(crate) fn blast_radius_query(depth: usize, limit: usize) -> String {
    let depth = depth.clamp(1, 5);
    let limit = clamp_limit(limit);
    format!(
        "MATCH (target {{id: $id, project: $project}}) \
         WHERE {CALL_TARGET_PREDICATE} \
         MATCH path = (affected:CodeSymbol {{project: $project}})-[:CALLS*1..{depth}]->(target) \
         WITH affected, min(length(path)) AS distance \
         OPTIONAL MATCH (file:CodeFile {{project: $project}})-[:DEFINES]->(affected) \
         RETURN DISTINCT affected.id AS node_id, \
                affected.name AS node_name, \
                affected.kind AS kind, file.path AS file_path, \
                affected.line_start AS line, \
                distance, 'call' AS rel_type \
         ORDER BY distance ASC, affected.name ASC \
         LIMIT {limit}"
    )
}

fn count_from_rows(rows: &[Row]) -> usize {
    rows.first()
        .and_then(|r| r.get("cnt"))
        .and_then(|v| {
            v.as_u64()
                .or_else(|| v.as_i64().and_then(|value| value.try_into().ok()))
        })
        .unwrap_or(0) as usize
}

pub fn count_callers(ctx: &Context, symbol_id: &str) -> anyhow::Result<usize> {
    falkor::with_falkor(ctx, 0, |client| {
        let (query, params) = count_callers_query(&ctx.project_id, symbol_id);
        let rows = client.query(&query, Some(params))?;
        Ok(count_from_rows(&rows))
    })
}

pub fn count_usages(ctx: &Context, symbol_id: &str) -> anyhow::Result<usize> {
    falkor::with_falkor(ctx, 0, |client| {
        let (query, params) = count_usages_query(&ctx.project_id, symbol_id);
        let rows = client.query(&query, Some(params))?;
        Ok(count_from_rows(&rows))
    })
}

pub fn find_callers(
    ctx: &Context,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    falkor::with_falkor(ctx, vec![], |client| {
        let (query, params) = find_callers_query(&ctx.project_id, symbol_id, offset, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn find_usages(
    ctx: &Context,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    falkor::with_falkor(ctx, vec![], |client| {
        let (query, params) = find_usages_query(&ctx.project_id, symbol_id, offset, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn find_callers_batch(
    ctx: &Context,
    symbol_ids: &[String],
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    if symbol_ids.is_empty() {
        return Ok(vec![]);
    }
    falkor::with_falkor(ctx, vec![], |client| {
        let (query, params) = find_callers_batch_query(&ctx.project_id, symbol_ids, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn find_callees_batch(
    ctx: &Context,
    symbol_ids: &[String],
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    if symbol_ids.is_empty() {
        return Ok(vec![]);
    }
    falkor::with_falkor(ctx, vec![], |client| {
        let (query, params) = find_callees_batch_query(&ctx.project_id, symbol_ids, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn get_imports(ctx: &Context, file_path: &str) -> anyhow::Result<Vec<GraphResult>> {
    falkor::with_falkor(ctx, vec![], |client| {
        let (query, params) = get_imports_query(&ctx.project_id, file_path);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn blast_radius(
    ctx: &Context,
    symbol_id: &str,
    depth: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    falkor::with_falkor(ctx, vec![], |client| {
        let query = blast_radius_query(depth, MAX_GRAPH_LIMIT);
        let params = typed_query::string_params(&[("project", &ctx.project_id), ("id", symbol_id)]);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}
