//! ghook — sandbox-tolerant hook dispatcher.
//!
//! Three modes:
//!   `ghook --gobby-owned --cli=<c> --type=<t> [--critical] [--detach]`
//!   `ghook --diagnose    --cli=<c> --type=<t>`
//!   `ghook --version`
//!
//! Mode 1 enqueues an envelope to `~/.gobby/hooks/inbox/` and attempts a
//! POST to the daemon. The enqueue-first transport is an internal detail;
//! stdout, stderr, and exit codes are intended to match the legacy Python
//! `hook_dispatcher.py` contract.
//!
//! Mode 2 prints a JSON diagnostic, no network, no envelope write.
//!
//! Mode 3 prints the ghook version and writes
//! `~/.gobby/bin/.ghook-compatibility` with `{schema_version, ghook_version}`.

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
mod terminal_context;
mod transport;

use cli_config::CliConfig;
use envelope::Envelope;

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

    /// Print version and write ~/.gobby/bin/.ghook-compatibility stamp.
    #[arg(long)]
    version: bool,

    /// Host CLI name (claude, codex, gemini, qwen).
    #[arg(long)]
    cli: Option<String>,

    /// Hook type (e.g. session-start, SessionStart, PreToolUse).
    #[arg(long = "type")]
    hook_type: Option<String>,

    /// Compatibility flag accepted for installer parity with legacy hook commands.
    #[arg(long)]
    critical: bool,

    /// Detach from the parent's session/process group before the POST.
    #[arg(long)]
    detach: bool,
}

fn main() -> ExitCode {
    let args = Args::parse();

    if args.version {
        return match write_compatibility_stamp() {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                // Still print the version; stamp-write failure is non-fatal.
                println!("ghook {}", diagnose::GHOOK_VERSION);
                eprintln!("note: could not write compatibility stamp: {e}");
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
        println!("{}", serde_json::json!({}));
        return ExitCode::SUCCESS;
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

    let mut input_data = match parsed {
        Ok(v) => v,
        Err(e) => {
            let _ = transport::quarantine_malformed(&stdin_raw, &e.to_string(), is_critical);
            println!("{}", serde_json::json!({}));
            return ExitCode::from(cfg.json_error_exit_code);
        }
    };

    // Terminal-context enrichment, gated by per-CLI set.
    if cfg.wants_terminal_context(hook_type) {
        terminal_context::inject(&mut input_data);
    }

    // Headers: omit on missing (never empty string).
    let mut headers: BTreeMap<String, String> = BTreeMap::new();
    if let Some(pid) = project_id {
        headers.insert("X-Gobby-Project-Id".into(), pid);
    }
    if let Some(sid) = input_data.get("session_id").and_then(|v| v.as_str())
        && !sid.is_empty()
    {
        headers.insert("X-Gobby-Session-Id".into(), sid.to_string());
    }

    let env = Envelope::new(
        is_critical,
        hook_type.to_string(),
        input_data,
        detect_source(&cfg),
        headers,
    );

    // Enqueue first (atomic write to ~/.gobby/hooks/inbox/).
    let inbox = match transport::inbox_dir() {
        Ok(d) => d,
        Err(e) => {
            return emit_action(action_from_failure(
                hook_type,
                &cfg,
                transport::DeliveryFailureKind::Other,
                &e.to_string(),
            ));
        }
    };
    let enqueued_path = match transport::enqueue_to(&env, &inbox) {
        Ok(p) => p,
        Err(e) => {
            return emit_action(action_from_failure(
                hook_type,
                &cfg,
                transport::DeliveryFailureKind::Other,
                &e.to_string(),
            ));
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

fn hooks_disabled_by_env() -> bool {
    std::env::var_os("GOBBY_HOOKS_DISABLED").is_some_and(|v| v == "1")
}

fn detect_source(cfg: &CliConfig) -> String {
    if cfg.source != "claude" {
        return cfg.source.to_string();
    }

    if let Some(source) = std::env::var_os("GOBBY_SOURCE")
        && !source.is_empty()
    {
        return source.to_string_lossy().into_owned();
    }
    if std::env::var("CLAUDE_CODE_ENTRYPOINT").ok().as_deref() == Some("sdk-py") {
        return "claude".to_string();
    }
    cfg.source.to_string()
}

fn emit_action(action: HookAction) -> ExitCode {
    if let Some(stdout_json) = action.stdout_json {
        println!("{stdout_json}");
    }
    if let Some(stderr_message) = action.stderr_message {
        eprintln!("\n{}", stderr_message.trim_end());
    }
    ExitCode::from(action.exit_code)
}

fn action_from_success_response(
    _canonical_source: &str,
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
        stdout_json: json_value_is_meaningful(&result).then_some(serialized),
        stderr_message: None,
    })
}

fn action_from_failure(
    hook_type: &str,
    cfg: &CliConfig,
    failure_kind: transport::DeliveryFailureKind,
    detail: &str,
) -> HookAction {
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

fn json_value_is_meaningful(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => {
            if let Some(i) = number.as_i64() {
                i != 0
            } else if let Some(u) = number.as_u64() {
                u != 0
            } else {
                number.as_f64().is_some_and(|f| f != 0.0)
            }
        }
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

fn write_compatibility_stamp() -> Result<()> {
    let home = dirs::home_dir().ok_or_else(|| anyhow::anyhow!("no home directory"))?;
    let bin_dir = home.join(".gobby").join("bin");
    std::fs::create_dir_all(&bin_dir)?;
    let stamp_path = bin_dir.join(".ghook-compatibility");
    let stamp = serde_json::json!({
        "schema_version": envelope::SCHEMA_VERSION,
        "ghook_version": diagnose::GHOOK_VERSION,
    });
    let bytes = serde_json::to_vec_pretty(&stamp)?;
    transport::atomic_write(&stamp_path, &bytes)?;
    println!("ghook {}", diagnose::GHOOK_VERSION);
    Ok(())
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
