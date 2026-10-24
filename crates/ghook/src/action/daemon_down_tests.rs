use super::*;
use crate::transport::DeliveryFailureKind;

const MESSAGE: &str = "[Gobby] Daemon down. Please wait.";

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
        "UserPromptSubmit",
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
        "UserPromptSubmit",
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
        "UserPromptSubmit",
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
        "user_prompt_submit",
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
        "UserPromptSubmit",
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
