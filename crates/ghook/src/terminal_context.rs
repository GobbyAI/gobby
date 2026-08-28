//! Terminal/process context enrichment.
//!
//! Port of `hook_dispatcher.py:181-223` — captures the caller's PID, TTY,
//! tmux pane, `TERM_PROGRAM`, and `GOBBY_*` env vars so the daemon can
//! reconcile spawned-terminal agents across lifecycle hooks.
//!
//! Sharp edge (dispatcher `:205`): `TMUX_PANE` is inherited by children
//! spawned into *other* terminals (e.g. Ghostty), so emitting it when
//! `TMUX` is not set would point `kill_agent` at the *parent's* pane. We
//! always emit process context, but tmux fields are populated only when
//! `TMUX` is present and `TMUX_PANE` matches the daemon's `^%\d+$` contract.

use serde_json::{Value, json};
use std::env;
use std::io::Read;
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const TMUX_IDENTITY_TIMEOUT: Duration = Duration::from_millis(500);

#[derive(Debug, PartialEq)]
struct TmuxIdentity {
    window_id: String,
    session_name: String,
}

/// Build a terminal-context object for injection under
/// `input_data.terminal_context`.
pub fn capture() -> Value {
    let tmux = env::var("TMUX").ok();
    let tmux_pane = env::var("TMUX_PANE").ok();
    let identity = tmux
        .as_deref()
        .filter(|value| !value.is_empty())
        .zip(tmux_pane.as_deref().filter(|pane| is_valid_tmux_pane(pane)))
        .and_then(|(tmux_env, pane)| {
            parse_tmux_socket_path(tmux_env)
                .and_then(|socket_path| query_tmux_identity(&socket_path, pane))
        });
    build_context(tmux.as_deref(), tmux_pane.as_deref(), identity.as_ref())
}

pub fn enabled_for_hook(hook_type: &str) -> bool {
    matches!(
        hook_type
            .chars()
            .filter(|ch| !matches!(ch, '-' | '_'))
            .flat_map(char::to_lowercase)
            .collect::<String>()
            .as_str(),
        "sessionstart"
            | "userpromptsubmit"
            | "beforeagent"
            | "preinvocation"
            | "sessionend"
            | "stop"
            | "afteragent"
            | "postinvocation"
            | "subagentstart"
            | "subagentstop"
            | "subagentend"
    )
}

fn build_context(
    tmux: Option<&str>,
    tmux_pane: Option<&str>,
    identity: Option<&TmuxIdentity>,
) -> Value {
    let parent_pid = parent_pid_or_null();
    let tty = tty_name_or_null();
    let valid_tmux = tmux
        .filter(|value| !value.is_empty())
        .zip(tmux_pane.filter(|pane| is_valid_tmux_pane(pane)));
    let tmux_pane = valid_tmux
        .as_ref()
        .map(|(_, pane)| Value::String((*pane).to_string()))
        .unwrap_or(Value::Null);
    let tmux_socket_path = valid_tmux
        .and_then(|(tmux, _)| parse_tmux_socket_path(tmux))
        .map(Value::String)
        .unwrap_or(Value::Null);
    let tmux_window_id = identity
        .map(|value| Value::String(value.window_id.clone()))
        .unwrap_or(Value::Null);
    let tmux_session = identity
        .map(|value| Value::String(value.session_name.clone()))
        .unwrap_or(Value::Null);
    let term_program = env_or_null("TERM_PROGRAM");

    json!({
        "parent_pid": parent_pid,
        "tty": tty,
        "tmux_pane": tmux_pane,
        "tmux_socket_path": tmux_socket_path,
        "tmux_window_id": tmux_window_id,
        "tmux_session": tmux_session,
        "term_program": term_program,
        "gobby_session_id": env_or_null("GOBBY_SESSION_ID"),
        "gobby_parent_session_id": env_or_null("GOBBY_PARENT_SESSION_ID"),
        "gobby_agent_run_id": env_or_null("GOBBY_AGENT_RUN_ID"),
        "gobby_project_id": env_or_null("GOBBY_PROJECT_ID"),
        "gobby_workflow_name": env_or_null("GOBBY_WORKFLOW_NAME"),
        // Carried so the daemon's SESSION_START handler can recognize and
        // drop registrations from daemon-spawned ACP subprocesses.
        "gobby_acp_child": env_or_null("GOBBY_ACP_CHILD"),
    })
}

/// Inject terminal context into an object-shaped `input_data`.
///
/// Existing provider context is preserved. Captured fields fill gaps, while
/// `gobby_agent_run_id` always comes from ghook's trusted process environment.
pub fn inject(input_data: &mut Value) {
    let Some(obj) = input_data.as_object_mut() else {
        return;
    };

    match obj.get_mut("terminal_context") {
        Some(Value::Object(existing)) => {
            let Value::Object(captured) = capture() else {
                return;
            };
            for (key, value) in captured {
                if key == "gobby_agent_run_id" {
                    existing.insert(key, value);
                } else {
                    existing.entry(key).or_insert(value);
                }
            }
        }
        Some(_) => {}
        None => {
            obj.insert("terminal_context".into(), capture());
        }
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

fn parse_tmux_identity(output: &str) -> Option<TmuxIdentity> {
    let (window_id, session_name) = output.trim().split_once('\t')?;
    let window_number = window_id.strip_prefix('@')?;
    if window_number.is_empty()
        || !window_number
            .chars()
            .all(|character| character.is_ascii_digit())
        || session_name.is_empty()
    {
        return None;
    }
    Some(TmuxIdentity {
        window_id: window_id.to_string(),
        session_name: session_name.to_string(),
    })
}

fn query_tmux_identity(socket_path: &str, pane: &str) -> Option<TmuxIdentity> {
    let mut child = Command::new("tmux")
        .args([
            "-S",
            socket_path,
            "display-message",
            "-p",
            "-t",
            pane,
            "#{window_id}\t#{session_name}",
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let mut stdout = child.stdout.take()?;
    let deadline = Instant::now() + TMUX_IDENTITY_TIMEOUT;

    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                if !status.success() {
                    return None;
                }
                let mut output = String::new();
                stdout.read_to_string(&mut output).ok()?;
                return parse_tmux_identity(&output);
            }
            Ok(None) if Instant::now() < deadline => {
                thread::sleep(Duration::from_millis(10));
            }
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
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
        let identity = TmuxIdentity {
            window_id: "@17".to_string(),
            session_name: "work".to_string(),
        };
        let ctx = build_context(
            Some("/private/tmp/tmux-501/default,12345,0"),
            Some("%42"),
            Some(&identity),
        );
        assert_eq!(ctx["tmux_pane"], "%42");
        assert_eq!(ctx["tmux_socket_path"], "/private/tmp/tmux-501/default");
        assert_eq!(ctx["tmux_window_id"], "@17");
        assert_eq!(ctx["tmux_session"], "work");
    }

    #[test]
    fn build_context_nulls_missing_empty_or_invalid_tmux_fields() {
        for (tmux, pane) in [
            (Some("/tmp/tmux,1,0"), Some("")),
            (Some("/tmp/tmux,1,0"), Some("42")),
            (Some("/tmp/tmux,1,0"), Some("%")),
            (Some("/tmp/tmux,1,0"), Some("%abc")),
            (Some(""), Some("%42")),
            (None, Some("%42")),
        ] {
            let ctx = build_context(tmux, pane, None);
            assert_eq!(ctx["tmux_pane"], Value::Null);
            assert_eq!(ctx["tmux_socket_path"], Value::Null);
            assert_eq!(ctx["tmux_window_id"], Value::Null);
            assert_eq!(ctx["tmux_session"], Value::Null);
        }
    }

    #[test]
    fn parse_tmux_identity_requires_window_and_session() {
        assert_eq!(
            parse_tmux_identity("@290\twork\n"),
            Some(TmuxIdentity {
                window_id: "@290".to_string(),
                session_name: "work".to_string(),
            })
        );
        assert_eq!(parse_tmux_identity("290\twork"), None);
        assert_eq!(parse_tmux_identity("@invalid\twork"), None);
        assert_eq!(parse_tmux_identity("@290\t"), None);
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
    fn context_bearing_hook_aliases_enable_terminal_context() {
        for hook_type in [
            "SessionStart",
            "session-start",
            "session_start",
            "UserPromptSubmit",
            "user-prompt-submit",
            "user_prompt_submit",
            "BeforeAgent",
            "before-agent",
            "before_agent",
            "PreInvocation",
            "pre-invocation",
            "pre_invocation",
            "SessionEnd",
            "session-end",
            "session_end",
            "Stop",
            "stop",
            "AfterAgent",
            "after-agent",
            "after_agent",
            "PostInvocation",
            "post-invocation",
            "post_invocation",
            "SubagentStart",
            "subagent-start",
            "subagent_start",
            "SubagentStop",
            "subagent-stop",
            "subagent_stop",
            "SubagentEnd",
            "subagent-end",
            "subagent_end",
        ] {
            assert!(enabled_for_hook(hook_type), "{hook_type}");
        }
    }

    #[test]
    fn tool_hooks_do_not_enable_terminal_context() {
        for hook_type in [
            "PreToolUse",
            "PostToolUse",
            "BeforeTool",
            "AfterTool",
            "PermissionRequest",
        ] {
            assert!(!enabled_for_hook(hook_type), "{hook_type}");
        }
    }

    #[test]
    fn inject_preserves_existing_provider_terminal_context() {
        let mut data = json!({
            "session_id": "s1",
            "terminal_context": {"custom": "preserved"},
        });
        inject(&mut data);
        assert_eq!(data["terminal_context"]["custom"], "preserved");
        assert!(data["terminal_context"].get("parent_pid").is_some());
        assert!(data["terminal_context"].get("gobby_agent_run_id").is_some());
    }

    #[test]
    fn inject_no_op_on_non_object() {
        let mut data = json!("not an object");
        inject(&mut data);
        assert_eq!(data, json!("not an object"));
    }

    #[test]
    fn capture_emits_expected_keys() {
        let ctx = build_context(Some("/tmp/tmux,1,0"), Some("%9"), None);
        let obj = ctx.as_object().expect("object");
        for key in [
            "parent_pid",
            "tty",
            "tmux_pane",
            "tmux_socket_path",
            "tmux_window_id",
            "tmux_session",
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
