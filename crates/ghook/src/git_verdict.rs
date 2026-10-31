//! Verdict for the git reference-transaction guard (`ghook --cli=git`).
//!
//! The guard answers a protected-branch ref update, so only a delivered
//! `decision: "allow"` exits 0. A `block` exits 1 with the daemon's reason.
//! Every other outcome exits 2 and the guard refuses: an unreachable, disabled
//! or unmanaged daemon, a retry, a malformed body, or an unknown decision.
//! Verdicts are synchronous and never persisted for replay.

use serde_json::Value;

use crate::action::HookAction;

pub(crate) const GIT_SOURCE: &str = "git";

const BLOCK_FALLBACK_REASON: &str = "blocked by the gobby git guard";

pub(crate) fn is_git_source(source: &str) -> bool {
    source == GIT_SOURCE
}

/// Maps a delivery to the guard's exit code. Only an HTTP 200 carries a verdict;
/// any other 2xx (for example 201 or 204) is refused.
pub(crate) fn verdict_from_delivery(status_code: Option<u16>, body: &str) -> HookAction {
    if status_code != Some(200) {
        return fail_closed("the daemon did not answer 200 OK");
    }
    verdict_from_body(body)
}

/// Maps a delivered 2xx body to the guard's exit code.
pub(crate) fn verdict_from_body(body: &str) -> HookAction {
    let Ok(value) = serde_json::from_str::<Value>(body) else {
        return fail_closed("the daemon's verdict was not JSON");
    };
    match value.get("decision").and_then(Value::as_str) {
        Some("allow") => HookAction {
            exit_code: 0,
            stdout_json: None,
            stderr_message: None,
        },
        Some("block") => HookAction {
            exit_code: 1,
            stdout_json: None,
            stderr_message: Some(
                value
                    .get("reason")
                    .and_then(Value::as_str)
                    .filter(|reason| !reason.is_empty())
                    .unwrap_or(BLOCK_FALLBACK_REASON)
                    .to_string(),
            ),
        },
        _ => fail_closed("the daemon's verdict had no allow or block decision"),
    }
}

/// Refuses the ref update when no verdict could be read.
pub(crate) fn fail_closed(detail: &str) -> HookAction {
    HookAction {
        exit_code: 2,
        stdout_json: None,
        stderr_message: Some(format!("ghook: git guard refused: {detail}")),
    }
}

#[cfg(test)]
#[path = "git_verdict/tests.rs"]
mod tests;
