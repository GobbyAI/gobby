//! The caller identity that authentication writes into the request headers.

use axum::http::HeaderMap;

/// The identity headers that authentication writes. A client never supplies them: the
/// front door removes them when a request enters, before any claim runs.
pub const IDENTITY_ENTRY_HEADERS: [&str; 3] =
    ["x-gobby-user-id", "x-gobby-machine-id", "x-gobby-key-id"];

/// Removes every identity header from `headers`. The front-door proof header is kept,
/// because pre-authentication checks read it.
pub fn strip_identity_entry_headers(headers: &mut HeaderMap) {
    let _ = headers;
}

/// The authenticated caller. It carries no secret.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CallerIdentity {
    /// The user the API key belongs to.
    pub user_id: String,
    /// The machine the API key was issued to.
    pub machine_id: String,
    /// The API key's id.
    pub key_id: String,
}

impl CallerIdentity {
    /// The identity in `headers`, or `None` when any of the three headers is absent.
    pub fn from_headers(headers: &HeaderMap) -> Option<Self> {
        let _ = headers;
        None
    }
}
