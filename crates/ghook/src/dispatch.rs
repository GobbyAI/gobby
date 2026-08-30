use crate::action::{
    DeliveryReceiptAck, HookAction, action_from_failure, action_from_success_response,
    continue_action, emit_action, emit_empty_json, extract_delivery_receipt,
};
use crate::args::Args;
use crate::cli_config::CliConfig;
use crate::envelope::Envelope;
use crate::source::detect_source;
use crate::{
    detach, diagnostics, output, planned_shutdown, statusline, terminal_context, transport,
};
use serde_json::Value;
use std::collections::BTreeMap;
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

fn emit_exit(action: HookAction) -> ExitCode {
    emit_action(action).exit_code
}

pub(crate) fn run_gobby_owned(args: &Args) -> ExitCode {
    let (Some(cli), Some(hook_type)) = (args.cli.as_deref(), args.hook_type.as_deref()) else {
        emit_empty_json();
        return ExitCode::from(2);
    };

    let Some(cfg) = CliConfig::for_cli(cli) else {
        emit_empty_json();
        return ExitCode::from(2);
    };

    // Daemon-spawned ACP subprocesses (for example qwen --acp) set
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

    let is_critical = cfg.is_critical_hook(hook_type);

    // IMPORTANT: walk up for project context BEFORE any detach.
    // Sandbox FS-read denials or a detached process's cwd semantics on
    // macOS would otherwise surprise us (plan :76).
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let mut project_root = gobby_core::project::find_project_root(&cwd);
    let managed_by_environment = has_managed_environment();

    // Read stdin before detach too — detach closes the controlling TTY but
    // stdin pipes from the host CLI should still be intact; read now to
    // avoid late-read surprises if the host closes the pipe on exit.
    let mut stdin_raw = Vec::with_capacity(4096);
    let read_ok = std::io::stdin().read_to_end(&mut stdin_raw).is_ok();

    if statusline::is_statusline_hook(cli, hook_type) {
        return statusline::handle(&stdin_raw);
    }

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
            if project_root.is_none() && !managed_by_environment {
                return emit_exit(continue_action(cfg.source, hook_type));
            }
            let _ = transport::quarantine_malformed(&stdin_raw, &e.to_string(), is_critical);
            emit_empty_json();
            return ExitCode::from(cfg.malformed_input_exit_code(hook_type));
        }
    };

    if project_root.is_none() {
        project_root = project_root_from_workspace_paths(&input_data);
    }

    let context = managed_context(project_root.as_deref(), &input_data);
    if !context.managed {
        return emit_exit(continue_action(cfg.source, hook_type));
    }

    if planned_shutdown::should_skip_dispatch(hook_type) {
        return emit_exit(continue_action(cfg.source, hook_type));
    }

    let env = build_dispatch_envelope(&cfg, hook_type, input_data, context.project_id.as_deref());

    let direct_post_after_enqueue_failure =
        |failure_detail: String| -> Result<HookAction, ExitCode> {
            if args.enqueue_only {
                let enqueue_error = format!("enqueue failed: {failure_detail}");
                return Ok(action_from_failure(
                    hook_type,
                    &cfg,
                    transport::DeliveryFailureKind::Other,
                    &enqueue_error,
                ));
            }

            // Mirror the normal enqueue→detach→POST ordering: a --detach run must
            // escape the host's process group before the bounded fallback POST,
            // or a host group-kill can reap us mid-delivery with no action emitted.
            if args.detach {
                detach::detach();
            }
            let daemon_url = gobby_core::daemon_url::daemon_url();
            // No inbox file exists on this direct-POST fallback.
            let missing_enqueued_path = PathBuf::new();
            let report = transport::post_and_cleanup(&env, &missing_enqueued_path, &daemon_url);
            match report.outcome {
                transport::DeliveryOutcome::Delivered => {
                    match delivered_action(&cfg, hook_type, &env, &report, None, &daemon_url) {
                        Ok((action, _)) => Ok(action),
                        Err(exit_code) => Err(exit_code),
                    }
                }
                transport::DeliveryOutcome::Enqueued => {
                    let direct_detail = report
                        .response_body
                        .as_deref()
                        .or(report.transport_error.as_deref())
                        .unwrap_or_default();
                    let diagnostic_error = format!(
                        "enqueue failed: {failure_detail}; direct post failed: {direct_detail}"
                    );
                    record_delivery_failure(
                        &env,
                        &report,
                        None,
                        &daemon_url,
                        diagnostics::FailureKind::DirectPostAfterEnqueueFailure,
                        Some(&diagnostic_error),
                    );
                    Ok(action_from_failure(
                        hook_type,
                        &cfg,
                        transport::DeliveryFailureKind::Other,
                        &failure_detail,
                    ))
                }
            }
        };

    // Enqueue first (atomic write to ~/.gobby/hooks/inbox/).
    let inbox = match transport::inbox_dir() {
        Ok(d) => d,
        Err(e) => {
            return match direct_post_after_enqueue_failure(e.to_string()) {
                Ok(action) => emit_exit(action),
                Err(exit_code) => exit_code,
            };
        }
    };
    let enqueued_path = match transport::enqueue_to(&env, &inbox) {
        Ok(p) => p,
        Err(e) => {
            return match direct_post_after_enqueue_failure(e.to_string()) {
                Ok(action) => emit_exit(action),
                Err(exit_code) => exit_code,
            };
        }
    };

    if args.enqueue_only {
        return emit_exit(continue_action(cfg.source, hook_type));
    }

    // Detach *after* project walk-up and enqueue — the file on disk is
    // now the source of truth even if we die mid-POST.
    if args.detach {
        detach::detach();
    }

    // Best-effort POST. A delivered envelope stays durable until its mapped
    // provider action has been written and stdout has flushed.
    let daemon_url = gobby_core::daemon_url::daemon_url();
    let report = transport::post_and_cleanup(&env, &enqueued_path, &daemon_url);
    let mut receipt = None;
    let action = match report.outcome {
        transport::DeliveryOutcome::Delivered => {
            match delivered_action(
                &cfg,
                hook_type,
                &env,
                &report,
                Some(&enqueued_path),
                &daemon_url,
            ) {
                Ok((action, ack)) => {
                    receipt = ack;
                    action
                }
                Err(exit_code) => return exit_code,
            }
        }
        transport::DeliveryOutcome::Enqueued => {
            // Retryable ingress backpressure (503 + {"status":"retry"}) is not
            // a failure: the envelope stays enqueued for drain replay and the
            // host CLI continues, even on critical hooks — blocking here would
            // live-lock the CLI against a daemon that keeps asking for retry.
            if report.is_retry_backpressure() {
                return emit_exit(continue_action(cfg.source, hook_type));
            }

            if report.is_adapter_timeout()
                && (cfg.source == "agy" || !cfg.is_critical_hook(hook_type))
            {
                return emit_exit(continue_action(cfg.source, hook_type));
            }

            if planned_shutdown::suppress_after_failed_post(
                hook_type,
                report.failure_kind,
                &enqueued_path,
            ) {
                return emit_exit(continue_action(cfg.source, hook_type));
            }

            let failure_kind = report
                .failure_kind
                .map(diagnostics::FailureKind::from)
                .unwrap_or(diagnostics::FailureKind::Other);
            record_delivery_failure(
                &env,
                &report,
                Some(&enqueued_path),
                &daemon_url,
                failure_kind,
                report.transport_error.as_deref(),
            );

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

    let emitted = emit_action(action);
    if matches!(report.outcome, transport::DeliveryOutcome::Delivered) {
        settle_delivered_inbox(&enqueued_path, emitted.stdout_succeeded, receipt.as_ref());
    }
    emitted.exit_code
}

fn settle_delivered_inbox(
    enqueued_path: &Path,
    stdout_succeeded: bool,
    receipt: Option<&DeliveryReceiptAck>,
) {
    if !stdout_succeeded || enqueued_path.as_os_str().is_empty() {
        return;
    }
    if let Some(receipt) = receipt {
        let ack = crate::envelope::DeliveryReceipt::new(
            receipt.receipt_id.clone(),
            receipt.original_envelope_id.clone(),
            receipt.delivery_generation,
        );
        if let Ok(bytes) = serde_json::to_vec_pretty(&ack)
            && transport::atomic_write(enqueued_path, &bytes).is_ok()
        {
            return;
        }
        // Ack-write failure leaves the original envelope for replay.
        return;
    }
    let _ = fs::remove_file(enqueued_path);
}

fn hooks_disabled_by_env() -> bool {
    std::env::var_os("GOBBY_HOOKS_DISABLED").is_some_and(|v| v == "1")
}

#[derive(Debug, PartialEq, Eq)]
struct ManagedContext {
    managed: bool,
    project_id: Option<String>,
}

fn has_managed_environment() -> bool {
    ["GOBBY_PROJECT_ID", "GOBBY_SESSION_ID", "GOBBY_AGENT_RUN_ID"]
        .into_iter()
        .any(|name| env_nonempty(name).is_some())
}

fn env_nonempty(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|value| !value.is_empty())
}

fn payload_project_id(input_data: &Value) -> Option<String> {
    input_data
        .get("project_id")
        .and_then(Value::as_str)
        .filter(|project_id| !project_id.is_empty())
        .map(str::to_owned)
}

/// AGY sets hook cwd to the directory containing `hooks.json` (often
/// `~/.gemini/config`). The workspace is in camelCase `workspacePaths`.
fn project_root_from_workspace_paths(input_data: &Value) -> Option<PathBuf> {
    let paths = input_data
        .get("workspacePaths")
        .or_else(|| input_data.get("workspace_paths"))
        .and_then(Value::as_array)?;
    for value in paths {
        let Some(raw) = value.as_str().filter(|path| !path.is_empty()) else {
            continue;
        };
        if let Some(root) = gobby_core::project::find_project_root(Path::new(raw)) {
            return Some(root);
        }
    }
    None
}

fn managed_context(project_root: Option<&Path>, input_data: &Value) -> ManagedContext {
    let environment_project_id = env_nonempty("GOBBY_PROJECT_ID");
    let filesystem_project_id =
        project_root.and_then(|root| gobby_core::project::read_project_id(root).ok());
    let payload_project_id = payload_project_id(input_data);

    ManagedContext {
        managed: project_root.is_some()
            || has_managed_environment()
            || payload_project_id.is_some(),
        project_id: environment_project_id
            .or(filesystem_project_id)
            .or(payload_project_id),
    }
}

fn build_dispatch_envelope(
    cfg: &CliConfig,
    hook_type: &str,
    mut input_data: Value,
    project_id: Option<&str>,
) -> Envelope {
    inject_machine_identity(&mut input_data);

    if terminal_context::enabled_for_hook(hook_type) {
        terminal_context::inject(&mut input_data);
    }

    // Headers: omit on missing (never empty string).
    let mut headers: BTreeMap<String, String> = BTreeMap::new();
    if let Some(pid) = project_id {
        headers.insert("X-Gobby-Project-Id".into(), pid.to_string());
    }
    let session_id = std::env::var("GOBBY_SESSION_ID")
        .ok()
        .filter(|sid| !sid.is_empty())
        .or_else(|| {
            input_data
                .get("session_id")
                .and_then(|value| value.as_str())
                .filter(|sid| !sid.is_empty())
                .map(str::to_owned)
        });
    if let Some(sid) = session_id {
        headers.insert("X-Gobby-Session-Id".into(), sid);
    }
    // Managed runs authenticate with a run-bound capability; the daemon
    // requires the matching run id on this context-bearing route.
    if let Ok(rid) = std::env::var("GOBBY_AGENT_RUN_ID")
        && !rid.is_empty()
    {
        headers.insert("X-Gobby-Agent-Run-Id".into(), rid);
    }

    Envelope::new(
        cfg.is_critical_hook(hook_type),
        hook_type.to_string(),
        input_data,
        detect_source(cfg),
        headers,
    )
}

fn inject_machine_identity(input_data: &mut Value) {
    let Some(obj) = input_data.as_object_mut() else {
        return;
    };

    match gobby_core::machine::read_local_machine_id() {
        Ok(machine_id) => {
            obj.insert("machine_id".into(), Value::String(machine_id));
            obj.insert(
                "os".into(),
                Value::String(gobby_core::machine::local_os_name().to_string()),
            );
            obj.remove("machine_id_error");
        }
        Err(error) => {
            obj.remove("machine_id");
            obj.remove("os");
            obj.insert(
                "machine_id_error".into(),
                Value::String(error.code().to_string()),
            );
        }
    }
}

fn delivered_action(
    cfg: &CliConfig,
    hook_type: &str,
    envelope: &Envelope,
    report: &transport::DeliveryReport,
    enqueued_path: Option<&Path>,
    daemon_url: &str,
) -> Result<(HookAction, Option<DeliveryReceiptAck>), ExitCode> {
    let body = report.response_body.as_deref().unwrap_or_default();
    let (stripped, receipt) = extract_delivery_receipt(body);
    match action_from_success_response(cfg.source, hook_type, &stripped) {
        Ok(action) => Ok((action, receipt)),
        Err(error) => {
            let _ = record_delivery_failure(
                envelope,
                report,
                enqueued_path,
                daemon_url,
                success_failure_kind(body),
                Some(&error),
            );
            output::stderr(format_args!(
                "ghook: daemon 2xx response could not be mapped for hook '{hook_type}': {error}\n"
            ));
            Err(ExitCode::from(1))
        }
    }
}

fn record_delivery_failure(
    envelope: &Envelope,
    report: &transport::DeliveryReport,
    enqueued_path: Option<&Path>,
    daemon_url: &str,
    failure_kind: diagnostics::FailureKind,
    error: Option<&str>,
) -> bool {
    let envelope_id = enqueued_path.and_then(transport::envelope_id_from_path);
    diagnostics::record_failure(diagnostics::FailureContext {
        envelope,
        envelope_id,
        failure_kind,
        status_code: report.status_code,
        error,
        response_body: report.response_body.as_deref(),
        transport_error: report.transport_error.as_deref(),
        daemon_url,
    })
    .is_ok()
}

fn success_failure_kind(response_body: &str) -> diagnostics::FailureKind {
    let trimmed = response_body.trim();
    if !trimmed.is_empty() && serde_json::from_str::<Value>(trimmed).is_err() {
        diagnostics::FailureKind::InvalidSuccessJson
    } else {
        diagnostics::FailureKind::SuccessResponseMapping
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::ffi::OsStr;
    use std::sync::Mutex;

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn with_tmux_env<T>(tmux: Option<&str>, tmux_pane: Option<&str>, f: impl FnOnce() -> T) -> T {
        let _guard = ENV_LOCK.lock().unwrap();
        let gobby_home = tempfile::tempdir().unwrap();
        let vars: [(&str, Option<&OsStr>); 3] = [
            ("TMUX", tmux.map(OsStr::new)),
            ("TMUX_PANE", tmux_pane.map(OsStr::new)),
            ("GOBBY_HOME", Some(gobby_home.path().as_os_str())),
        ];
        temp_env::with_vars(vars, f)
    }

    fn with_gobby_home<T>(gobby_home: &Path, f: impl FnOnce() -> T) -> T {
        let _guard = ENV_LOCK.lock().unwrap();
        temp_env::with_var("GOBBY_HOME", Some(gobby_home), f)
    }

    #[test]
    fn dispatch_envelope_injects_local_machine_identity() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("machine_id"), "machine-client\n").unwrap();

        with_gobby_home(dir.path(), || {
            let cfg = CliConfig::for_cli("codex").expect("supported CLI");
            let envelope =
                build_dispatch_envelope(&cfg, "PreToolUse", json!({"machine_id": "stale"}), None);

            assert_eq!(envelope.input_data["machine_id"], "machine-client");
            assert_eq!(
                envelope.input_data["os"],
                gobby_core::machine::local_os_name()
            );
            assert!(envelope.input_data.get("machine_id_error").is_none());
        });
    }

    #[test]
    fn dispatch_envelope_uses_managed_session_for_header_and_preserves_payload_session() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("machine_id"), "machine-client\n").unwrap();

        with_gobby_home(dir.path(), || {
            temp_env::with_var("GOBBY_SESSION_ID", Some("gobby-session"), || {
                let cfg = CliConfig::for_cli("codex").expect("supported CLI");
                let envelope = build_dispatch_envelope(
                    &cfg,
                    "SessionStart",
                    json!({"session_id": "native-session"}),
                    None,
                );

                assert_eq!(
                    envelope.headers.get("X-Gobby-Session-Id"),
                    Some(&"gobby-session".to_string())
                );
                assert_eq!(envelope.input_data["session_id"], "native-session");
            });
        });
    }

    #[test]
    fn dispatch_envelope_uses_payload_session_without_managed_session() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("machine_id"), "machine-client\n").unwrap();

        with_gobby_home(dir.path(), || {
            temp_env::with_var("GOBBY_SESSION_ID", None::<&str>, || {
                let cfg = CliConfig::for_cli("codex").expect("supported CLI");
                let envelope = build_dispatch_envelope(
                    &cfg,
                    "SessionStart",
                    json!({"session_id": "native-session"}),
                    None,
                );

                assert_eq!(
                    envelope.headers.get("X-Gobby-Session-Id"),
                    Some(&"native-session".to_string())
                );
                assert_eq!(envelope.input_data["session_id"], "native-session");
            });
        });
    }

    #[test]
    fn dispatch_envelope_reports_missing_machine_id_file() {
        let dir = tempfile::tempdir().unwrap();

        with_gobby_home(dir.path(), || {
            let cfg = CliConfig::for_cli("codex").expect("supported CLI");
            let envelope = build_dispatch_envelope(
                &cfg,
                "PreToolUse",
                json!({"machine_id": "stale", "os": "stale-os"}),
                None,
            );

            assert!(envelope.input_data.get("machine_id").is_none());
            assert!(envelope.input_data.get("os").is_none());
            assert_eq!(
                envelope.input_data["machine_id_error"],
                "machine_id_missing"
            );
        });
    }

    #[test]
    fn dispatch_envelope_reports_empty_machine_id_file() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("machine_id"), " \n").unwrap();

        with_gobby_home(dir.path(), || {
            let cfg = CliConfig::for_cli("codex").expect("supported CLI");
            let envelope =
                build_dispatch_envelope(&cfg, "PreToolUse", json!({"session_id": "sess-1"}), None);

            assert!(envelope.input_data.get("machine_id").is_none());
            assert_eq!(envelope.input_data["machine_id_error"], "machine_id_empty");
        });
    }

    #[test]
    fn dispatch_envelope_injects_valid_tmux_pane_for_context_bearing_hooks() {
        with_tmux_env(Some("/tmp/tmux-501/default,12345,0"), Some("%17"), || {
            let cfg = CliConfig::for_cli("grok").expect("supported CLI");
            for hook_type in [
                "SessionStart",
                "UserPromptSubmit",
                "BeforeAgent",
                "PreInvocation",
            ] {
                let envelope =
                    build_dispatch_envelope(&cfg, hook_type, json!({"session_id": "sess-1"}), None);

                assert_eq!(
                    envelope.input_data["terminal_context"]["tmux_pane"], "%17",
                    "{hook_type}"
                );
            }
        });
    }

    #[test]
    fn dispatch_envelope_omits_terminal_context_for_tool_hooks() {
        with_tmux_env(Some("/tmp/tmux-501/default,12345,0"), Some("%17"), || {
            let cfg = CliConfig::for_cli("codex").expect("supported CLI");
            let envelope =
                build_dispatch_envelope(&cfg, "PreToolUse", json!({"session_id": "sess-1"}), None);

            assert!(envelope.input_data.get("terminal_context").is_none());
        });
    }

    #[test]
    fn dispatch_envelope_nulls_tmux_fields_for_missing_or_invalid_tmux_pane() {
        for pane in [None, Some(""), Some("17"), Some("%"), Some("%x")] {
            with_tmux_env(Some("/tmp/tmux-501/default,12345,0"), pane, || {
                let cfg = CliConfig::for_cli("qwen").expect("supported CLI");
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
            let cfg = CliConfig::for_cli("qwen").expect("supported CLI");
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

    #[test]
    fn settle_delivered_inbox_replaces_original_with_delivery_receipt() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("n-0000000000001-abcd.json");
        std::fs::write(&path, r#"{"hook_type":"PreInvocation"}"#).unwrap();
        let receipt = DeliveryReceiptAck {
            receipt_id: "r1".into(),
            original_envelope_id: "n-0000000000001-abcd".into(),
            delivery_generation: 1,
        };

        settle_delivered_inbox(&path, true, Some(&receipt));

        let written: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(written["kind"], "delivery-receipt");
        assert_eq!(written["receipt_id"], "r1");
        assert_eq!(written["original_envelope_id"], "n-0000000000001-abcd");
        assert_eq!(written["delivery_generation"], 1);
        assert_eq!(written["schema_version"], 1);
    }

    #[test]
    fn settle_delivered_inbox_deletes_original_when_no_receipt() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("n-0000000000001-abcd.json");
        std::fs::write(&path, r#"{"hook_type":"PreInvocation"}"#).unwrap();

        settle_delivered_inbox(&path, true, None);

        assert!(!path.exists());
    }

    #[test]
    fn settle_delivered_inbox_keeps_original_when_stdout_fails() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("n-0000000000001-abcd.json");
        std::fs::write(&path, r#"{"hook_type":"PreInvocation"}"#).unwrap();
        let receipt = DeliveryReceiptAck {
            receipt_id: "r1".into(),
            original_envelope_id: "n-0000000000001-abcd".into(),
            delivery_generation: 1,
        };

        settle_delivered_inbox(&path, false, Some(&receipt));

        let written = std::fs::read_to_string(&path).unwrap();
        assert_eq!(written, r#"{"hook_type":"PreInvocation"}"#);
    }
}
