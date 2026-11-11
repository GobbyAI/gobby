//! The session-bound credential an unspawned MCP client presents.
//!
//! The hook client stores the daemon's credential for its session under the CLI
//! process that runs both the hooks and this MCP client. The MCP client reads it back
//! by deriving the same CLI pid from its own parent. A managed run never reads one.
//!
//! Under Codex's shared app-server worker (#25905) the parent runs every hand-started
//! thread, so its pid names no session and its record is never read. Each tool call
//! instead names its thread in `_meta`, and the dialer exchanges that thread id for the
//! thread's own credential (see `daemon::Dialer`).

use gobby_core::process_identity::{current_cli_pid, parent_is_codex_shared_host};
use gobby_core::session_credential::{self, SessionCredential, cli_pid_key};
use serde_json::{Map, Value};

/// The credential stored for this unspawned client's CLI process, if one exists.
pub fn cli_binding() -> Option<SessionCredential> {
    if session_credential::is_managed_environment() || parent_is_codex_shared_host() {
        return None;
    }
    let home = gobby_core::gobby_home().ok()?;
    session_credential::load(&home, &cli_pid_key(current_cli_pid()?))
}

/// Whether this unspawned client runs under Codex's shared app-server worker.
pub fn under_shared_host() -> bool {
    !session_credential::is_managed_environment() && parent_is_codex_shared_host()
}

/// The Codex thread a tool call's `_meta` names: `threadId`, or the turn metadata's.
pub fn codex_thread_id(meta: &Map<String, Value>) -> Option<String> {
    meta.get("threadId")
        .and_then(Value::as_str)
        .or_else(|| {
            meta.get("x-codex-turn-metadata")
                .and_then(|turn| turn.get("thread_id"))
                .and_then(Value::as_str)
        })
        .filter(|thread| !thread.is_empty())
        .map(str::to_owned)
}

#[cfg(test)]
#[path = "session_binding/tests.rs"]
mod tests;
