//! Terminal signals sent by the MCP wrapper (not the SESSION_START hook).

use crate::contract::WRAPPER_CONTRACT;
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use std::io::Read;
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const GENERATION_FORMAT: &str = "#{pid}\t#{start_time}\t#{window_id}\t#{session_name}";
const IDENTITY_FORMAT: &str = "#{window_id}\t#{session_name}";

pub fn capture() -> Map<String, Value> {
    #[cfg(unix)]
    let parent = Some(std::os::unix::process::parent_id());
    #[cfg(not(unix))]
    let parent = None;
    capture_with(&std::env::vars().collect(), parent, run_tmux)
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
    Value::Object(filtered).to_string()
}

fn capture_with(
    environment: &BTreeMap<String, String>,
    parent: Option<u32>,
    mut runner: impl FnMut(&str, &str, &str) -> Option<String>,
) -> Map<String, Value> {
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
    let pane = env("TMUX_PANE");
    if let Some(pane) = pane {
        context.insert("tmux_pane".into(), json!(pane));
    }
    if let Some(socket) = env("TMUX")
        .and_then(|value| value.split(',').next())
        .filter(|v| !v.is_empty())
    {
        context.insert("tmux_socket_path".into(), json!(socket));
        if let Some(pane) = pane.filter(|value| valid_id(value, '%')) {
            let generation = runner(socket, pane, GENERATION_FORMAT)
                .and_then(|output| parse_generation(&output));
            if let Some((pid, start, window, session)) = generation {
                context.insert("tmux_server_pid".into(), json!(pid));
                context.insert("tmux_server_start_time".into(), json!(start));
                context.insert("tmux_window_id".into(), json!(window));
                context.insert("tmux_session".into(), json!(session));
            } else if let Some((window, session)) =
                runner(socket, pane, IDENTITY_FORMAT).and_then(|output| parse_identity(&output))
            {
                context.insert("tmux_window_id".into(), json!(window));
                context.insert("tmux_session".into(), json!(session));
            }
        }
    }
    for (env_name, key) in [
        ("TMUX_SESSION", "tmux_session"),
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

fn valid_id(value: &str, prefix: char) -> bool {
    value.strip_prefix(prefix).is_some_and(|suffix| {
        !suffix.is_empty() && suffix.bytes().all(|byte| byte.is_ascii_digit())
    })
}

fn parse_generation(output: &str) -> Option<(u64, u64, String, String)> {
    let parts: Vec<_> = output.trim().split('\t').collect();
    if parts.len() != 4
        || !valid_id(parts[2], '@')
        || parts[3].is_empty()
        || !parts[..2]
            .iter()
            .all(|field| !field.is_empty() && field.bytes().all(|b| b.is_ascii_digit()))
    {
        return None;
    }
    Some((
        parts[0].parse().ok()?,
        parts[1].parse().ok()?,
        parts[2].into(),
        parts[3].into(),
    ))
}

fn parse_identity(output: &str) -> Option<(String, String)> {
    let (window, session) = output.trim().split_once('\t')?;
    (valid_id(window, '@') && !session.is_empty()).then(|| (window.into(), session.into()))
}

fn run_tmux(socket: &str, pane: &str, format: &str) -> Option<String> {
    let mut child = Command::new("tmux")
        .args(["-S", socket, "display-message", "-p", "-t", pane, format])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let deadline = Instant::now() + Duration::from_millis(500);
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                if !status.success() {
                    return None;
                }
                let mut output = String::new();
                child.stdout.take()?.read_to_string(&mut output).ok()?;
                return Some(output);
            }
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(10)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    }
}

#[cfg(test)]
#[path = "terminal_context/tests.rs"]
mod tests;
