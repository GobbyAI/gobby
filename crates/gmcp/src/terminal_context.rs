//! Terminal signals sent by the MCP wrapper (not the SESSION_START hook).

use crate::contract::WRAPPER_CONTRACT;
use gobby_core::json::{COMPACT, Json, dumps};
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;

pub fn capture() -> Map<String, Value> {
    #[cfg(unix)]
    let parent = Some(std::os::unix::process::parent_id());
    #[cfg(not(unix))]
    let parent = None;
    capture_with(&environment(), parent)
}

fn environment() -> BTreeMap<String, String> {
    // A value that is not UTF-8 cannot be carried as a JSON string, so it is skipped.
    std::env::vars_os()
        .filter_map(|(name, value)| Some((name.into_string().ok()?, value.into_string().ok()?)))
        .collect()
}

pub fn serialize(context: &Map<String, Value>) -> String {
    let filtered: Map<String, Value> = WRAPPER_CONTRACT
        .terminal_context_keys
        .iter()
        .filter_map(|key| {
            context
                .get(key)
                .filter(|value| !value.is_null())
                .map(|value| (key.clone(), value.clone()))
        })
        .collect();
    // Python writes `json.dumps(allowlisted, separators=(",", ":"))`.
    dumps(&Json::from(&Value::Object(filtered)), COMPACT)
        .expect("COMPACT writes every float a Value can hold")
}

fn capture_with(environment: &BTreeMap<String, String>, parent: Option<u32>) -> Map<String, Value> {
    let mut context = Map::new();
    if let Some(parent) = parent {
        context.insert("parent_pid".into(), json!(parent));
    }
    let env = |name: &str| {
        environment
            .get(name)
            .map(String::as_str)
            .filter(|v| !v.is_empty())
    };
    for (env_name, key) in [
        ("TTY", "tty"),
        ("TERM_PROGRAM", "term_program"),
        ("TERM_SESSION_ID", "term_session_id"),
        ("GOBBY_TERMINAL_ID", "gobby_terminal_id"),
        ("GOBBY_PANE_REF", "gobby_pane_ref"),
    ] {
        if let Some(value) = env(env_name) {
            context.entry(key).or_insert_with(|| json!(value));
        }
    }
    context
}

#[cfg(test)]
#[path = "terminal_context/tests.rs"]
mod tests;
