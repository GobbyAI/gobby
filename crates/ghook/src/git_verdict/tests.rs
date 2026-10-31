use super::*;

use crate::action::{
    action_from_failure, action_from_malformed_input, action_from_success_response, continue_action,
};
use crate::cli_config::CliConfig;
use crate::transport::DeliveryFailureKind;

fn git_config() -> CliConfig {
    CliConfig::for_cli("git").expect("git guard must be recognized")
}

fn exit_code(action: &HookAction) -> u8 {
    action.exit_code
}

#[test]
fn only_the_git_source_is_a_git_guard() {
    assert!(is_git_source("git"));
    assert!(!is_git_source("claude"));
    assert!(!is_git_source("Git"));
}

#[test]
fn allow_exits_zero_with_no_output() {
    let action = verdict_from_body(r#"{"decision":"allow"}"#);
    assert_eq!(
        action,
        HookAction {
            exit_code: 0,
            stdout_json: None,
            stderr_message: None,
        }
    );
}

#[test]
fn allow_with_extra_keys_still_allows() {
    let action = verdict_from_body(r#"{"decision":"allow","continue":true,"reason":"ok"}"#);
    assert_eq!(exit_code(&action), 0);
    assert_eq!(action.stderr_message, None);
}

#[test]
fn block_exits_one_with_the_daemon_reason_on_stderr() {
    let action = verdict_from_body(r#"{"decision":"block","reason":"Use a task branch."}"#);
    assert_eq!(exit_code(&action), 1);
    assert_eq!(action.stdout_json, None);
    assert_eq!(action.stderr_message.as_deref(), Some("Use a task branch."));
}

#[test]
fn block_without_a_reason_uses_the_fallback() {
    let action = verdict_from_body(r#"{"decision":"block"}"#);
    assert_eq!(exit_code(&action), 1);
    assert_eq!(
        action.stderr_message.as_deref(),
        Some(BLOCK_FALLBACK_REASON)
    );
}

#[test]
fn block_with_an_empty_reason_uses_the_fallback() {
    let action = verdict_from_body(r#"{"decision":"block","reason":""}"#);
    assert_eq!(exit_code(&action), 1);
    assert_eq!(
        action.stderr_message.as_deref(),
        Some(BLOCK_FALLBACK_REASON)
    );
}

#[test]
fn deny_is_not_a_block_and_fails_closed() {
    assert_eq!(exit_code(&verdict_from_body(r#"{"decision":"deny"}"#)), 2);
}

#[test]
fn decision_match_is_exact() {
    assert_eq!(exit_code(&verdict_from_body(r#"{"decision":"ALLOW"}"#)), 2);
    assert_eq!(exit_code(&verdict_from_body(r#"{"decision":" allow"}"#)), 2);
    assert_eq!(exit_code(&verdict_from_body(r#"{"decision":"Block"}"#)), 2);
}

#[test]
fn missing_decision_fails_closed() {
    assert_eq!(exit_code(&verdict_from_body(r#"{"continue":true}"#)), 2);
    assert_eq!(exit_code(&verdict_from_body(r#"{"decision":null}"#)), 2);
}

#[test]
fn non_object_json_fails_closed() {
    assert_eq!(exit_code(&verdict_from_body("[]")), 2);
    assert_eq!(exit_code(&verdict_from_body("true")), 2);
    assert_eq!(exit_code(&verdict_from_body(r#""allow""#)), 2);
}

#[test]
fn empty_and_unparseable_bodies_fail_closed() {
    assert_eq!(exit_code(&verdict_from_body("")), 2);
    assert_eq!(exit_code(&verdict_from_body("not json")), 2);
    assert_eq!(exit_code(&verdict_from_body("<html>502</html>")), 2);
}

#[test]
fn only_an_http_200_delivery_carries_a_verdict() {
    let allow = r#"{"decision":"allow"}"#;
    assert_eq!(exit_code(&verdict_from_delivery(Some(200), allow)), 0);
    assert_eq!(exit_code(&verdict_from_delivery(Some(201), allow)), 2);
    assert_eq!(exit_code(&verdict_from_delivery(Some(204), allow)), 2);
    assert_eq!(exit_code(&verdict_from_delivery(None, allow)), 2);
}

#[test]
fn a_non_200_block_refuses_without_the_daemon_reason() {
    let block = r#"{"decision":"block","reason":"Use a task branch."}"#;
    assert_eq!(exit_code(&verdict_from_delivery(Some(200), block)), 1);
    let action = verdict_from_delivery(Some(202), block);
    assert_eq!(exit_code(&action), 2);
    assert_eq!(
        action.stderr_message.as_deref(),
        Some("ghook: git guard refused: the daemon did not answer 200 OK")
    );
}

#[test]
fn fail_closed_writes_a_refusal_and_no_stdout() {
    let action = fail_closed("no verdict");
    assert_eq!(exit_code(&action), 2);
    assert_eq!(action.stdout_json, None);
    assert_eq!(
        action.stderr_message.as_deref(),
        Some("ghook: git guard refused: no verdict")
    );
}

#[test]
fn continue_action_fails_closed_for_git() {
    let action = continue_action("git", "PreToolUse");
    assert_eq!(exit_code(&action), 2);
    assert_eq!(action.stdout_json, None);
}

#[test]
fn continue_action_still_fails_open_for_other_sources() {
    assert_eq!(exit_code(&continue_action("claude", "PreToolUse")), 0);
}

#[test]
fn success_response_routes_the_git_verdict() {
    let allow = action_from_success_response("git", "PreToolUse", r#"{"decision":"allow"}"#);
    assert_eq!(allow.map(|action| exit_code(&action)), Ok(0));

    let block = action_from_success_response(
        "git",
        "PreToolUse",
        r#"{"decision":"block","reason":"nope"}"#,
    );
    assert_eq!(block.map(|action| exit_code(&action)), Ok(1));
}

#[test]
fn empty_success_body_fails_closed_for_git() {
    let action = action_from_success_response("git", "PreToolUse", "");
    assert_eq!(action.map(|action| exit_code(&action)), Ok(2));
}

#[test]
fn failure_fails_closed_for_git_on_every_kind() {
    let config = git_config();
    for kind in [
        DeliveryFailureKind::Http,
        DeliveryFailureKind::Connect,
        DeliveryFailureKind::Timeout,
        DeliveryFailureKind::Other,
    ] {
        let action = action_from_failure("PreToolUse", &config, kind, "detail");
        assert_eq!(exit_code(&action), 2);
        assert_eq!(action.stdout_json, None);
    }
}

#[test]
fn failure_fails_closed_for_a_spawned_git_guard() {
    let config = git_config().with_spawned_agent(true);
    let action = action_from_failure(
        "PreToolUse",
        &config,
        DeliveryFailureKind::Connect,
        "connection refused",
    );
    assert_eq!(exit_code(&action), 2);
}

#[test]
fn malformed_stdin_fails_closed_for_git() {
    let action = action_from_malformed_input(&git_config(), "PreToolUse", "bad json");
    assert_eq!(exit_code(&action), 2);
}
