//! The session-bound credential an unspawned MCP client presents.
//!
//! The hook client stores the daemon's credential for its session under the CLI
//! process that runs both the hooks and this MCP client. The MCP client reads it back
//! by deriving the same CLI pid from its own parent. A managed run never reads one.

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

#[cfg(test)]
#[path = "session_binding/tests.rs"]
mod tests;
