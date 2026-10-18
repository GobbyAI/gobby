//! The tools that forward to the daemon, each built the way its `DaemonProxy` method builds it.

use std::fmt::Write as _;
use std::time::Duration;

use rmcp::ErrorData;
use rmcp::model::{CallToolResult, ContentBlock, JsonObject};
use serde_json::{Map, Value, json};

use crate::daemon::{Dialer, RequestOptions};
use crate::shaping::{compact_server_list, strip_none, tool_schema};

/// The answer `init_project` gives without a request.
const INIT_PROJECT_ERROR: &str =
    "Project initialization requires CLI access. Use 'gobby init' command instead.";

/// Recommendations and searches get longer than the 30 s default.
const SEARCH_TIMEOUT: Duration = Duration::from_secs(60);

/// Runs the named tool and returns the answer as JSON.
pub fn call(dialer: &Dialer, name: &str, arguments: &JsonObject) -> Result<Value, ErrorData> {
    let session = opt_str(arguments, "session_id")?;
    match name {
        "list_mcp_servers" => {
            let answer = dialer.request("GET", "/api/mcp/servers", None, scoped(session));
            Ok(compact_server_list(answer))
        }
        "list_tools" => {
            let server = str_arg(arguments, "server_name")?;
            if server.is_empty() {
                return Ok(list_all_tools(dialer, session));
            }
            let path = format!("/api/mcp/{server}/tools");
            Ok(strip_none(dialer.request(
                "GET",
                &path,
                None,
                scoped(session),
            )))
        }
        "get_tool_schema" => {
            let server_name = str_arg(arguments, "server_name")?;
            let tool_name = str_arg(arguments, "tool_name")?;
            let body = json!({"server_name": server_name, "tool_name": tool_name});
            let answer = dialer.request(
                "POST",
                "/api/mcp/tools/schema",
                Some(&body),
                scoped(session),
            );
            Ok(tool_schema(answer))
        }
        "recommend_tools" => {
            let task_description = str_arg(arguments, "task_description")?;
            let agent_id = opt_str(arguments, "agent_id")?;
            let body = json!({
                "task_description": task_description,
                "agent_id": agent_id,
                "search_mode": arg_or(arguments, "search_mode", json!("llm")),
                "top_k": arg_or(arguments, "top_k", json!(10)),
                "min_similarity": arg_or(arguments, "min_similarity", json!(0.3)),
                "cwd": cwd(dialer),
            });
            let options = RequestOptions {
                timeout: SEARCH_TIMEOUT,
                ..RequestOptions::default()
            };
            Ok(dialer.request("POST", "/api/mcp/tools/recommend", Some(&body), options))
        }
        "search_tools" => {
            let query = str_arg(arguments, "query")?;
            let server = opt_str(arguments, "server_name")?;
            let body = json!({
                "query": query,
                "top_k": arg_or(arguments, "top_k", json!(10)),
                "min_similarity": arg_or(arguments, "min_similarity", json!(0.0)),
                "server": server,
                "cwd": cwd(dialer),
            });
            let options = RequestOptions {
                timeout: SEARCH_TIMEOUT,
                ..RequestOptions::default()
            };
            Ok(dialer.request("POST", "/api/mcp/tools/search", Some(&body), options))
        }
        "add_mcp_server" => {
            // Python drops every unset field and adds the caller's project id.
            let mut body = Map::new();
            body.insert("name".into(), json!(str_arg(arguments, "name")?));
            let optional = ["transport", "url", "headers", "command", "args", "env"];
            for key in optional
                .into_iter()
                .chain(["template", "values", "description"])
            {
                insert_set(&mut body, arguments, key);
            }
            body.insert("enabled".into(), arg_or(arguments, "enabled", json!(true)));
            body.insert("scope".into(), arg_or(arguments, "scope", json!("project")));
            if let Some(project) = dialer.caller_project_id() {
                body.insert("project_id".into(), json!(project));
            }
            let body = Value::Object(body);
            Ok(dialer.request("POST", "/api/mcp/servers", Some(&body), scoped(None)))
        }
        "remove_mcp_server" => {
            let name = str_arg(arguments, "name")?;
            let scope = opt_str(arguments, "scope")?.unwrap_or("project");
            let project = dialer.caller_project_id();
            let mut params = vec![("scope", scope)];
            if let Some(project) = project.as_deref() {
                params.push(("project_id", project));
            }
            let options = RequestOptions {
                params: &params,
                ..RequestOptions::default()
            };
            let path = format!("/api/mcp/servers/{name}");
            Ok(dialer.request("DELETE", &path, None, options))
        }
        "import_mcp_server" => {
            let body = json!({
                "from_project": value_arg(arguments, "from_project"),
                "servers": value_arg(arguments, "servers"),
                "github_url": value_arg(arguments, "github_url"),
                "query": value_arg(arguments, "query"),
            });
            Ok(dialer.request("POST", "/api/mcp/servers/import", Some(&body), scoped(None)))
        }
        "set_variable" => {
            let target = str_arg(arguments, "session_id")?;
            let name = str_arg(arguments, "name")?;
            let body = json!({
                "name": name,
                "value": value_arg(arguments, "value"),
                "scope": arg_or(arguments, "scope", json!("session")),
            });
            let path = format!("/api/sessions/{}/variables/set", quote(target));
            Ok(dialer.request("POST", &path, Some(&body), scoped(Some(target))))
        }
        "get_variable" => {
            let target = str_arg(arguments, "session_id")?;
            let body = json!({"name": value_arg(arguments, "name"), "scope": "session"});
            let path = format!("/api/sessions/{}/variables/get", quote(target));
            Ok(dialer.request("POST", &path, Some(&body), scoped(Some(target))))
        }
        "init_project" => Ok(json!({"success": false, "error": INIT_PROJECT_ERROR})),
        _ => Err(ErrorData::invalid_params(
            format!("unknown tool: {name}"),
            None,
        )),
    }
}

/// Lists the tools of every server in the status map, dropping the servers whose listing fails.
fn list_all_tools(dialer: &Dialer, session: Option<&str>) -> Value {
    let status = dialer.request("GET", "/api/admin/status", None, scoped(session));
    if status.get("success") == Some(&Value::Bool(false)) {
        return status;
    }
    let mut servers = Vec::new();
    if let Some(names) = status.get("mcp_servers").and_then(Value::as_object) {
        for name in names.keys() {
            let path = format!("/api/mcp/{name}/tools");
            let listing = dialer.request("GET", &path, None, scoped(session));
            if listing.get("success") == Some(&Value::Bool(true)) {
                let tools = listing.get("tools").cloned().unwrap_or_else(|| json!([]));
                servers.push(json!({"name": name, "tools": tools}));
            }
        }
    }
    strip_none(json!({"success": true, "servers": servers}))
}

/// The call result for an answer: the answer as pretty JSON text, and as structured content.
pub fn envelope(answer: Value) -> CallToolResult {
    let text = serde_json::to_string_pretty(&answer).unwrap_or_else(|_| answer.to_string());
    let mut result = CallToolResult::structured(answer);
    result.content = vec![ContentBlock::text(text)];
    result
}

fn scoped(session_id: Option<&str>) -> RequestOptions<'_> {
    RequestOptions {
        session_id,
        ..RequestOptions::default()
    }
}

fn cwd(dialer: &Dialer) -> String {
    dialer.cwd().to_string_lossy().into_owned()
}

fn str_arg<'a>(arguments: &'a JsonObject, key: &str) -> Result<&'a str, ErrorData> {
    opt_str(arguments, key)?
        .ok_or_else(|| ErrorData::invalid_params(format!("{key} is required"), None))
}

fn opt_str<'a>(arguments: &'a JsonObject, key: &str) -> Result<Option<&'a str>, ErrorData> {
    match arguments.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(text)) => Ok(Some(text.as_str())),
        Some(_) => Err(ErrorData::invalid_params(
            format!("{key} must be a string"),
            None,
        )),
    }
}

fn value_arg(arguments: &JsonObject, key: &str) -> Value {
    arguments.get(key).cloned().unwrap_or(Value::Null)
}

fn arg_or(arguments: &JsonObject, key: &str, default: Value) -> Value {
    match arguments.get(key) {
        None | Some(Value::Null) => default,
        Some(value) => value.clone(),
    }
}

fn insert_set(body: &mut Map<String, Value>, arguments: &JsonObject, key: &str) {
    let value = value_arg(arguments, key);
    if !value.is_null() {
        body.insert(key.into(), value);
    }
}

/// Percent-encodes a path segment the way Python's `quote(segment, safe="")` does.
fn quote(segment: &str) -> String {
    let mut quoted = String::with_capacity(segment.len());
    for byte in segment.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'-' | b'~') {
            quoted.push(char::from(byte));
        } else {
            let _ = write!(quoted, "%{byte:02X}");
        }
    }
    quoted
}
