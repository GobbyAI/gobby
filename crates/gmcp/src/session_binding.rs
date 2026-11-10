//! The session-bound credential an unspawned MCP client presents.
//!
//! The hook client stores the daemon's credential for its session under the CLI
//! process that runs both the hooks and this MCP client. The MCP client reads it back
//! by deriving the same CLI pid from its own parent. A seat on a shared host, which
//! every seat on it shares, is stored under its own session refs instead; the client
//! finds it by the session_id its call names, and the hook gate refuses a call naming
//! any session other than the caller's own. A managed run never reads one.

use gobby_core::process_identity::current_cli_pid;
use gobby_core::session_credential::{self, SessionCredential, cli_pid_key};

/// The credential stored for this unspawned client's CLI process, if one exists.
pub fn cli_binding() -> Option<SessionCredential> {
    if session_credential::is_managed_environment() {
        return None;
    }
    let home = gobby_core::gobby_home().ok()?;
    session_credential::load(&home, &cli_pid_key(current_cli_pid()?))
}

/// The credential a call presents: its CLI's own record first, else the record stored
/// under the session ref the call names.
pub fn binding_for(session_ref: Option<&str>) -> Option<SessionCredential> {
    if let Some(binding) = cli_binding() {
        return Some(binding);
    }
    if session_credential::is_managed_environment() {
        return None;
    }
    let reference = session_ref
        .map(str::trim)
        .filter(|reference| !reference.is_empty())?;
    let home = gobby_core::gobby_home().ok()?;
    session_credential::load(&home, &session_ref_key(reference))
}

/// Store key for a shared-host seat's record under one of its own session refs; ghook
/// writes the same key.
pub fn session_ref_key(session_ref: &str) -> String {
    format!("session-ref:{session_ref}")
}

#[cfg(test)]
#[path = "session_binding/tests.rs"]
mod tests;
