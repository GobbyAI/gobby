//! ghook — sandbox-tolerant hook dispatcher.
//!
//! Three modes:
//!   `ghook --gobby-owned --cli=<c> --type=<t> [--detach]`
//!   `ghook --diagnose    --cli=<c> --type=<t>`
//!   `ghook --version`
//!
//! Mode 1 enqueues an envelope to `~/.gobby/hooks/inbox/` and attempts a
//! POST to the daemon. The enqueue-first transport is an internal detail;
//! stdout, stderr, and exit codes follow the current per-CLI hook protocol.
//!
//! Mode 2 prints a JSON diagnostic, no network, no envelope write.
//!
//! Mode 3 prints the ghook version and writes
//! `~/.gobby/bin/.ghook-runtime.json` with `{schema_version, ghook_version}`.

use anyhow::Result;
use clap::Parser;
use serde_json::Value;
use std::collections::BTreeMap;
use std::io::Read;
use std::path::PathBuf;
use std::process::ExitCode;

mod cli_config;
mod detach;
mod diagnose;
mod envelope;
mod json_value;
mod output;
mod planned_shutdown;
mod source;
mod statusline;
mod terminal_context;
#[cfg(test)]
mod test_http;
mod transport;

use cli_config::CliConfig;
use envelope::Envelope;
use json_value::is_python_truthy;
use source::detect_source;

#[derive(Debug, PartialEq, Eq)]
struct HookAction {
    exit_code: u8,
    stdout_json: Option<String>,
    stderr_message: Option<String>,
}

#[derive(Parser, Debug)]
#[command(
    name = "ghook",
    about = "Gobby sandbox-tolerant hook dispatcher",
    disable_version_flag = true
)]
struct Args {
    /// Normal hook-invocation mode. Required for enqueue/POST.
    #[arg(long)]
    gobby_owned: bool,

    /// Print diagnostic JSON for the given cli/type, then exit.
    #[arg(long)]
    diagnose: bool,

    /// Print version and write ~/.gobby/bin/.ghook-runtime.json stamp.
    #[arg(long)]
    version: bool,

    /// Host CLI name (claude, codex, gemini, qwen, droid).
    #[arg(long)]
    cli: Option<String>,

    /// Hook type (e.g. session-start, SessionStart, PreToolUse).
    #[arg(long = "type")]
    hook_type: Option<String>,

    /// Detach from the parent's session/process group before the POST.
    #[arg(long)]
    detach: bool,
}

fn main() -> ExitCode {
    let args = Args::parse();

    if args.version {
        return match write_runtime_stamp() {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                // Still print the version; stamp-write failure is non-fatal.
                output::stdout(format_args!("ghook {}\n", diagnose::GHOOK_VERSION));
                output::stderr(format_args!("note: could not write runtime stamp: {e}\n"));
                ExitCode::SUCCESS
            }
        };
    }
    if args.diagnose {
        return run_diagnose(&args);
    }
    if args.gobby_owned {
        return run_gobby_owned(&args);
    }

    eprintln!("ghook: no mode specified; use one of --gobby-owned, --diagnose, or --version");
    ExitCode::from(2)
}

fn run_diagnose(args: &Args) -> ExitCode {
    let (Some(cli), Some(hook_type)) = (args.cli.as_deref(), args.hook_type.as_deref()) else {
        eprintln!("--diagnose requires --cli and --type");
        return ExitCode::from(2);
    };
    let out = diagnose::diagnose(cli, hook_type);
    match serde_json::to_string_pretty(&out) {
        Ok(s) => {
            println!("{s}");
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("diagnose serialization failed: {e}");
            ExitCode::from(2)
        }
    }
}

fn run_gobby_owned(args: &Args) -> ExitCode {
    let (Some(cli), Some(hook_type)) = (args.cli.as_deref(), args.hook_type.as_deref()) else {
        println!("{}", serde_json::json!({}));
        return ExitCode::from(2);
    };

    // Daemon-spawned ACP subprocesses (gemini --acp, qwen --acp) set
    // GOBBY_HOOKS_DISABLED=1 to stop their inherited SessionStart hook from
    // registering phantom sessions. Short-circuit before any side effects: no
    // enqueue, no POST, no terminal-context enrichment.
    if hooks_disabled_by_env() {
        if statusline::is_statusline_hook(cli, hook_type) {
            return ExitCode::SUCCESS;
        }
        println!("{}", serde_json::json!({}));
        return ExitCode::SUCCESS;
    }

    if planned_shutdown::should_skip_dispatch(hook_type) {
        return emit_action(continue_action());
    }

    if statusline::is_statusline_hook(cli, hook_type) {
        let mut stdin_raw = Vec::with_capacity(4096);
        if let Err(e) = std::io::stdin().read_to_end(&mut stdin_raw) {
            eprintln!("ghook statusline: failed to read stdin: {e}");
            return ExitCode::SUCCESS;
        }
        return statusline::handle(&stdin_raw);
    }

    let cfg = CliConfig::for_dispatch(cli);
    let is_critical = cfg.is_critical_hook(hook_type);

    // IMPORTANT: walk up for project context BEFORE any detach.
    // Sandbox FS-read denials or a detached process's cwd semantics on
    // macOS would otherwise surprise us (plan :76).
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let project_root = gobby_core::project::find_project_root(&cwd);
    let project_id = project_root
        .as_ref()
        .and_then(|r| gobby_core::project::read_project_id(r).ok());

    // Read stdin before detach too — detach closes the controlling TTY but
    // stdin pipes from the host CLI should still be intact; read now to
    // avoid late-read surprises if the host closes the pipe on exit.
    let mut stdin_raw = Vec::with_capacity(4096);
    let read_ok = std::io::stdin().read_to_end(&mut stdin_raw).is_ok();

    // Parse. Empty stdin is a parse error in the Python dispatcher too.
    let parsed: Result<Value, serde_json::Error> = if read_ok {
        serde_json::from_slice(&stdin_raw)
    } else {
        Err(serde_json::Error::io(std::io::Error::other(
            "failed to read stdin",
        )))
    };

    let input_data = match parsed {
        Ok(v) => v,
        Err(e) => {
            let _ = transport::quarantine_malformed(&stdin_raw, &e.to_string(), is_critical);
            println!("{}", serde_json::json!({}));
            return ExitCode::from(cfg.json_error_exit_code);
        }
    };

    let env = build_dispatch_envelope(&cfg, hook_type, input_data, project_id.as_deref());

    let direct_post_after_enqueue_failure = |failure_detail: String| -> HookAction {
        // Mirror the normal enqueue→detach→POST ordering: a --detach run must
        // escape the host's process group before the bounded fallback POST,
        // or a host group-kill can reap us mid-delivery with no action emitted.
        if args.detach {
            detach::detach();
        }
        let daemon_url = gobby_core::daemon_url::daemon_url();
        // No inbox file exists here; post_and_cleanup ignores remove errors after 2xx.
        let missing_enqueued_path = PathBuf::new();
        let report = transport::post_and_cleanup(&env, &missing_enqueued_path, &daemon_url);
        match report.outcome {
            transport::DeliveryOutcome::Delivered => {
                let body = report.response_body.as_deref().unwrap_or_default();
                match action_from_success_response(cfg.source, hook_type, body) {
                    Ok(action) => action,
                    Err(error) => action_from_failure(
                        hook_type,
                        &cfg,
                        transport::DeliveryFailureKind::Other,
                        &error,
                    ),
                }
            }
            transport::DeliveryOutcome::Enqueued => action_from_failure(
                hook_type,
                &cfg,
                transport::DeliveryFailureKind::Other,
                &failure_detail,
            ),
        }
    };

    // Enqueue first (atomic write to ~/.gobby/hooks/inbox/).
    let inbox = match transport::inbox_dir() {
        Ok(d) => d,
        Err(e) => {
            return emit_action(direct_post_after_enqueue_failure(e.to_string()));
        }
    };
    let enqueued_path = match transport::enqueue_to(&env, &inbox) {
        Ok(p) => p,
        Err(e) => {
            return emit_action(direct_post_after_enqueue_failure(e.to_string()));
        }
    };

    // Detach *after* project walk-up and enqueue — the file on disk is
    // now the source of truth even if we die mid-POST.
    if args.detach {
        detach::detach();
    }

    // Best-effort POST. Enqueue file is deleted on 2xx; otherwise kept.
    let daemon_url = gobby_core::daemon_url::daemon_url();
    let report = transport::post_and_cleanup(&env, &enqueued_path, &daemon_url);
    let action = match report.outcome {
        transport::DeliveryOutcome::Delivered => {
            let body = report.response_body.as_deref().unwrap_or_default();
            match action_from_success_response(cfg.source, hook_type, body) {
                Ok(action) => action,
                Err(error) => action_from_failure(
                    hook_type,
                    &cfg,
                    transport::DeliveryFailureKind::Other,
                    &error,
                ),
            }
        }
        transport::DeliveryOutcome::Enqueued => {
            if planned_shutdown::suppress_after_failed_post(
                hook_type,
                report.failure_kind,
                &enqueued_path,
            ) {
                return emit_action(continue_action());
            }

            let detail = report
                .response_body
                .or(report.transport_error)
                .unwrap_or_default();
            action_from_failure(
                hook_type,
                &cfg,
                report
                    .failure_kind
                    .unwrap_or(transport::DeliveryFailureKind::Other),
                &detail,
            )
        }
    };

    emit_action(action)
}

fn continue_action() -> HookAction {
    HookAction {
        exit_code: 0,
        stdout_json: Some(serde_json::json!({"continue": true}).to_string()),
        stderr_message: None,
    }
}

fn hooks_disabled_by_env() -> bool {
    std::env::var_os("GOBBY_HOOKS_DISABLED").is_some_and(|v| v == "1")
}

fn build_dispatch_envelope(
    cfg: &CliConfig,
    hook_type: &str,
    mut input_data: Value,
    project_id: Option<&str>,
) -> Envelope {
    if terminal_context::enabled_for_hook(hook_type) {
        terminal_context::inject(&mut input_data);
    }

    // Headers: omit on missing (never empty string).
    let mut headers: BTreeMap<String, String> = BTreeMap::new();
    if let Some(pid) = project_id {
        headers.insert("X-Gobby-Project-Id".into(), pid.to_string());
    }
    if let Some(sid) = input_data.get("session_id").and_then(|v| v.as_str())
        && !sid.is_empty()
    {
        headers.insert("X-Gobby-Session-Id".into(), sid.to_string());
    }

    Envelope::new(
        cfg.is_critical_hook(hook_type),
        hook_type.to_string(),
        input_data,
        detect_source(cfg),
        headers,
    )
}

fn emit_action(action: HookAction) -> ExitCode {
    if let Some(stdout_json) = action.stdout_json {
        output::stdout(format_args!("{stdout_json}\n"));
    }
    if let Some(stderr_message) = action.stderr_message {
        output::stderr(format_args!("\n{}\n", stderr_message.trim_end()));
    }
    ExitCode::from(action.exit_code)
}

fn action_from_success_response(
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
        if hook_type != "Stop" {
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

fn action_from_failure(
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

fn write_runtime_stamp() -> Result<()> {
    let bin_dir = gobby_core::gobby_home()?.join("bin");
    std::fs::create_dir_all(&bin_dir)?;
    let stamp_path = bin_dir.join(".ghook-runtime.json");
    let stamp = serde_json::json!({
        "schema_version": envelope::SCHEMA_VERSION,
        "ghook_version": diagnose::GHOOK_VERSION,
    });
    let bytes = serde_json::to_vec_pretty(&stamp)?;
    transport::atomic_write(&stamp_path, &bytes)?;
    output::stdout(format_args!("ghook {}\n", diagnose::GHOOK_VERSION));
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::transport::DeliveryFailureKind;
    use serde_json::json;
    use std::sync::Mutex;

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn with_tmux_env<T>(tmux: Option<&str>, tmux_pane: Option<&str>, f: impl FnOnce() -> T) -> T {
        let _guard = ENV_LOCK.lock().unwrap();
        temp_env::with_vars([("TMUX", tmux), ("TMUX_PANE", tmux_pane)], f)
    }

    #[test]
    fn dispatch_envelope_injects_valid_tmux_pane_for_session_start() {
        with_tmux_env(Some("/tmp/tmux-501/default,12345,0"), Some("%17"), || {
            let cfg = CliConfig::for_dispatch("grok");
            let envelope = build_dispatch_envelope(
                &cfg,
                "SessionStart",
                json!({"session_id": "sess-1"}),
                None,
            );

            assert_eq!(envelope.input_data["terminal_context"]["tmux_pane"], "%17");
        });
    }

    #[test]
    fn dispatch_envelope_omits_terminal_context_for_tool_hooks() {
        with_tmux_env(Some("/tmp/tmux-501/default,12345,0"), Some("%17"), || {
            let cfg = CliConfig::for_dispatch("codex");
            let envelope =
                build_dispatch_envelope(&cfg, "PreToolUse", json!({"session_id": "sess-1"}), None);

            assert!(envelope.input_data.get("terminal_context").is_none());
        });
    }

    #[test]
    fn dispatch_envelope_nulls_tmux_fields_for_missing_or_invalid_tmux_pane() {
        for pane in [None, Some(""), Some("17"), Some("%"), Some("%x")] {
            with_tmux_env(Some("/tmp/tmux-501/default,12345,0"), pane, || {
                let cfg = CliConfig::for_dispatch("gemini");
                let envelope = build_dispatch_envelope(
                    &cfg,
                    "SessionStart",
                    json!({"session_id": "sess-1"}),
                    None,
                );

                assert_eq!(
                    envelope.input_data["terminal_context"]["tmux_pane"],
                    json!(null)
                );
                assert_eq!(
                    envelope.input_data["terminal_context"]["tmux_socket_path"],
                    json!(null)
                );
            });
        }

        with_tmux_env(None, Some("%17"), || {
            let cfg = CliConfig::for_dispatch("gemini");
            let envelope = build_dispatch_envelope(
                &cfg,
                "SessionStart",
                json!({"session_id": "sess-1"}),
                None,
            );

            assert_eq!(
                envelope.input_data["terminal_context"]["tmux_pane"],
                json!(null)
            );
            assert_eq!(
                envelope.input_data["terminal_context"]["tmux_socket_path"],
                json!(null)
            );
        });
    }

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
    fn hooks_disabled_by_env_reads_env_var() {
        // Avoid racing other tests that read GOBBY_* env vars — touching the
        // process env from tests is inherently global, but the key we use is
        // unique to this check.
        // SAFETY: single-threaded Rust tests within this module; no other test
        // reads or writes GOBBY_HOOKS_DISABLED.
        unsafe {
            std::env::remove_var("GOBBY_HOOKS_DISABLED");
        }
        assert!(!hooks_disabled_by_env());

        unsafe {
            std::env::set_var("GOBBY_HOOKS_DISABLED", "1");
        }
        assert!(hooks_disabled_by_env());

        unsafe {
            std::env::set_var("GOBBY_HOOKS_DISABLED", "0");
        }
        assert!(!hooks_disabled_by_env(), "only '1' should short-circuit");

        unsafe {
            std::env::set_var("GOBBY_HOOKS_DISABLED", "");
        }
        assert!(
            !hooks_disabled_by_env(),
            "empty string should not short-circuit"
        );

        unsafe {
            std::env::remove_var("GOBBY_HOOKS_DISABLED");
        }
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
