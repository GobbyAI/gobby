//! Spawned sessions pause on a refused daemon connection; operators can repair it.

use crate::action::{HookAction, continue_action, is_codex_interrupt};
use crate::cli_config::CliConfig;
use crate::planned_shutdown::{is_stop_hook, restart_in_progress};
use crate::transport::DeliveryFailureKind;

pub(crate) fn action(
    hook_type: &str,
    cfg: &CliConfig,
    failure_kind: DeliveryFailureKind,
) -> Option<HookAction> {
    if !cfg.spawned_agent
        || !matches!(failure_kind, DeliveryFailureKind::Connect)
        || is_codex_interrupt(cfg.source, hook_type)
    {
        return None;
    }
    // Claude ends the turn on a batch hook's exit 2. During a restart the turn
    // continues instead; the next tool call still pauses at its pre-tool hook.
    let restart_gap = hook_type == "post-tool-batch" && restart_in_progress();
    Some(if is_stop_like_hook(hook_type) || restart_gap {
        continue_action(cfg.source, hook_type)
    } else {
        HookAction {
            exit_code: 2,
            stdout_json: None,
            stderr_message: Some("[Gobby] Daemon down. Please wait.".to_string()),
        }
    })
}

/// Gobby calls a held seat still makes while the daemon is down: keeping its
/// handoff, renewing its wait and reporting. gmcp retries them through a restart.
const HOLD_CALLS: [(&str, &str); 5] = [
    ("gobby-sessions", "set_handoff"),
    ("gobby-sessions", "get_handoff"),
    ("gobby-sessions", "feedback"),
    ("gobby-agents", "send_message"),
    ("gobby-agents", "wait_for_coordination"),
];

const CALL_TOOL_NAMES: [&str; 3] = ["call_tool", "mcp__gobby__call_tool", "mcp_gobby_call_tool"];

/// Like [`action`], but lets a spawned seat's own re-check through: the prompt a
/// scheduled wakeup delivers, `ScheduleWakeup` itself, and the hold calls. Any
/// other tool call still pauses on "Daemon down".
pub(crate) fn action_for(
    hook_type: &str,
    cfg: &CliConfig,
    failure_kind: DeliveryFailureKind,
    input: &serde_json::Value,
) -> Option<HookAction> {
    let paused = action(hook_type, cfg, failure_kind)?;
    if paused.exit_code == 2 && admitted_during_outage(hook_type, input) {
        return Some(continue_action(cfg.source, hook_type));
    }
    Some(paused)
}

fn admitted_during_outage(hook_type: &str, input: &serde_json::Value) -> bool {
    let hook: String = hook_type
        .chars()
        .filter(|c| *c != '-' && *c != '_')
        .map(|c| c.to_ascii_lowercase())
        .collect();
    match hook.as_str() {
        "userpromptsubmit" => true,
        "pretooluse" | "posttooluse" | "posttoolusefailure" | "beforetool" | "aftertool"
        | "aftertoolfailure" => is_hold_call(input),
        "posttoolbatch" => input
            .get("tool_calls")
            .and_then(serde_json::Value::as_array)
            .is_some_and(|calls| !calls.is_empty() && calls.iter().all(is_hold_call)),
        _ => false,
    }
}

fn text(value: Option<&serde_json::Value>) -> Option<&str> {
    value.and_then(serde_json::Value::as_str)
}

fn is_hold_call(call: &serde_json::Value) -> bool {
    match text(call.get("tool_name")) {
        Some("ScheduleWakeup") => true,
        Some(name) if CALL_TOOL_NAMES.contains(&name) => {
            let args = call.get("tool_input");
            let server = text(args.and_then(|a| a.get("server_name")));
            let tool = text(args.and_then(|a| a.get("tool_name")));
            HOLD_CALLS
                .iter()
                .any(|(s, t)| server == Some(*s) && tool == Some(*t))
        }
        _ => false,
    }
}

fn is_stop_like_hook(hook_type: &str) -> bool {
    is_stop_hook(hook_type)
        || matches!(
            hook_type,
            "subagent-stop" | "SubagentStop" | "subagent_stop"
        )
}
