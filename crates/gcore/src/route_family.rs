//! The route-family seam: a group of HTTP routes the front door can serve itself.
//!
//! gdaemon composes families into its routing table and picks each family's
//! `Proxy | Native | Compare` backend from bootstrap `front_door.routes`. Family
//! crates implement this trait without depending on gdaemon.

use std::future::Future;
use std::pin::Pin;

use axum::Router;
use axum::http::StatusCode;
use axum::http::request::Parts;

use crate::bootstrap::RouteBackend;

/// A group of routes that gdaemon can serve itself, over router state `S`.
///
/// A family with its own state returns `Router::new().route(..).with_state(own)`,
/// which is a `Router<S>` for any `S`.
pub trait RouteFamily<S>: Send + Sync {
    /// The family name used as the key in `front_door.routes`.
    fn name(&self) -> &'static str;
    /// Path prefixes the family owns; matched on whole path segments.
    fn prefixes(&self) -> &'static [&'static str];
    /// Native handlers. Requests under a claimed prefix that no handler matches are
    /// proxied.
    fn router(&self) -> Router<S>;
    /// The backend used when `front_door.routes` has no entry for this family.
    fn default_backend(&self) -> RouteBackend {
        RouteBackend::Proxy
    }
    /// False sends the request to the proxy unchanged, whatever the backend.
    fn claims(&self, _parts: &Parts) -> bool {
        true
    }
    /// Compare mode, claimed non-GET/HEAD request: check the proxied response against
    /// the request that produced it. `request` is the body bytes compare already
    /// buffered.
    fn check_proxied<'a>(
        &'a self,
        _parts: &'a Parts,
        _request: &'a [u8],
        _status: StatusCode,
        _response: &'a [u8],
    ) -> Pin<Box<dyn Future<Output = ProxiedCheck> + Send + 'a>> {
        Box::pin(async { ProxiedCheck::Skipped })
    }
}

/// The outcome of [`RouteFamily::check_proxied`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProxiedCheck {
    /// The family does not check this request.
    Skipped,
    /// The proxied response is consistent with the request.
    Verified,
    /// A JSON pointer or reason naming the inconsistency, never request or response
    /// values.
    Mismatch(String),
}
