//! Terminal/process context enrichment.
//!
//! Captures the caller's PID, TTY, `TERM_PROGRAM`, and `GOBBY_*` environment
//! variables so the daemon can reconcile native terminal agents across hooks.

use serde_json::{Value, json};
use std::env;

/// Build a terminal-context object for injection under
/// `input_data.terminal_context`.
pub fn capture() -> Value {
    build_context()
}

pub fn enabled_for_hook(hook_type: &str) -> bool {
    sender_context_enabled_for_hook(hook_type)
        || matches!(
            normalized_hook_type(hook_type).as_str(),
            "sessionstart"
                | "userpromptsubmit"
                | "beforeagent"
                | "preinvocation"
                | "sessionend"
                | "stop"
                | "stopcancelled"
                | "afteragent"
                | "postinvocation"
                | "subagentstart"
                | "subagentstop"
                | "subagentend"
        )
}

pub(crate) fn normalized_hook_type(hook_type: &str) -> String {
    hook_type
        .chars()
        .filter(|ch| !matches!(ch, '-' | '_'))
        .flat_map(char::to_lowercase)
        .collect()
}

fn sender_context_enabled_for_hook(hook_type: &str) -> bool {
    matches!(
        normalized_hook_type(hook_type).as_str(),
        "pretooluse"
            | "posttooluse"
            | "posttoolusefailure"
            | "posttoolbatch"
            | "beforetool"
            | "aftertool"
            | "aftertoolfailure"
            | "permissionrequest"
    )
}

pub fn capture_for_hook(hook_type: &str) -> Value {
    if sender_context_enabled_for_hook(hook_type) {
        // Tool hooks need the sender that can refresh a stale recorded CLI PID.
        // Tool events need only process and run identity.
        json!({
            "parent_pid": parent_pid_or_null(),
            "gobby_agent_run_id": env_or_null("GOBBY_AGENT_RUN_ID"),
        })
    } else {
        capture()
    }
}

fn build_context() -> Value {
    let parent_pid = parent_pid_or_null();
    let tty = tty_name_or_null();
    let term_program = env_or_null("TERM_PROGRAM");

    json!({
        "parent_pid": parent_pid,
        "tty": tty,
        "term_program": term_program,
        "gobby_session_id": env_or_null("GOBBY_SESSION_ID"),
        "gobby_parent_session_id": env_or_null("GOBBY_PARENT_SESSION_ID"),
        "gobby_agent_run_id": env_or_null("GOBBY_AGENT_RUN_ID"),
        "gobby_project_id": env_or_null("GOBBY_PROJECT_ID"),
        "gobby_workflow_name": env_or_null("GOBBY_WORKFLOW_NAME"),
        // Carried so the daemon's SESSION_START handler can recognize and
        // drop registrations from daemon-spawned ACP subprocesses.
        "gobby_acp_child": env_or_null("GOBBY_ACP_CHILD"),
        // Exported into every native pane so SESSION_START can bind the CLI
        // session to the terminal row it runs in.
        "gobby_terminal_id": env_or_null("GOBBY_TERMINAL_ID"),
        "gobby_pane_ref": env_or_null("GOBBY_PANE_REF"),
    })
}

/// Inject terminal context into an object-shaped `input_data`.
///
/// Existing provider context is preserved. Captured fields fill gaps, while
/// `gobby_agent_run_id` always comes from ghook's trusted process environment.
pub fn inject(input_data: &mut Value, hook_type: &str) {
    let Some(obj) = input_data.as_object_mut() else {
        return;
    };

    match obj.get_mut("terminal_context") {
        Some(Value::Object(existing)) => {
            let Value::Object(captured) = capture_for_hook(hook_type) else {
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
            obj.insert("terminal_context".into(), capture_for_hook(hook_type));
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

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

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
            "StopCancelled",
            "stop-cancelled",
            "stop_cancelled",
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
    fn tool_hooks_enable_sender_context() {
        for hook_type in [
            "PreToolUse",
            "PostToolUse",
            "BeforeTool",
            "AfterTool",
            "pre-tool-use",
            "post_tool_use_failure",
            "PostToolBatch",
            "after-tool-failure",
            "PermissionRequest",
        ] {
            assert!(enabled_for_hook(hook_type), "{hook_type}");
            let context = capture_for_hook(hook_type);
            assert_eq!(context.as_object().unwrap().len(), 2, "{hook_type}");
            assert!(context["parent_pid"].as_i64().unwrap() > 0, "{hook_type}");
        }
    }

    #[test]
    fn tool_sender_capture_cost() {
        assert!(
            capture_for_hook("pre-tool-use")["parent_pid"]
                .as_i64()
                .unwrap()
                > 0
        );
        let started = std::time::Instant::now();
        for _ in 0..10_000 {
            std::hint::black_box(capture_for_hook("pre-tool-use"));
        }
        eprintln!(
            "10000 sender captures: {:?}, mean {:?}",
            started.elapsed(),
            started.elapsed() / 10_000
        );
    }

    #[test]
    fn inject_preserves_existing_provider_terminal_context() {
        let mut data = json!({
            "session_id": "s1",
            "terminal_context": {"custom": "preserved"},
        });
        inject(&mut data, "SessionStart");
        assert_eq!(data["terminal_context"]["custom"], "preserved");
        assert!(data["terminal_context"].get("parent_pid").is_some());
        assert!(data["terminal_context"].get("gobby_agent_run_id").is_some());
    }

    #[test]
    fn inject_no_op_on_non_object() {
        let mut data = json!("not an object");
        inject(&mut data, "SessionStart");
        assert_eq!(data, json!("not an object"));
    }

    #[test]
    fn capture_emits_expected_keys() {
        let ctx = build_context();
        let obj = ctx.as_object().expect("object");
        for key in [
            "parent_pid",
            "tty",
            "term_program",
            "gobby_session_id",
            "gobby_parent_session_id",
            "gobby_agent_run_id",
            "gobby_project_id",
            "gobby_workflow_name",
            "gobby_acp_child",
            "gobby_terminal_id",
            "gobby_pane_ref",
        ] {
            assert!(obj.contains_key(key), "missing key: {key}");
        }
        assert_eq!(obj.len(), 11, "the native terminal context contract");
    }
}
