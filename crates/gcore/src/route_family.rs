//! The route-family seam: a group of HTTP routes the front door can serve itself.
//!
//! gdaemon composes families into its routing table and picks each family's
//! `Proxy | Native | Compare` backend from bootstrap `front_door.routes`. Family
//! crates implement this trait without depending on gdaemon.

use std::future::Future;
use std::net::SocketAddr;
use std::pin::Pin;

use axum::Router;
use axum::body::Body;
use axum::http::request::Parts;
use axum::http::{Method, Request, Response, StatusCode};

use crate::bootstrap::RouteBackend;
use crate::family::FamilyContext;
use crate::family::lifecycle::{Activation, HookError, SpawnKind, StopFailure, StopMode};

/// A boxed future that may borrow from the family or the request state for `'a`.
pub type BoxFut<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Where a claim runs relative to the front door's refusal gate.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Gate {
    /// The claim answers even while the backend refuses requests.
    BeforeRefusal,
    /// The claim runs only for requests the refusal gate lets through.
    AfterRefusal,
}

/// Whether a claim's handler runs before or after the credential check.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Credential {
    /// The handler runs before any credential check.
    Unchecked,
    /// The handler runs after authentication. Identity may still be absent: a request
    /// without credentials passes authentication with no identity, so a handler that
    /// acts on a caller refuses an absent one.
    AfterAuthenticate,
}

/// How a claim matches the request path. The path is the raw, undecoded path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PathMatch {
    /// The path equals the string.
    Exact(&'static str),
    /// The path equals the string or continues it past a `/` boundary.
    Segment(&'static str),
    /// The path starts with the string.
    RawPrefix(&'static str),
    /// Every path.
    Any,
}

/// Which peers a claim accepts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PeerRule {
    /// Every peer.
    Any,
    /// Only loopback peers. An IPv4-mapped IPv6 address counts as its IPv4 address, so
    /// the test is `peer.ip().to_canonical().is_loopback()`.
    LoopbackOnly,
}

/// Whether a claim applies to upgrade requests.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// Upgrade and plain requests alike.
    Any,
    /// Upgrade requests only.
    UpgradeOnly,
}

/// One row of a family's claim table: a request shape the family answers itself.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RouteClaim {
    /// The method the row answers, or every method when `None`.
    pub method: Option<Method>,
    /// How the row matches the request path.
    pub path: PathMatch,
    /// Where the row runs relative to the refusal gate.
    pub gate: Gate,
    /// Whether the row's handler runs before or after the credential check.
    pub credential: Credential,
    /// Which peers the row accepts.
    pub peer: PeerRule,
    /// Whether the row applies to upgrade requests.
    pub kind: Kind,
}

impl RouteClaim {
    /// Whether this row claims a request with the given method, raw path and upgrade flag.
    pub fn matches(&self, method: &Method, path: &str, upgrade: bool) -> bool {
        let method_matches = self.method.as_ref().is_none_or(|claimed| claimed == method);
        let path_matches = match self.path {
            PathMatch::Exact(exact) => path == exact,
            PathMatch::Segment(segment) => path
                .strip_prefix(segment)
                .is_some_and(|rest| rest.is_empty() || rest.starts_with('/')),
            PathMatch::RawPrefix(prefix) => path.starts_with(prefix),
            PathMatch::Any => true,
        };
        let kind_matches = match self.kind {
            Kind::Any => true,
            Kind::UpgradeOnly => upgrade,
        };
        method_matches && path_matches && kind_matches
    }
}

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
    /// The rows of the family's claim table. A family with none is reached through its
    /// prefixes alone.
    fn route_claims(&self) -> &'static [RouteClaim] {
        &[]
    }
    /// False leaves the family's claims unanswered, whatever their rows say.
    fn claim_enabled(&self, _state: &S) -> bool {
        true
    }
    /// Answers a request that one of the family's rows claimed. `Ok(request)` continues
    /// to the next phase; `Err(response)` answers the request.
    fn serve_claim<'a>(
        &'a self,
        _state: &'a S,
        _peer: SocketAddr,
        _request: Request<Body>,
    ) -> BoxFut<'a, Result<Request<Body>, Response<Body>>> {
        Box::pin(async {
            let mut response = Response::new(Body::empty());
            *response.status_mut() = StatusCode::NOT_IMPLEMENTED;
            Err(response)
        })
    }
    /// Lower ranks start first; families with equal ranks keep their registry order.
    /// Deactivation runs in the reverse of start order.
    fn start_rank(&self) -> u8 {
        0
    }
    /// Runs once per process, before admission.
    fn start<'a>(&'a self, _ctx: &'a FamilyContext) -> BoxFut<'a, Result<(), HookError>> {
        Box::pin(async { Ok(()) })
    }
    /// Runs once per lease acquisition, before the lease is verified for the lifetime: the
    /// supervised lifetime's entry heartbeat and its job admission come after every `activate`.
    /// Jobs registered through `activation` are staged for the lease and start only at that
    /// admission. Nothing else a hook does here is held back or fenced by the lease: an effect
    /// that needs the lease's authority (a write, a spawned task, a call a peer daemon must not
    /// repeat) must carry its own fence, by stopping on the context's lease state or by
    /// guarding the effect in its own SQL. Heartbeats verify that lease state only from the
    /// admission to the lifetime's last heartbeat before its release; outside that interval it
    /// can read Active with nothing verifying it, so an effect that must not outlive the lease
    /// needs its own SQL guard there.
    fn activate<'a>(
        &'a self,
        _activation: &'a mut Activation<'_>,
    ) -> BoxFut<'a, Result<(), HookError>> {
        Box::pin(async { Ok(()) })
    }
    /// Runs once per lease release or drain, in reverse start order. It also unwinds a
    /// fatal `start` or `activate`, the failing family's own included, so it must be safe
    /// on a family that is only partly started or activated: it releases what was
    /// acquired and skips what was not.
    fn deactivate<'a>(&'a self, _mode: StopMode) -> BoxFut<'a, Result<(), StopFailure>> {
        Box::pin(async { Ok(()) })
    }
    /// Runs before each worker spawn.
    fn before_spawn<'a>(
        &'a self,
        _ctx: &'a FamilyContext,
        _spawn: SpawnKind,
    ) -> BoxFut<'a, Result<(), HookError>> {
        Box::pin(async { Ok(()) })
    }
    /// Runs after each healthy start or respawn of the worker.
    fn after_healthy<'a>(&'a self, _ctx: &'a FamilyContext) -> BoxFut<'a, Result<(), HookError>> {
        Box::pin(async { Ok(()) })
    }
    /// Runs once, when recovery completes.
    fn recovery_complete<'a>(
        &'a self,
        _ctx: &'a FamilyContext,
    ) -> BoxFut<'a, Result<(), HookError>> {
        Box::pin(async { Ok(()) })
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
