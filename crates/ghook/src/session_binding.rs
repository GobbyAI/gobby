//! Caller identity for hook requests, decided per envelope.
//!
//! A spawned managed run keeps its managed token and headers. An unspawned session
//! presents the credential the daemon minted for its CLI process. Without one it
//! sends the machine key and no session selector, because the daemon refuses a
//! machine-bearer request that names a session.

use std::borrow::Cow;

use gobby_core::process_identity::current_cli_pid;
use gobby_core::session_credential::{
    self, SESSION_SELECTOR_HEADERS, SessionCredential, cli_pid_key,
};
use serde_json::Value;

use crate::envelope::Envelope;
use crate::terminal_context::normalized_hook_type;

/// How one envelope's request authenticates.
#[derive(Debug)]
pub enum Caller {
    /// A spawned managed run: the envelope headers and ordinary key stand.
    Managed,
    /// An unspawned session presenting its own credential.
    Bound(SessionCredential),
    /// An unspawned session with no credential: the machine key, no session selector.
    Unbound,
}

impl Caller {
    /// Whether the request is unspawned, so its session selectors must not be sent
    /// with the machine key.
    pub fn is_unspawned(&self) -> bool {
        !matches!(self, Caller::Managed)
    }
}

/// Whether a request header selects a session, run or execution.
pub fn is_session_selector(name: &str) -> bool {
    SESSION_SELECTOR_HEADERS
        .iter()
        .any(|selector| name.eq_ignore_ascii_case(selector))
}

/// Decide how the envelope's request authenticates.
///
/// Every SessionStart reattests the CLI through the machine path and receives a fresh
/// credential, including after activation invalidates a cached provider token. Any other
/// hook presents only the record stored for this hook's own CLI process, which the kernel
/// names. The session the body names never selects a record; it is only checked against
/// the one this CLI holds.
pub fn caller_for(envelope: &Envelope) -> Caller {
    if session_credential::is_managed_environment() {
        return Caller::Managed;
    }
    if is_session_start(envelope) {
        return Caller::Unbound;
    }
    let Some(provider_session_id) = provider_session_id(envelope) else {
        return Caller::Unbound;
    };
    let (Ok(home), Some(cli)) = (gobby_core::gobby_home(), current_cli_pid()) else {
        return Caller::Unbound;
    };
    match session_credential::load(&home, &cli_pid_key(cli)) {
        Some(credential) if credential.provider_session_id == provider_session_id => {
            Caller::Bound(credential)
        }
        _ => Caller::Unbound,
    }
}

/// Keep the credential a hook response carries; return the body without it.
///
/// The daemon mints on a SessionStart or on the first hook that creates a deferred
/// session's row. The record goes only under the CLI process that owns this hook, which
/// the MCP client derives from its own parent, and names the provider session it is for.
/// A failed write is reported without the token.
pub fn remember_response_credential<'a>(envelope: &Envelope, body: &'a str) -> Cow<'a, str> {
    let (stripped, credential) = session_credential::extract(body);
    let Some(mut credential) = credential else {
        return stripped;
    };
    let (Some(provider_session_id), Some(cli)) = (provider_session_id(envelope), current_cli_pid())
    else {
        return stripped;
    };
    credential.provider_session_id = provider_session_id.to_string();
    store_credential(cli, &credential);
    stripped
}

fn store_credential(cli: u32, credential: &SessionCredential) {
    let stored = gobby_core::gobby_home()
        .and_then(|home| session_credential::store(&home, &[cli_pid_key(cli)], credential));
    if let Err(error) = stored {
        crate::output::stderr(format_args!(
            "ghook: could not keep the session credential: {error}\n"
        ));
    }
}

fn is_session_start(envelope: &Envelope) -> bool {
    normalized_hook_type(&envelope.hook_type) == "sessionstart"
}

fn provider_session_id(envelope: &Envelope) -> Option<&str> {
    envelope
        .input_data
        .get("session_id")
        .and_then(Value::as_str)
        .filter(|id| !id.is_empty())
}

#[cfg(test)]
#[path = "session_binding/tests.rs"]
mod tests;
