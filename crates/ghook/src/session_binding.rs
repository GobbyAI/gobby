//! Caller identity for hook requests, decided per envelope.
//!
//! A spawned managed run keeps its managed token and headers. An unspawned session
//! presents the credential the daemon minted for it at SessionStart. Without one it
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
/// A `/clear` SessionStart starts a successor session, which only the machine path
/// mints, so it never presents the predecessor's credential.
pub fn caller_for(envelope: &Envelope) -> Caller {
    if session_credential::is_managed_environment() {
        return Caller::Managed;
    }
    if is_clear_session_start(envelope) {
        return Caller::Unbound;
    }
    let Some(provider_session_id) = provider_session_id(envelope) else {
        return Caller::Unbound;
    };
    let Ok(home) = gobby_core::gobby_home() else {
        return Caller::Unbound;
    };
    match session_credential::bound_for_provider(&home, provider_session_id) {
        Some(credential) => Caller::Bound(credential),
        None => Caller::Unbound,
    }
}

/// Keep the credential a SessionStart response carries; return the body without it.
///
/// The record goes under the provider's session id and under the CLI process that
/// owns this hook, which the MCP client derives from its own parent. A failed write
/// is reported without the token.
pub fn remember_response_credential<'a>(envelope: &Envelope, body: &'a str) -> Cow<'a, str> {
    let (stripped, credential) = session_credential::extract(body);
    let Some(mut credential) = credential else {
        return stripped;
    };
    let Some(provider_session_id) = provider_session_id(envelope) else {
        return stripped;
    };
    credential.provider_session_id = provider_session_id.to_string();
    let mut keys = vec![session_credential::provider_key(provider_session_id)];
    keys.extend(current_cli_pid().map(cli_pid_key));
    store_credential(&keys, &credential);
    stripped
}

/// Keep a bound SessionStart's credential under this CLI as well.
///
/// A restarted CLI has a new pid but the same provider session, so its MCP client
/// finds the credential it was already bound to. Only a bound SessionStart the daemon
/// accepted qualifies; a minted or unbound request has no credential to keep here.
pub fn remember_bound_credential(envelope: &Envelope, caller: &Caller) {
    let Caller::Bound(credential) = caller else {
        return;
    };
    if !is_session_start(envelope) {
        return;
    }
    if let Some(cli) = current_cli_pid() {
        store_credential(&[cli_pid_key(cli)], credential);
    }
}

fn store_credential(keys: &[String], credential: &SessionCredential) {
    let stored = gobby_core::gobby_home()
        .and_then(|home| session_credential::store(&home, keys, credential));
    if let Err(error) = stored {
        crate::output::stderr(format_args!(
            "ghook: could not keep the session credential: {error}\n"
        ));
    }
}

fn is_session_start(envelope: &Envelope) -> bool {
    normalized_hook_type(&envelope.hook_type) == "sessionstart"
}

/// Whether the envelope is a SessionStart that a `/clear` produced.
fn is_clear_session_start(envelope: &Envelope) -> bool {
    is_session_start(envelope)
        && envelope.input_data.get("source").and_then(Value::as_str) == Some("clear")
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
