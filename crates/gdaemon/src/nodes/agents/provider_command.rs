//! Provider argv for a node launch: the port of `build_cli_command` and the per-provider
//! additions of the Python `prepare_*_spawn` plans.
use super::sandbox::runtime::SandboxLaunch;
use crate::node_commands::{DroidMode, SpawnPayload};
use anyhow::{Context, Result, bail};
use serde_json::{Map, Value, json};
use std::{collections::BTreeMap, path::Path};

/// Gobby MCP tools Codex may call without prompting.
const CODEX_PREAPPROVED_GOBBY_TOOLS: &[&str] = &[
    "list_mcp_servers",
    "list_tools",
    "get_tool_schema",
    "call_tool",
    "get_variable",
    "set_variable",
];

/// Identity variables the Codex MCP subprocess receives.
const CODEX_MCP_IDENTITY_VARS: &[&str] =
    &["GOBBY_SESSION_ID", "GOBBY_PROJECT_ID", "GOBBY_AGENT_RUN_ID"];

/// The provider's own argv, before the sandbox wrapper: `payload.provider` first. `env` is the
/// resolved provider environment from `build_env`; Codex's runtime overrides read it.
pub fn build_command(
    payload: &SpawnPayload,
    cwd: &Path,
    gobby_home: &Path,
    sandbox_launch: &SandboxLaunch,
    env: &BTreeMap<String, String>,
) -> Result<Vec<String>> {
    Ok(match payload.provider.as_str() {
        "claude" => claude_argv(payload, cwd, sandbox_launch)?,
        "codex" => codex_argv(payload, cwd, gobby_home, sandbox_launch, env),
        "grok" => grok_argv(payload, cwd),
        "droid" => droid_argv(payload, cwd),
        "agy" => agy_argv(payload, cwd),
        other => bail!("no provider command for `{other}`"),
    })
}

fn claude_argv(
    payload: &SpawnPayload,
    cwd: &Path,
    sandbox_launch: &SandboxLaunch,
) -> Result<Vec<String>> {
    let mut argv = vec!["claude".to_string()];
    match non_empty(&payload.resume_session_id) {
        Some(resume) => push_pair(&mut argv, "--resume", resume),
        None => push_pair(&mut argv, "--session-id", &payload.session_id),
    }
    push_optional(&mut argv, "--model", &payload.model);
    push_optional(&mut argv, "--effort", &payload.reasoning_effort);
    argv.push("--dangerously-skip-permissions".into());
    let mcp_config = cwd.join(".mcp.json");
    if mcp_config.exists() {
        push_pair(&mut argv, "--mcp-config", &mcp_config.display().to_string());
        argv.push("--strict-mcp-config".into());
    }
    // The preflighted sandbox arguments outrank caller arguments.
    let (settings, passthrough) =
        managed_claude_settings([&payload.provider_args, &sandbox_launch.provider_args])?;
    push_pair(&mut argv, "--settings", &settings);
    argv.extend(passthrough);
    push_prompt(&mut argv, &payload.prompt);
    Ok(argv)
}

/// Folds every `--settings` pair of `arg_lists` (rising precedence, inline JSON objects only)
/// into one payload, then applies the managed keys on top: a node launch always runs inside
/// SRT, so Claude's own sandbox stays off, and every managed launch disables idle compaction.
/// Returns the compact payload and the remaining arguments in their original order.
fn managed_claude_settings(arg_lists: [&[String]; 2]) -> Result<(String, Vec<String>)> {
    let mut settings = Value::Object(Map::new());
    let mut passthrough = Vec::new();
    for args in arg_lists {
        let mut args = args.iter();
        while let Some(arg) = args.next() {
            if arg != "--settings" {
                passthrough.push(arg.clone());
                continue;
            }
            let raw = args.next().context("Claude --settings has no value")?;
            let payload: Value = serde_json::from_str(raw)
                .context("Claude --settings must be an inline JSON object")?;
            if !payload.is_object() {
                bail!("Claude --settings must be an inline JSON object");
            }
            merge_settings(&mut settings, payload);
        }
    }
    merge_settings(
        &mut settings,
        json!({"sandbox": {"enabled": false}, "idleCompaction": false}),
    );
    Ok((serde_json::to_string(&settings)?, passthrough))
}

/// Overlays `overlay` onto `base`, merging nested objects key by key.
fn merge_settings(base: &mut Value, overlay: Value) {
    match (base, overlay) {
        (Value::Object(base), Value::Object(overlay)) => {
            for (key, value) in overlay {
                match base.get_mut(&key) {
                    Some(existing) if existing.is_object() && value.is_object() => {
                        merge_settings(existing, value);
                    }
                    _ => {
                        base.insert(key, value);
                    }
                }
            }
        }
        (base, overlay) => *base = overlay,
    }
}

fn codex_argv(
    payload: &SpawnPayload,
    cwd: &Path,
    gobby_home: &Path,
    sandbox_launch: &SandboxLaunch,
    env: &BTreeMap<String, String>,
) -> Vec<String> {
    let mut argv = vec!["codex".to_string()];
    let resume = non_empty(&payload.resume_session_id);
    if resume.is_some() {
        argv.push("resume".into());
    }
    match non_empty(&payload.codex_oss_provider) {
        Some(provider) => {
            argv.extend(["--oss".into(), "--local-provider".into(), provider.into()]);
            push_optional(&mut argv, "-m", &payload.model);
        }
        None => push_optional(&mut argv, "--model", &payload.model),
    }
    if let Some(effort) = non_empty(&payload.reasoning_effort) {
        push_config(&mut argv, "model_reasoning_effort", &quoted(effort));
    }
    argv.extend([
        "--ask-for-approval".into(),
        "never".into(),
        "--disable".into(),
        "guardian_approval".into(),
        "-C".into(),
        cwd.display().to_string(),
    ]);
    let gmcp = gobby_home.join("bin").join("gmcp");
    push_config(
        &mut argv,
        "mcp_servers.gobby.command",
        &quoted(&gmcp.display().to_string()),
    );
    push_config(&mut argv, "mcp_servers.gobby.args", "[]");
    push_config(&mut argv, "mcp_servers.gobby.required", "true");
    push_config(&mut argv, "mcp_servers.gobby.startup_timeout_sec", "120");
    push_config(&mut argv, "mcp_servers.gobby.tool_timeout_sec", "360");
    if let Some(tmp) = env.get("TMPDIR") {
        push_config(
            &mut argv,
            "shell_environment_policy.set.TMPDIR",
            &quoted(tmp),
        );
        let tmp_prefix = format!("{tmp}/zsh");
        push_config(
            &mut argv,
            "shell_environment_policy.set.TMPPREFIX",
            &quoted(&tmp_prefix),
        );
        push_config(&mut argv, "mcp_servers.gobby.env.TMPDIR", &quoted(tmp));
        push_config(
            &mut argv,
            "shell_environment_policy.set.OCR_NO_UPDATE",
            &quoted("1"),
        );
    }
    push_env_pair(&mut argv, "GOBBY_HOME", &gobby_home.display().to_string());
    for name in [
        "GOBBY_DAEMON_URL",
        "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
        "GOBBY_MACHINE_ID",
    ] {
        if let Some(value) = env.get(name) {
            push_env_pair(&mut argv, name, value);
        }
    }
    for name in CODEX_MCP_IDENTITY_VARS {
        if let Some(value) = env.get(*name) {
            push_config(
                &mut argv,
                &format!("mcp_servers.gobby.env.{name}"),
                &quoted(value),
            );
        }
    }
    for name in ["UV_CACHE_DIR", "CARGO_HOME", "CARGO_TARGET_DIR"] {
        if let Some(value) = env.get(name) {
            push_env_pair(&mut argv, name, value);
        }
    }
    for tool in CODEX_PREAPPROVED_GOBBY_TOOLS {
        let key = format!("mcp_servers.gobby.tools.{tool}.approval_mode");
        push_config(&mut argv, &key, &quoted("approve"));
    }
    for override_text in &payload.codex_config_overrides {
        argv.extend(["-c".into(), override_text.clone()]);
    }
    push_config(&mut argv, "sandbox_mode", &quoted("danger-full-access"));
    push_config(&mut argv, "check_for_update_on_startup", "false");
    argv.extend(sandbox_launch.provider_args.iter().cloned());
    if let Some(resume) = resume {
        argv.push(resume.into());
    }
    argv
}

fn grok_argv(payload: &SpawnPayload, cwd: &Path) -> Vec<String> {
    let mut argv = vec![
        "grok".to_string(),
        "--always-approve".into(),
        "--no-alt-screen".into(),
        "--cwd".into(),
        cwd.display().to_string(),
    ];
    push_optional(&mut argv, "--model", &payload.model);
    push_optional(&mut argv, "--reasoning-effort", &payload.reasoning_effort);
    push_optional(&mut argv, "--resume", &payload.resume_session_id);
    if non_empty(&payload.execution_mode) != Some("interactive") {
        argv.push("--single".into());
    }
    push_prompt(&mut argv, &payload.prompt);
    argv
}

fn droid_argv(payload: &SpawnPayload, cwd: &Path) -> Vec<String> {
    let mut argv = vec!["droid".to_string()];
    match payload.droid_mode {
        DroidMode::Exec => {
            argv.push("exec".into());
            push_optional(&mut argv, "--session-id", &payload.resume_session_id);
            argv.extend(["--cwd".into(), cwd.display().to_string()]);
            push_optional(&mut argv, "--model", &payload.model);
            push_optional(&mut argv, "--reasoning-effort", &payload.reasoning_effort);
            argv.extend(["--auto".into(), "high".into()]);
        }
        DroidMode::Interactive => {
            push_optional(&mut argv, "--resume", &payload.resume_session_id);
            push_optional(&mut argv, "--model", &payload.model);
            push_optional(&mut argv, "--reasoning-effort", &payload.reasoning_effort);
        }
    }
    push_prompt(&mut argv, &payload.prompt);
    argv
}

fn agy_argv(payload: &SpawnPayload, cwd: &Path) -> Vec<String> {
    let mut argv = vec!["agy".to_string()];
    push_optional(&mut argv, "--conversation", &payload.resume_session_id);
    argv.extend([
        "--dangerously-skip-permissions".into(),
        "--add-dir".into(),
        cwd.display().to_string(),
    ]);
    push_optional(&mut argv, "--model", &payload.model);
    push_optional(&mut argv, "--effort", &payload.reasoning_effort);
    if !payload.prompt.is_empty() {
        push_pair(&mut argv, "--print", &payload.prompt);
    }
    argv.push("--sandbox=false".into());
    argv
}

fn non_empty(value: &Option<String>) -> Option<&str> {
    value.as_deref().filter(|text| !text.is_empty())
}

fn push_pair(argv: &mut Vec<String>, flag: &str, value: &str) {
    argv.extend([flag.to_string(), value.to_string()]);
}

fn push_optional(argv: &mut Vec<String>, flag: &str, value: &Option<String>) {
    if let Some(value) = non_empty(value) {
        push_pair(argv, flag, value);
    }
}

/// The prompt is the last argument; an empty prompt adds nothing.
fn push_prompt(argv: &mut Vec<String>, prompt: &str) {
    if !prompt.is_empty() {
        argv.push(prompt.to_string());
    }
}

fn push_config(argv: &mut Vec<String>, key: &str, value: &str) {
    argv.extend(["-c".to_string(), format!("{key}={value}")]);
}

/// Sets a variable for Codex's MCP subprocess and its shell.
fn push_env_pair(argv: &mut Vec<String>, name: &str, value: &str) {
    push_config(
        argv,
        &format!("mcp_servers.gobby.env.{name}"),
        &quoted(value),
    );
    push_config(
        argv,
        &format!("shell_environment_policy.set.{name}"),
        &quoted(value),
    );
}

/// A TOML basic string as Codex reads it: JSON string syntax, the Python port's `json.dumps`.
fn quoted(value: &str) -> String {
    Value::String(value.to_string()).to_string()
}
