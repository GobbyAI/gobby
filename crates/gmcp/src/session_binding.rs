//! The session-bound credential an unspawned MCP client presents.
//!
//! The hook client stores the daemon's credential for its session under the tmux
//! pane. The MCP client, started in that same pane, reads it back from there. Outside
//! tmux the two clients share no key, so an unspawned MCP client there has no binding.

use gobby_core::session_credential::{self, SessionCredential};

/// The credential stored for this unspawned client's tmux pane, if one exists.
pub fn pane_binding() -> Option<SessionCredential> {
    if session_credential::is_managed_environment() {
        return None;
    }
    let home = gobby_core::gobby_home().ok()?;
    let tmux = std::env::var("TMUX").ok();
    let pane = std::env::var("TMUX_PANE").ok();
    session_credential::bound_for_pane(&home, tmux.as_deref(), pane.as_deref())
}

#[cfg(test)]
#[path = "session_binding/tests.rs"]
mod tests;
