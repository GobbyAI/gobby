//! Caller identity for hook requests, decided per envelope.
//!
//! A spawned managed run keeps its managed token and headers. An unspawned session
//! presents the credential the daemon minted for it at SessionStart. Without one it
//! sends the machine key and no session selector, because the daemon refuses a
//! machine-bearer request that names a session.

use std::borrow::Cow;

use gobby_core::session_credential::{self, SESSION_SELECTOR_HEADERS, SessionCredential};
use serde_json::Value;

use crate::envelope::Envelope;

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
pub fn caller_for(envelope: &Envelope) -> Caller {
    if session_credential::is_managed_environment() {
        return Caller::Managed;
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
/// The record goes under the provider's session id and, inside tmux, under the pane
/// the MCP client can also derive. A failed write is reported without the token.
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
    let tmux = std::env::var("TMUX").ok();
    let tmux_pane = std::env::var("TMUX_PANE").ok();
    if let Some(pane) = session_credential::pane_key(tmux.as_deref(), tmux_pane.as_deref()) {
        keys.push(pane);
    }
    let stored = gobby_core::gobby_home()
        .and_then(|home| session_credential::store(&home, &keys, &credential));
    if let Err(error) = stored {
        crate::output::stderr(format_args!(
            "ghook: could not keep the session credential: {error}\n"
        ));
    }
    stripped
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
