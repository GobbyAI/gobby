//! Terminal/process context enrichment.
//!
//! Port of `hook_dispatcher.py:181-223` — captures the caller's PID, TTY,
//! tmux pane, `TERM_PROGRAM`, and `GOBBY_*` env vars so the daemon can
//! reconcile spawned-terminal agents on session-start.
//!
//! Sharp edge (dispatcher `:205`): `TMUX_PANE` is inherited by children
//! spawned into *other* terminals (e.g. Ghostty), so emitting it when
//! `TMUX` is not set would point `kill_agent` at the *parent's* pane. We
//! emit `terminal_context` only when `TMUX` is present and `TMUX_PANE`
//! matches the daemon's `^%\d+$` tmux-pane contract.

use serde_json::{Value, json};
use std::env;

/// Build a terminal-context object for injection under
/// `input_data.terminal_context`.
pub fn capture() -> Option<Value> {
    let tmux = env::var("TMUX").ok()?;
    let tmux_pane = env::var("TMUX_PANE").ok()?;
    build_context(&tmux, &tmux_pane)
}

fn build_context(tmux: &str, tmux_pane: &str) -> Option<Value> {
    if tmux.is_empty() || !is_valid_tmux_pane(tmux_pane) {
        return None;
    }

    let parent_pid = parent_pid_or_null();
    let tty = tty_name_or_null();
    let tmux_socket_path = parse_tmux_socket_path(tmux)
        .map(Value::String)
        .unwrap_or(Value::Null);
    let term_program = env_or_null("TERM_PROGRAM");

    Some(json!({
        "parent_pid": parent_pid,
        "tty": tty,
        "tmux_pane": tmux_pane,
        "tmux_socket_path": tmux_socket_path,
        "term_program": term_program,
        "gobby_session_id": env_or_null("GOBBY_SESSION_ID"),
        "gobby_parent_session_id": env_or_null("GOBBY_PARENT_SESSION_ID"),
        "gobby_agent_run_id": env_or_null("GOBBY_AGENT_RUN_ID"),
        "gobby_project_id": env_or_null("GOBBY_PROJECT_ID"),
        "gobby_workflow_name": env_or_null("GOBBY_WORKFLOW_NAME"),
        // Carried so the daemon's SESSION_START handler can recognize and
        // drop registrations from daemon-spawned ACP subprocesses.
        "gobby_acp_child": env_or_null("GOBBY_ACP_CHILD"),
    }))
}

/// Inject terminal context into `input_data` when:
/// (a) `input_data` is a JSON object, AND
/// (b) no `terminal_context` key is already present (mirror Python's
///     `setdefault` — dispatcher `:682`).
pub fn inject(input_data: &mut Value) {
    if let Some(obj) = input_data.as_object_mut()
        && !obj.contains_key("terminal_context")
        && let Some(ctx) = capture()
    {
        obj.insert("terminal_context".into(), ctx);
    }
}

fn env_or_null(key: &str) -> Value {
    match env::var(key) {
        Ok(v) => Value::String(v),
        Err(_) => Value::Null,
    }
}

fn parent_pid_or_null() -> Value {
    // getppid is infallible on all supported targets; no Windows port here,
    // but std::process::id lacks a parent-pid equivalent so we call libc.
    #[cfg(unix)]
    {
        // SAFETY: libc::getppid has no preconditions and cannot fail.
        let pid = unsafe { libc::getppid() };
        Value::from(pid as i64)
    }
    #[cfg(windows)]
    {
        // Windows lacks a direct parent-PID syscall without snapshotting —
        // dispatcher's `os.getppid()` is a Unix concept. Emit null rather
        // than fabricate a value; the daemon treats null as "unknown".
        Value::Null
    }
}

fn tty_name_or_null() -> Value {
    #[cfg(unix)]
    {
        // SAFETY: libc::ttyname is thread-hostile (returns a pointer into
        // a static buffer), but we're single-threaded here and copy the
        // bytes out before any other call could mutate the buffer.
        unsafe {
            let ptr = libc::ttyname(0);
            if ptr.is_null() {
                return Value::Null;
            }
            let cstr = std::ffi::CStr::from_ptr(ptr);
            match cstr.to_str() {
                Ok(s) => Value::String(s.to_owned()),
                Err(_) => Value::Null,
            }
        }
    }
    #[cfg(windows)]
    {
        Value::Null
    }
}

fn is_valid_tmux_pane(pane: &str) -> bool {
    let Some(rest) = pane.strip_prefix('%') else {
        return false;
    };
    !rest.is_empty() && rest.bytes().all(|b| b.is_ascii_digit())
}

/// Extract the socket path from the `TMUX` env var. Mirror of
/// `gobby.sessions.tmux_context.parse_tmux_socket_path` and the inline
/// copy at `hook_dispatcher.py:43-53`.
fn parse_tmux_socket_path(tmux_env: &str) -> Option<String> {
    let head = tmux_env.split(',').next()?.trim();
    if head.is_empty() {
        None
    } else {
        Some(head.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parse_socket_path_extracts_leading_segment() {
        assert_eq!(
            parse_tmux_socket_path("/private/tmp/tmux-501/default,12345,0"),
            Some("/private/tmp/tmux-501/default".into())
        );
    }

    #[test]
    fn parse_socket_path_handles_empty() {
        assert_eq!(parse_tmux_socket_path(""), None);
        assert_eq!(parse_tmux_socket_path(",12,0"), None);
    }

    #[test]
    fn build_context_sets_tmux_pane_verbatim() {
        let ctx = build_context("/private/tmp/tmux-501/default,12345,0", "%42").unwrap();
        assert_eq!(ctx["tmux_pane"], "%42");
        assert_eq!(ctx["tmux_socket_path"], "/private/tmp/tmux-501/default");
    }

    #[test]
    fn build_context_rejects_missing_empty_or_invalid_pane() {
        assert!(build_context("/tmp/tmux,1,0", "").is_none());
        assert!(build_context("/tmp/tmux,1,0", "42").is_none());
        assert!(build_context("/tmp/tmux,1,0", "%").is_none());
        assert!(build_context("/tmp/tmux,1,0", "%abc").is_none());
        assert!(build_context("", "%42").is_none());
    }

    #[test]
    fn valid_tmux_pane_matches_daemon_contract() {
        assert!(is_valid_tmux_pane("%1"));
        assert!(is_valid_tmux_pane("%001"));
        assert!(!is_valid_tmux_pane(""));
        assert!(!is_valid_tmux_pane("%"));
        assert!(!is_valid_tmux_pane(" %1"));
        assert!(!is_valid_tmux_pane("%1 "));
        assert!(!is_valid_tmux_pane("1"));
    }

    #[test]
    fn inject_respects_existing_terminal_context() {
        let mut data = json!({
            "session_id": "s1",
            "terminal_context": {"custom": "preserved"},
        });
        inject(&mut data);
        assert_eq!(data["terminal_context"]["custom"], "preserved");
        assert!(data["terminal_context"].get("parent_pid").is_none());
    }

    #[test]
    fn inject_no_op_on_non_object() {
        let mut data = json!("not an object");
        inject(&mut data);
        assert_eq!(data, json!("not an object"));
    }

    #[test]
    fn capture_emits_expected_keys() {
        let ctx = build_context("/tmp/tmux,1,0", "%9").unwrap();
        let obj = ctx.as_object().expect("object");
        for key in [
            "parent_pid",
            "tty",
            "tmux_pane",
            "tmux_socket_path",
            "term_program",
            "gobby_session_id",
            "gobby_parent_session_id",
            "gobby_agent_run_id",
            "gobby_project_id",
            "gobby_workflow_name",
            "gobby_acp_child",
        ] {
            assert!(obj.contains_key(key), "missing key: {key}");
        }
    }
}
