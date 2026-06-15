use crate::action::{
    HookAction, action_from_failure, action_from_success_response, continue_action, emit_action,
    emit_empty_json,
};
use crate::args::Args;
use crate::cli_config::CliConfig;
use crate::envelope::Envelope;
use crate::source::detect_source;
use crate::{detach, planned_shutdown, statusline, terminal_context, transport};
use serde_json::Value;
use std::collections::BTreeMap;
use std::io::Read;
use std::path::PathBuf;
use std::process::ExitCode;

pub(crate) fn run_gobby_owned(args: &Args) -> ExitCode {
    let (Some(cli), Some(hook_type)) = (args.cli.as_deref(), args.hook_type.as_deref()) else {
        emit_empty_json();
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
        emit_empty_json();
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
            emit_empty_json();
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

#[cfg(test)]
mod tests {
    use super::*;
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
}
