//! Response shaping for the wrapper's tools, following the Python wrapper's output rules.

use serde_json::{Map, Value, json};

/// The daemon's project id for the global scope.
const GLOBAL_PROJECT_ID: &str = "00000000-0000-0000-0000-000000000002";

/// Recursively removes `null` object fields, as the Python wrapper's `_strip_none` does.
pub fn strip_none(value: Value) -> Value {
    match value {
        Value::Object(map) => Value::Object(
            map.into_iter()
                .filter(|(_, value)| !value.is_null())
                .map(|(key, value)| (key, strip_none(value)))
                .collect(),
        ),
        Value::Array(items) => Value::Array(items.into_iter().map(strip_none).collect()),
        other => other,
    }
}

/// Compacts a server list the way `compact_mcp_server_list` does, without ids.
pub fn compact_server_list(result: Value) -> Value {
    if result.get("success") == Some(&Value::Bool(false)) {
        return result;
    }
    let Some(servers) = result.get("servers").and_then(Value::as_array) else {
        return result;
    };
    let mut rows = Vec::new();
    let mut issues = Vec::new();
    for server in servers {
        if let Some(name) = server.as_str() {
            rows.push(json!({"name": name}));
            continue;
        }
        let Some(object) = server.as_object() else {
            continue;
        };
        let Some(name) = non_empty(object, "name") else {
            continue;
        };
        let state = object
            .get("state")
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        let enabled = !matches!(object.get("enabled"), Some(Value::Bool(false)));
        let transport = non_empty(object, "transport");
        let mut row = Map::new();
        row.insert("name".into(), json!(name));
        row.insert("state".into(), json!(state));
        row.insert("enabled".into(), json!(enabled));
        if let Some(transport) = transport {
            row.insert("transport".into(), json!(transport));
        }
        if let Some(scope) = non_empty(object, "scope") {
            row.insert("scope".into(), json!(scope));
        } else if object.get("project_id").and_then(Value::as_str) == Some(GLOBAL_PROJECT_ID) {
            row.insert("scope".into(), json!("global"));
        }
        if let Some(template) = non_empty(object, "template") {
            row.insert("template".into(), json!(template));
        }
        rows.push(Value::Object(row));
        if state == "connected" && enabled {
            continue;
        }
        let mut issue = Map::new();
        issue.insert("name".into(), json!(name));
        issue.insert("state".into(), json!(state));
        if let Some(transport) = transport {
            issue.insert("transport".into(), json!(transport));
        }
        if !enabled {
            issue.insert("enabled".into(), json!(false));
        }
        issues.push(Value::Object(issue));
    }
    let mut compact = Map::new();
    if let Some(success) = result.get("success") {
        compact.insert("success".into(), success.clone());
    }
    compact.insert("servers".into(), Value::Array(rows));
    for key in ["total", "connected"] {
        if let Some(value) = result.get(key) {
            compact.insert(key.into(), value.clone());
        }
    }
    if !issues.is_empty() {
        compact.insert("issues".into(), Value::Array(issues));
    }
    if let Some(extra) = result.as_object() {
        for (key, value) in extra {
            if !matches!(
                key.as_str(),
                "success" | "servers" | "total" | "connected" | "issues"
            ) {
                compact.insert(key.clone(), value.clone());
            }
        }
    }
    Value::Object(compact)
}

/// Shapes a schema answer as `get_tool_schema` does: an error keeps only its message.
pub fn tool_schema(result: Value) -> Value {
    if let Some(error) = result.get("error") {
        return json!({"success": false, "error": error.clone()});
    }
    strip_none(json!({
        "success": true,
        "tool": {
            "name": result.get("name").cloned().unwrap_or(Value::Null),
            "description": result.get("description").cloned().unwrap_or(Value::Null),
            "inputSchema": result.get("inputSchema").cloned().unwrap_or(Value::Null),
        },
    }))
}

fn non_empty<'a>(object: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
    object
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
}
