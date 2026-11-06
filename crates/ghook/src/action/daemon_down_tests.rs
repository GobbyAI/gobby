use super::*;
use crate::transport::DeliveryFailureKind;

const MESSAGE: &str = "[Gobby] Daemon down. Please wait.";

#[test]
fn spawned_subagent_stop_passes_for_every_cli() {
    let mut failures = Vec::new();
    for (cli, hook) in [
        ("claude", "subagent-stop"),
        ("codex", "SubagentStop"),
        ("droid", "SubagentStop"),
        ("grok", "subagent_stop"),
        ("agy", "SubagentStop"),
    ] {
        let cfg = CliConfig::for_cli(cli).unwrap().with_spawned_agent(true);
        let action = action_from_failure(hook, &cfg, DeliveryFailureKind::Connect, "refused");
        if action.exit_code != 0 || action.stderr_message.is_some() {
            failures.push(format!("{cli} {hook}: {action:?}"));
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

#[test]
fn spawned_codex_interrupt_keeps_base_failure() {
    let cfg = CliConfig::for_cli("codex")
        .unwrap()
        .with_spawned_agent(true);
    let action = action_from_failure("Interrupt", &cfg, DeliveryFailureKind::Connect, "refused");
    assert_eq!(action.exit_code, 1);
    assert!(action.stdout_json.is_none());
    assert_eq!(action.stderr_message.as_deref(), Some("Daemon unreachable"));
}

macro_rules! spawned_config {
    ($name:ident, $cli:literal, [$($hook:literal),+]) => {
        #[test]
        fn $name() {
            let cfg = CliConfig::for_cli($cli).unwrap().with_spawned_agent(true);
            let mut failures = Vec::new();
            for hook in [$($hook),+] {
                let action = action_from_failure(hook, &cfg, DeliveryFailureKind::Connect, "refused");
                if action.exit_code != 2 || action.stderr_message.as_deref() != Some(MESSAGE)
                    || action.stdout_json.is_some() {
                    failures.push(format!("{hook}: {action:?}"));
                }
            }
            assert!(failures.is_empty(), "{}", failures.join("\n"));
        }
    };
}

spawned_config!(
    spawned_claude_connect_blocks,
    "claude",
    [
        "PreToolUse",
        "PostToolUse",
        "session-start",
        "session-end",
        "pre-compact"
    ]
);
spawned_config!(
    spawned_codex_connect_blocks,
    "codex",
    [
        "PreToolUse",
        "PostToolUse",
        "SessionStart",
        "SessionEnd",
        "PreCompact"
    ]
);
spawned_config!(
    spawned_droid_connect_blocks,
    "droid",
    [
        "PreToolUse",
        "PostToolUse",
        "SessionStart",
        "SessionEnd",
        "PreCompact"
    ]
);
spawned_config!(
    spawned_grok_connect_blocks,
    "grok",
    [
        "pre_tool_use",
        "post_tool_use",
        "session_start",
        "session_end",
        "pre_compact"
    ]
);
spawned_config!(
    spawned_agy_connect_blocks,
    "agy",
    [
        "PreToolUse",
        "PostToolUse",
        "SessionStart",
        "SessionEnd",
        "PreCompact"
    ]
);

#[test]
fn spawned_connect_stop_passes_for_every_cli() {
    let mut failures = Vec::new();
    for cli in ["claude", "codex", "droid", "grok", "agy"] {
        let cfg = CliConfig::for_cli(cli).unwrap().with_spawned_agent(true);
        let action = action_from_failure("Stop", &cfg, DeliveryFailureKind::Connect, "refused");
        if action.exit_code != 0 || action.stderr_message.is_some() {
            failures.push(format!("{cli}: {action:?}"));
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

fn hold_call(server: &str, tool: &str) -> serde_json::Value {
    serde_json::json!({
        "tool_name": "mcp__gobby__call_tool",
        "tool_input": {"server_name": server, "tool_name": tool},
    })
}

#[test]
fn spawned_outage_admits_recheck_and_hold_calls_for_every_cli() {
    let wakeup = serde_json::json!({"tool_name": "ScheduleWakeup"});
    let grok_wrapper = serde_json::json!({
        "tool_name": "mcp_gobby_call_tool",
        "tool_input": {"server_name": "gobby-agents", "tool_name": "send_message"},
    });
    let admitted = [
        hold_call("gobby-sessions", "set_handoff"),
        hold_call("gobby-sessions", "get_handoff"),
        hold_call("gobby-sessions", "feedback"),
        hold_call("gobby-agents", "send_message"),
        hold_call("gobby-agents", "wait_for_coordination"),
        grok_wrapper,
        wakeup,
    ];
    let mut failures = Vec::new();
    for (cli, pre, post, prompt) in [
        (
            "claude",
            "pre-tool-use",
            "post-tool-use-failure",
            "user-prompt-submit",
        ),
        ("codex", "PreToolUse", "PostToolUse", "UserPromptSubmit"),
        ("droid", "PreToolUse", "PostToolUse", "UserPromptSubmit"),
        (
            "grok",
            "pre_tool_use",
            "post_tool_use",
            "user_prompt_submit",
        ),
        ("agy", "PreToolUse", "PostToolUse", "UserPromptSubmit"),
    ] {
        let cfg = CliConfig::for_cli(cli).unwrap().with_spawned_agent(true);
        let kind = DeliveryFailureKind::Connect;
        for input in &admitted {
            for hook in [pre, post] {
                let action = crate::daemon_down::action_for(hook, &cfg, kind, input);
                if action.as_ref().is_none_or(|a| a.exit_code != 0) {
                    failures.push(format!("{cli} {hook} {input}: {action:?}"));
                }
            }
        }
        let prompt_action =
            crate::daemon_down::action_for(prompt, &cfg, kind, &serde_json::Value::Null);
        if prompt_action.as_ref().is_none_or(|a| a.exit_code != 0) {
            failures.push(format!("{cli} {prompt}: {prompt_action:?}"));
        }
        for input in [
            serde_json::json!({"tool_name": "Bash"}),
            hold_call("gobby-tasks", "claim_task"),
            hold_call("gobby-agents", "spawn_agent"),
            serde_json::Value::Null,
        ] {
            let action = crate::daemon_down::action_for(pre, &cfg, kind, &input);
            if action.as_ref().is_none_or(|a| a.exit_code != 2) {
                failures.push(format!("{cli} {pre} {input} should pause: {action:?}"));
            }
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

#[test]
fn spawned_outage_admits_a_batch_of_admitted_calls() {
    let cfg = CliConfig::for_cli("claude")
        .unwrap()
        .with_spawned_agent(true);
    let admitted = serde_json::json!({"tool_calls": [
        hold_call("gobby-agents", "wait_for_coordination"),
        {"tool_name": "ScheduleWakeup"},
    ]});
    let action = crate::daemon_down::action_for(
        "post-tool-batch",
        &cfg,
        DeliveryFailureKind::Connect,
        &admitted,
    );
    assert_eq!(action.map(|a| a.exit_code), Some(0));
}

#[test]
fn spawned_timeout_http_and_other_preserve_operator_actions() {
    for cli in ["claude", "codex", "droid", "grok", "agy"] {
        let operator = CliConfig::for_cli(cli).unwrap();
        let spawned = CliConfig::for_cli(cli).unwrap().with_spawned_agent(true);
        for hook in ["PreToolUse", "SessionStart", "Stop"] {
            for kind in [
                DeliveryFailureKind::Timeout,
                DeliveryFailureKind::Http,
                DeliveryFailureKind::Other,
            ] {
                let expected = action_from_failure(hook, &operator, kind, "fixture error");
                let actual = action_from_failure(hook, &spawned, kind, "fixture error");
                assert_eq!(
                    actual.exit_code, expected.exit_code,
                    "{cli} {hook} {kind:?}"
                );
                assert_eq!(actual.stdout_json, expected.stdout_json);
                assert_eq!(actual.stderr_message, expected.stderr_message);
            }
        }
    }
}
