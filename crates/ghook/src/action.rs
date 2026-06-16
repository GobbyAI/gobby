use crate::cli_config::CliConfig;
use crate::json_value::is_python_truthy;
use crate::planned_shutdown::is_stop_hook;
use crate::{output, transport};
use serde_json::Value;
use std::process::ExitCode;

#[derive(Debug, PartialEq, Eq)]
pub(crate) struct HookAction {
    pub(crate) exit_code: u8,
    pub(crate) stdout_json: Option<String>,
    pub(crate) stderr_message: Option<String>,
}

pub(crate) fn continue_action() -> HookAction {
    HookAction {
        exit_code: 0,
        stdout_json: Some(serde_json::json!({"continue": true}).to_string()),
        stderr_message: None,
    }
}

pub(crate) fn emit_empty_json() {
    output::stdout(format_args!("{{}}\n"));
}

pub(crate) fn emit_action(action: HookAction) -> ExitCode {
    if let Some(stdout_json) = action.stdout_json {
        output::stdout(format_args!("{stdout_json}\n"));
    }
    if let Some(stderr_message) = action.stderr_message {
        output::stderr(format_args!("\n{}\n", stderr_message.trim_end()));
    }
    ExitCode::from(action.exit_code)
}

pub(crate) fn action_from_success_response(
    canonical_source: &str,
    hook_type: &str,
    response_body: &str,
) -> Result<HookAction, String> {
    let trimmed = response_body.trim();
    if trimmed.is_empty() {
        return Ok(HookAction {
            exit_code: 0,
            stdout_json: None,
            stderr_message: None,
        });
    }

    let result: Value = serde_json::from_str(trimmed).map_err(|e| e.to_string())?;
    let serialized = serde_json::to_string(&result).map_err(|e| e.to_string())?;

    if canonical_source == "droid" {
        return Ok(action_from_droid_success(result, serialized));
    }

    // Claude carries a structured permissionDecision channel; emitting a
    // second stderr+exit(2) channel on top makes Claude render every
    // PreToolUse deny twice. Mirror the daemon contract: only the
    // top-level continue:false + stopReason shape (HARD_STOP) becomes
    // exit 2. Codex/Gemini/Qwen keep the is_blocked path below.
    if canonical_source == "claude" {
        let map = result.as_object();
        let continue_false =
            map.and_then(|m| m.get("continue")).and_then(Value::as_bool) == Some(false);
        let stop_reason = map
            .and_then(|m| m.get("stopReason"))
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty());

        if continue_false && let Some(reason) = stop_reason {
            return Ok(HookAction {
                exit_code: 2,
                stdout_json: None,
                stderr_message: Some(reason.to_string()),
            });
        }

        return Ok(HookAction {
            exit_code: 0,
            stdout_json: is_python_truthy(&result).then_some(serialized),
            stderr_message: None,
        });
    }

    if is_blocked(&result) {
        if !is_stop_hook(hook_type) {
            return Ok(HookAction {
                exit_code: 0,
                stdout_json: Some(serialized),
                stderr_message: None,
            });
        }
        return Ok(HookAction {
            exit_code: 2,
            stdout_json: None,
            stderr_message: Some(extract_reason(&result)),
        });
    }

    Ok(HookAction {
        exit_code: 0,
        stdout_json: is_python_truthy(&result).then_some(serialized),
        stderr_message: None,
    })
}

fn action_from_droid_success(result: Value, serialized: String) -> HookAction {
    if result
        .as_object()
        .and_then(|map| map.get("continue"))
        .and_then(Value::as_bool)
        == Some(false)
    {
        return HookAction {
            exit_code: 2,
            stdout_json: Some(serialized),
            stderr_message: Some(extract_reason(&result)),
        };
    }

    HookAction {
        exit_code: 0,
        stdout_json: is_python_truthy(&result).then_some(serialized),
        stderr_message: None,
    }
}

pub(crate) fn action_from_failure(
    hook_type: &str,
    cfg: &CliConfig,
    failure_kind: transport::DeliveryFailureKind,
    detail: &str,
) -> HookAction {
    if cfg.source == "droid" {
        let message = match failure_kind {
            transport::DeliveryFailureKind::Http => format!("Daemon error: {detail}"),
            transport::DeliveryFailureKind::Connect => "Daemon unreachable".to_string(),
            transport::DeliveryFailureKind::Timeout => "Hook execution timeout".to_string(),
            transport::DeliveryFailureKind::Other => detail.to_string(),
        };
        return HookAction {
            exit_code: 1,
            stdout_json: None,
            stderr_message: Some(message),
        };
    }

    if cfg.is_critical_hook(hook_type) {
        let reason = match failure_kind {
            transport::DeliveryFailureKind::Http => format!(
                "Hook error on critical hook '{hook_type}' — blocking to fail safe. Detail: {detail}"
            ),
            transport::DeliveryFailureKind::Connect => format!(
                "Daemon connection failed on critical hook '{hook_type}' — blocking to fail safe."
            ),
            transport::DeliveryFailureKind::Timeout => {
                format!("Hook timeout on critical hook '{hook_type}' — blocking to fail safe.")
            }
            transport::DeliveryFailureKind::Other => format!(
                "Hook failure on critical hook '{hook_type}' — blocking to fail safe. Error: {detail}"
            ),
        };
        return HookAction {
            exit_code: 2,
            stdout_json: None,
            stderr_message: Some(reason),
        };
    }

    let message = match failure_kind {
        transport::DeliveryFailureKind::Http => format!("Daemon error: {detail}"),
        transport::DeliveryFailureKind::Connect => "Daemon unreachable".to_string(),
        transport::DeliveryFailureKind::Timeout => "Hook execution timeout".to_string(),
        transport::DeliveryFailureKind::Other => detail.to_string(),
    };

    HookAction {
        exit_code: 1,
        stdout_json: Some(
            serde_json::json!({
                "status": "error",
                "message": message,
            })
            .to_string(),
        ),
        stderr_message: None,
    }
}

fn is_blocked(result: &Value) -> bool {
    let Some(map) = result.as_object() else {
        return false;
    };

    if map.get("continue").and_then(Value::as_bool) == Some(false) {
        return true;
    }
    if matches!(
        map.get("decision").and_then(Value::as_str),
        Some("deny" | "block")
    ) {
        return true;
    }
    if map.get("permissionDecision").and_then(Value::as_str) == Some("deny") {
        return true;
    }
    map.get("hookSpecificOutput")
        .and_then(Value::as_object)
        .and_then(|hook_specific| hook_specific.get("permissionDecision"))
        .and_then(Value::as_str)
        == Some("deny")
}

fn extract_reason(result: &Value) -> String {
    let Some(map) = result.as_object() else {
        return "Blocked by hook".to_string();
    };

    for key in ["stopReason", "user_message", "reason"] {
        if let Some(reason) = map.get(key).and_then(Value::as_str)
            && !reason.is_empty()
        {
            return reason.to_string();
        }
    }

    // Modern Claude Code PreToolUse denies put the reason inside
    // hookSpecificOutput.permissionDecisionReason — mirror the nested lookup
    // already done by is_blocked so users see the daemon's actual message
    // instead of the bare "Blocked by hook" fallback.
    if let Some(hook_specific) = map.get("hookSpecificOutput").and_then(Value::as_object) {
        for key in ["permissionDecisionReason", "reason"] {
            if let Some(reason) = hook_specific.get(key).and_then(Value::as_str)
                && !reason.is_empty()
            {
                return reason.to_string();
            }
        }
    }

    "Blocked by hook".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::transport::DeliveryFailureKind;
    use serde_json::json;

    #[test]
    fn action_from_success_forwards_sessionstart_context_json() {
        let action = action_from_success_response(
            "codex",
            "SessionStart",
            r#"{"decision":"accept","hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"Gobby Session ID: #42 (uuid-123)"}}"#,
        )
        .unwrap();

        assert_eq!(action.exit_code, 0);
        assert_eq!(action.stderr_message, None);
        let stdout_json = action.stdout_json.unwrap();
        let parsed: Value = serde_json::from_str(&stdout_json).unwrap();
        assert_eq!(parsed["decision"], "accept");
        assert_eq!(
            parsed["hookSpecificOutput"]["additionalContext"],
            "Gobby Session ID: #42 (uuid-123)"
        );
        assert_eq!(
            parsed["hookSpecificOutput"]["hookEventName"],
            "SessionStart"
        );
    }

    #[test]
    fn action_from_success_treats_codex_pretool_deny_as_json_block() {
        let action = action_from_success_response(
            "codex",
            "PreToolUse",
            r#"{"decision":"block","reason":"Tool not allowed","hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny"}}"#,
        )
        .unwrap();

        assert_eq!(action.exit_code, 0);
        assert!(
            action
                .stdout_json
                .unwrap()
                .contains(r#""permissionDecision":"deny""#)
        );
        assert_eq!(action.stderr_message, None);
    }

    #[test]
    fn action_from_success_surfaces_nested_permission_decision_reason() {
        let action = action_from_success_response(
            "claude",
            "PreToolUse",
            r#"{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Task #50 requires TDD expansion before edits"}}"#,
        )
        .unwrap();

        assert_eq!(action.exit_code, 0);
        let stdout_json = action.stdout_json.unwrap();
        let parsed: Value = serde_json::from_str(&stdout_json).unwrap();
        assert_eq!(
            parsed["hookSpecificOutput"]["permissionDecisionReason"],
            "Task #50 requires TDD expansion before edits"
        );
        assert_eq!(action.stderr_message, None);
    }

    #[test]
    fn action_from_success_preserves_additional_context_on_claude_block() {
        let action = action_from_success_response(
            "claude",
            "PreToolUse",
            r#"{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Use the python skill","additionalContext":"<skill name=\"python\">body</skill>"}}"#,
        )
        .unwrap();

        assert_eq!(action.exit_code, 0);
        let stdout_json = action.stdout_json.unwrap();
        let parsed: Value = serde_json::from_str(&stdout_json).unwrap();
        assert_eq!(parsed["hookSpecificOutput"]["permissionDecision"], "deny");
        assert_eq!(
            parsed["hookSpecificOutput"]["additionalContext"],
            "<skill name=\"python\">body</skill>"
        );
        assert_eq!(action.stderr_message, None);
    }

    #[test]
    fn action_from_success_preserves_user_prompt_submit_block_json() {
        let action = action_from_success_response(
            "claude",
            "UserPromptSubmit",
            r#"{"decision":"block","reason":"Create a task first","hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"Task schema instructions"}}"#,
        )
        .unwrap();

        assert_eq!(action.exit_code, 0);
        let stdout_json = action.stdout_json.unwrap();
        let parsed: Value = serde_json::from_str(&stdout_json).unwrap();
        assert_eq!(parsed["decision"], "block");
        assert_eq!(parsed["reason"], "Create a task first");
        assert_eq!(
            parsed["hookSpecificOutput"]["additionalContext"],
            "Task schema instructions"
        );
        assert_eq!(action.stderr_message, None);
    }

    #[test]
    fn action_from_success_treats_stop_block_as_exit_two() {
        let action = action_from_success_response(
            "codex",
            "Stop",
            r#"{"decision":"block","reason":"Task still in_progress"}"#,
        )
        .unwrap();

        assert_eq!(
            action,
            HookAction {
                exit_code: 2,
                stdout_json: None,
                stderr_message: Some("Task still in_progress".to_string()),
            }
        );
    }

    #[test]
    fn action_from_success_treats_lowercase_stop_block_as_exit_two() {
        let action = action_from_success_response(
            "grok",
            "stop",
            r#"{"decision":"block","reason":"Grok blocked stop"}"#,
        )
        .unwrap();

        assert_eq!(
            action,
            HookAction {
                exit_code: 2,
                stdout_json: None,
                stderr_message: Some("Grok blocked stop".to_string()),
            }
        );
    }

    #[test]
    fn action_from_success_keeps_non_stop_grok_block_as_json() {
        let action = action_from_success_response(
            "grok",
            "pre_tool_use",
            r#"{"decision":"block","reason":"policy"}"#,
        )
        .unwrap();

        assert_eq!(action.exit_code, 0);
        assert_eq!(
            action.stdout_json.as_deref(),
            Some(r#"{"decision":"block","reason":"policy"}"#)
        );
        assert_eq!(action.stderr_message, None);
    }

    #[test]
    fn action_from_success_claude_hard_stop_exits_two() {
        let action = action_from_success_response(
            "claude",
            "Stop",
            r#"{"continue":false,"stopReason":"Daemon halted run"}"#,
        )
        .unwrap();

        assert_eq!(
            action,
            HookAction {
                exit_code: 2,
                stdout_json: None,
                stderr_message: Some("Daemon halted run".to_string()),
            }
        );
    }

    #[test]
    fn action_from_success_claude_stop_with_permission_deny_no_exit_two() {
        let action = action_from_success_response(
            "claude",
            "Stop",
            r#"{"hookSpecificOutput":{"permissionDecision":"deny","permissionDecisionReason":"r"}}"#,
        )
        .unwrap();

        assert_eq!(action.exit_code, 0);
        assert!(action.stdout_json.is_some());
        assert_eq!(action.stderr_message, None);
    }

    #[test]
    fn action_from_success_claude_continue_false_without_reason_does_not_exit_two() {
        let action =
            action_from_success_response("claude", "Stop", r#"{"continue":false}"#).unwrap();

        assert_eq!(action.exit_code, 0);
        assert_eq!(action.stderr_message, None);
    }

    #[test]
    fn action_from_success_treats_droid_continue_false_as_exit_two_with_json() {
        let action = action_from_success_response(
            "droid",
            "PreToolUse",
            r#"{"continue":false,"stopReason":"Create a task first"}"#,
        )
        .unwrap();

        assert_eq!(action.exit_code, 2);
        let stdout_json = action.stdout_json.unwrap();
        let parsed: Value = serde_json::from_str(&stdout_json).unwrap();
        assert_eq!(parsed["continue"], false);
        assert_eq!(
            action.stderr_message.as_deref(),
            Some("Create a task first")
        );
    }

    #[test]
    fn action_from_success_preserves_droid_block_json_without_exit_two() {
        let action = action_from_success_response(
            "droid",
            "Stop",
            r#"{"decision":"block","reason":"Task still in progress"}"#,
        )
        .unwrap();

        assert_eq!(action.exit_code, 0);
        let stdout_json = action.stdout_json.unwrap();
        let parsed: Value = serde_json::from_str(&stdout_json).unwrap();
        assert_eq!(parsed["decision"], "block");
        assert_eq!(parsed["reason"], "Task still in progress");
        assert_eq!(action.stderr_message, None);
    }

    #[test]
    fn action_from_failure_blocks_critical_hooks() {
        let action = action_from_failure(
            "SessionStart",
            &CliConfig::for_dispatch("codex"),
            DeliveryFailureKind::Http,
            "Internal Server Error",
        );
        assert_eq!(action.exit_code, 2);
        assert!(action.stdout_json.is_none());
        assert!(
            action
                .stderr_message
                .unwrap()
                .contains("Hook error on critical hook 'SessionStart'")
        );
    }

    #[test]
    fn action_from_failure_returns_json_for_noncritical_hooks() {
        let action = action_from_failure(
            "PostToolUse",
            &CliConfig::for_dispatch("codex"),
            DeliveryFailureKind::Connect,
            "ignored",
        );
        assert_eq!(action.exit_code, 1);
        let stdout_json = action.stdout_json.unwrap();
        let parsed: Value = serde_json::from_str(&stdout_json).unwrap();
        assert_eq!(parsed["status"], "error");
        assert_eq!(parsed["message"], "Daemon unreachable");
        assert!(action.stderr_message.is_none());
    }

    #[test]
    fn action_from_failure_treats_timeout_like_python() {
        let action = action_from_failure(
            "PreToolUse",
            &CliConfig::for_dispatch("claude"),
            DeliveryFailureKind::Timeout,
            "timed out reading response",
        );
        assert_eq!(action.exit_code, 1);
        let parsed: Value = serde_json::from_str(action.stdout_json.as_deref().unwrap()).unwrap();
        assert_eq!(parsed["message"], "Hook execution timeout");
    }

    #[test]
    fn action_from_failure_treats_connect_on_critical_hook_as_exit_two() {
        let action = action_from_failure(
            "Stop",
            &CliConfig::for_dispatch("codex"),
            DeliveryFailureKind::Connect,
            "connection failed",
        );
        assert_eq!(action.exit_code, 2);
        assert!(action.stdout_json.is_none());
        assert_eq!(
            action.stderr_message.as_deref(),
            Some("Daemon connection failed on critical hook 'Stop' — blocking to fail safe.")
        );
    }

    #[test]
    fn action_from_failure_returns_stderr_for_droid_transport_errors() {
        let action = action_from_failure(
            "PreToolUse",
            &CliConfig::for_dispatch("droid"),
            DeliveryFailureKind::Http,
            "Internal Server Error",
        );
        assert_eq!(action.exit_code, 1);
        assert!(action.stdout_json.is_none());
        assert_eq!(
            action.stderr_message.as_deref(),
            Some("Daemon error: Internal Server Error")
        );
    }

    #[test]
    fn is_blocked_matches_dispatcher_patterns() {
        assert!(is_blocked(&json!({"continue": false})));
        assert!(is_blocked(&json!({"decision": "deny"})));
        assert!(is_blocked(&json!({"decision": "block"})));
        assert!(is_blocked(&json!({"permissionDecision": "deny"})));
        assert!(is_blocked(
            &json!({"hookSpecificOutput": {"permissionDecision": "deny"}})
        ));
        assert!(!is_blocked(&json!({"decision": "approve"})));
    }
}
