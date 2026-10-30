//! Provider environment for a node launch: the port of the Python spawn environment layers
//! (`prepare_*_spawn`, `_apply_extra_env`, `merge_spawn_path`) and `apply_native_env_policy`.
use super::sandbox::runtime::SandboxLaunch;
use crate::node_commands::SpawnPayload;
use anyhow::Result;
use std::{collections::BTreeMap, path::Path};

/// What the native host spawns with: the argv prefix that unsets denied ambient keys, and
/// the environment.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProviderEnv {
    pub argv_prefix: Vec<String>,
    pub env: BTreeMap<String, String>,
}

/// The inputs `build_env` reads besides the payload.
pub struct EnvInputs<'a> {
    pub cwd: &'a Path,
    pub gobby_home: &'a Path,
    pub front_door_url: &'a str,
    pub prompt_file: &'a Path,
    pub cache_env: &'a BTreeMap<String, String>,
    pub sandbox_launch: &'a SandboxLaunch,
    pub ambient: &'a BTreeMap<String, String>,
}

/// Keys a spawn's `extra_env` may not set: identity, terminal and cache names.
const RESERVED_EXTRA_ENV_KEYS: &[&str] = &[
    "GOBBY_DAEMON_URL",
    "GOBBY_SESSION_ID",
    "GOBBY_PARENT_SESSION_ID",
    "GOBBY_AGENT_RUN_ID",
    "GOBBY_IS_SPAWNED_AGENT",
    "GOBBY_AGENT_API_TOKEN",
    "GOBBY_WORKFLOW_NAME",
    "GOBBY_PROJECT_ID",
    "GOBBY_AGENT_DEPTH",
    "GOBBY_MAX_AGENT_DEPTH",
    "GOBBY_PROMPT",
    "GOBBY_PROMPT_FILE",
    "GOBBY_TERMINAL_ID",
    "GOBBY_NODE_ID",
    "GOBBY_NODE_REF",
    "GOBBY_WORKSPACE_ID",
    "GOBBY_TAB_ID",
    "GOBBY_PANE_ID",
    "GOBBY_PANE_REF",
    "UV_CACHE_DIR",
    "CARGO_HOME",
    "CARGO_TARGET_DIR",
    "GOBBY_MACHINE_ID",
];

const UNIVERSAL_ALLOWLIST: &[&str] = &[
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "PATH",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "CLAUDE_CONFIG_DIR",
    "SSH_AUTH_SOCK",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
];

const MCP_TIMEOUT_VALUE: &str = "120000";
const MCP_CONNECT_TIMEOUT_MS_VALUE: &str = "60000";

/// Layers the identity, front door, prompt file, cache, `extra_env`, provider endpoint and
/// sandbox variables, then applies the native env policy over `ambient`.
pub fn build_env(payload: &SpawnPayload, inputs: &EnvInputs<'_>) -> Result<ProviderEnv> {
    let cli = payload.provider.as_str();
    let mut env = inputs.cache_env.clone();
    insert_identity(&mut env, payload, inputs);
    apply_extra_env(&mut env, payload, inputs.gobby_home, inputs.ambient);
    apply_provider_env(&mut env, payload, inputs.cwd);
    // The sandbox run environment is complete, so its values win over the layers above.
    env.extend(inputs.sandbox_launch.env.clone());
    apply_ambient(&mut env, cli, inputs.ambient);
    let denied = denied_ambient_keys(cli);
    for key in denied {
        env.remove(*key);
    }
    for key in ["VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT"] {
        env.insert(key.to_string(), String::new());
    }
    let argv_prefix = if denied.is_empty() {
        Vec::new()
    } else {
        let mut prefix = vec!["/usr/bin/env".to_string()];
        for key in denied {
            prefix.extend(["-u".to_string(), (*key).to_string()]);
        }
        prefix.push("--".to_string());
        prefix
    };
    Ok(ProviderEnv { argv_prefix, env })
}

fn insert_identity(
    env: &mut BTreeMap<String, String>,
    payload: &SpawnPayload,
    inputs: &EnvInputs<'_>,
) {
    let mut set = |key: &str, value: String| {
        env.insert(key.to_string(), value);
    };
    set("GOBBY_SESSION_ID", payload.session_id.clone());
    set("GOBBY_AGENT_RUN_ID", payload.run_id.clone());
    set("GOBBY_IS_SPAWNED_AGENT", "1".to_string());
    set("GOBBY_PROJECT_ID", payload.project_id.clone());
    set("GOBBY_AGENT_DEPTH", payload.agent_depth.to_string());
    set("GOBBY_MAX_AGENT_DEPTH", payload.max_agent_depth.to_string());
    set("GOBBY_DAEMON_URL", inputs.front_door_url.to_string());
    set(
        "GOBBY_PROMPT_FILE",
        inputs.prompt_file.display().to_string(),
    );
    if let Some(parent) = non_empty(&payload.parent_session_id) {
        set("GOBBY_PARENT_SESSION_ID", parent.to_string());
    }
    if let Some(workflow) = non_empty(&payload.workflow_name) {
        set("GOBBY_WORKFLOW_NAME", workflow.to_string());
    }
}

/// Applies `extra_env`: `PATH` merges with the managed tool bin, reserved keys are skipped,
/// and every other key overrides.
fn apply_extra_env(
    env: &mut BTreeMap<String, String>,
    payload: &SpawnPayload,
    gobby_home: &Path,
    ambient: &BTreeMap<String, String>,
) {
    for (key, value) in &payload.extra_env {
        if key == "PATH" {
            merge_spawn_path(env, value, gobby_home, ambient);
        } else if !RESERVED_EXTRA_ENV_KEYS.contains(&key.as_str()) {
            env.insert(key.clone(), value.clone());
        }
    }
}

/// The provider-specific layer: Claude's managed env and endpoint, and the other providers'
/// endpoint pairs. Each endpoint variable is set only when the payload supplies it.
fn apply_provider_env(env: &mut BTreeMap<String, String>, payload: &SpawnPayload, cwd: &Path) {
    let (base_key, key_key) = match payload.provider.as_str() {
        "claude" => {
            env.insert("CLAUDE_CODE_DISABLE_AUTO_MEMORY".into(), "1".into());
            env.insert(
                "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION".into(),
                "false".into(),
            );
            env.insert("MCP_TIMEOUT".into(), MCP_TIMEOUT_VALUE.into());
            if cwd.join(".mcp.json").exists() {
                env.insert(
                    "MCP_CONNECT_TIMEOUT_MS".into(),
                    MCP_CONNECT_TIMEOUT_MS_VALUE.into(),
                );
            }
            ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")
        }
        "codex" => ("OPENAI_BASE_URL", "OPENAI_API_KEY"),
        "grok" => ("GROK_API_BASE", "XAI_API_KEY"),
        "droid" => ("FACTORY_API_BASE_URL", "FACTORY_API_KEY"),
        _ => return,
    };
    if let Some(base) = non_empty(&payload.api_base) {
        env.insert(base_key.into(), base.into());
    }
    if let Some(token) = non_empty(&payload.api_token) {
        env.insert(key_key.into(), token.into());
    }
}

/// The native env policy's ambient layer: allowlisted keys the spawn does not already set.
fn apply_ambient(
    env: &mut BTreeMap<String, String>,
    cli: &str,
    ambient: &BTreeMap<String, String>,
) {
    for key in UNIVERSAL_ALLOWLIST.iter().chain(cli_allowlist(cli).iter()) {
        let Some(value) = ambient.get(*key) else {
            continue;
        };
        // Empty ambient values pass through only for the Claude secure-storage namespace.
        if value.is_empty() && *key != "CLAUDE_SECURESTORAGE_CONFIG_DIR" {
            continue;
        }
        env.entry((*key).to_string())
            .or_insert_with(|| value.clone());
    }
}

/// Port of `merge_spawn_path`: the preferred entries, then the base entries, deduplicated,
/// with the managed tool bin placed ahead of them.
fn merge_spawn_path(
    env: &mut BTreeMap<String, String>,
    preferred: &str,
    gobby_home: &Path,
    ambient: &BTreeMap<String, String>,
) {
    let base = env
        .get("PATH")
        .or_else(|| ambient.get("PATH"))
        .cloned()
        .unwrap_or_default();
    let managed_bin = gobby_home.join("bin").display().to_string();
    let mut entries: Vec<&str> = Vec::new();
    for entry in preferred.split(':').chain(base.split(':')) {
        if !entry.is_empty() && !entries.contains(&entry) {
            entries.push(entry);
        }
    }
    entries.retain(|entry| *entry != managed_bin);
    let insert_at = entries
        .iter()
        .take_while(|entry| is_isolated_gobby_bin(entry, &managed_bin))
        .count();
    entries.insert(insert_at, &managed_bin);
    env.insert("PATH".to_string(), entries.join(":"));
}

/// Another checkout's `~/.gobby/bin`, which stays ahead of the managed bin only when leading.
fn is_isolated_gobby_bin(entry: &str, managed_bin: &str) -> bool {
    if entry == managed_bin {
        return false;
    }
    let path = Path::new(entry);
    path.file_name().is_some_and(|name| name == "bin")
        && path
            .parent()
            .and_then(Path::file_name)
            .is_some_and(|name| name == ".gobby")
}

fn cli_allowlist(cli: &str) -> &'static [&'static str] {
    match cli {
        "claude" => &[
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_MODEL",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
            "CLAUDE_SECURESTORAGE_CONFIG_DIR",
            "AWS_PROFILE",
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_BEARER_TOKEN_BEDROCK",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
            "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_ENDPOINT",
            "DISABLE_AUTOUPDATER",
        ],
        "codex" => &[
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_ORG_ID",
            "OPENAI_MODEL",
        ],
        "grok" => &["XAI_API_KEY", "GROK_API_KEY", "GROK_API_BASE"],
        "droid" => &[
            "FACTORY_API_KEY",
            "FACTORY_BASE_URL",
            "FACTORY_API_BASE_URL",
        ],
        _ => &[],
    }
}

/// Ambient keys a provider CLI must not inherit, sorted as Python's `denied_ambient_keys`.
fn denied_ambient_keys(cli: &str) -> &'static [&'static str] {
    match cli {
        "agy" => &[
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
        ],
        _ => &[],
    }
}

fn non_empty(value: &Option<String>) -> Option<&str> {
    value.as_deref().filter(|text| !text.is_empty())
}
