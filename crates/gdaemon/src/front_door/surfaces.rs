//! The native surfaces the front door claims. Each surface is a route family that
//! carries claim rows and answers its claimed requests in `serve_claim`. It has no table
//! path (empty prefixes and an empty router), so the routing table never selects it, and
//! the claim runner is the only route to it. The routing table pins every surface to
//! `Native`, whatever the `front_door.routes` map says.

use std::net::SocketAddr;

use axum::Router;
use axum::body::Body;
use axum::http::{Method, Request, Response};
use gobby_core::route_family::{
    BoxFut, Credential, Gate, Kind, PathMatch, PeerRule, RouteClaim, RouteFamily,
};

use super::FrontDoorState;
#[cfg(unix)]
use super::auth;
use super::challenge;
use super::is_browser_ws;
#[cfg(unix)]
use super::terminal_api;
#[cfg(unix)]
use crate::lease::admin_api;
use crate::nodes::{NativeRoutes, channel, internal_api, machines_api};
use crate::websocket::publish;
use crate::websocket::subscriptions::EntryMode;

static WS_BRIDGE_CLAIMS: &[RouteClaim] = &[RouteClaim {
    method: None,
    path: PathMatch::RawPrefix("/_gobby/"),
    gate: Gate::BeforeRefusal,
    credential: Credential::Unchecked,
    peer: PeerRule::Any,
    kind: Kind::Any,
}];

#[cfg(unix)]
static TERMINAL_WRITE_CLAIMS: &[RouteClaim] = &[RouteClaim {
    method: Some(Method::POST),
    path: PathMatch::Exact(terminal_api::PATH),
    gate: Gate::BeforeRefusal,
    credential: Credential::Unchecked,
    peer: PeerRule::Any,
    kind: Kind::Any,
}];

#[cfg(unix)]
static LEASE_ADMIN_CLAIMS: &[RouteClaim] = &[
    RouteClaim {
        method: None,
        path: PathMatch::Exact("/api/admin/lease/status"),
        gate: Gate::BeforeRefusal,
        credential: Credential::AfterAuthenticate,
        peer: PeerRule::Any,
        kind: Kind::Any,
    },
    RouteClaim {
        method: None,
        path: PathMatch::Exact("/api/admin/lease/promote"),
        gate: Gate::BeforeRefusal,
        credential: Credential::AfterAuthenticate,
        peer: PeerRule::Any,
        kind: Kind::Any,
    },
    RouteClaim {
        method: None,
        path: PathMatch::Exact("/api/admin/lease/recover"),
        gate: Gate::BeforeRefusal,
        credential: Credential::AfterAuthenticate,
        peer: PeerRule::Any,
        kind: Kind::Any,
    },
];

static NODE_INTERNAL_CLAIMS: &[RouteClaim] = &[RouteClaim {
    method: None,
    path: PathMatch::RawPrefix("/api/internal/nodes/"),
    gate: Gate::AfterRefusal,
    credential: Credential::Unchecked,
    peer: PeerRule::LoopbackOnly,
    kind: Kind::Any,
}];

static RUNTIME_CHALLENGE_CLAIMS: &[RouteClaim] = &[RouteClaim {
    method: Some(Method::POST),
    path: PathMatch::Exact("/api/runtime/handshake/challenge"),
    gate: Gate::AfterRefusal,
    credential: Credential::Unchecked,
    peer: PeerRule::Any,
    kind: Kind::Any,
}];

static NODE_CHANNEL_CLAIMS: &[RouteClaim] = &[RouteClaim {
    method: None,
    path: PathMatch::Exact("/api/nodes/channel"),
    gate: Gate::AfterRefusal,
    credential: Credential::AfterAuthenticate,
    peer: PeerRule::Any,
    kind: Kind::Any,
}];

static MACHINES_CLAIMS: &[RouteClaim] = &[RouteClaim {
    method: None,
    path: PathMatch::Segment("/api/machines"),
    gate: Gate::AfterRefusal,
    credential: Credential::AfterAuthenticate,
    peer: PeerRule::Any,
    kind: Kind::Any,
}];

static WS_NATIVE_CLAIMS: &[RouteClaim] = &[RouteClaim {
    method: None,
    path: PathMatch::Any,
    gate: Gate::AfterRefusal,
    credential: Credential::AfterAuthenticate,
    peer: PeerRule::Any,
    kind: Kind::UpgradeOnly,
}];

/// Native WebSocket bridge for the publish path under `/_gobby/`.
pub struct WsBridgeFamily;

impl RouteFamily<FrontDoorState> for WsBridgeFamily {
    fn name(&self) -> &'static str {
        "ws_bridge"
    }
    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }
    fn router(&self) -> Router<FrontDoorState> {
        Router::new()
    }
    fn route_claims(&self) -> &'static [RouteClaim] {
        WS_BRIDGE_CLAIMS
    }
    fn serve_claim<'a>(
        &'a self,
        state: &'a FrontDoorState,
        peer: SocketAddr,
        request: Request<Body>,
    ) -> BoxFut<'a, Result<Request<Body>, Response<Body>>> {
        Box::pin(async move { Err(publish::handle(&state.native_ws, request, peer).await) })
    }
}

/// Terminal write route, answered only while the runtime is present.
#[cfg(unix)]
pub struct TerminalWriteFamily;

#[cfg(unix)]
impl RouteFamily<FrontDoorState> for TerminalWriteFamily {
    fn name(&self) -> &'static str {
        "terminal_write"
    }
    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }
    fn router(&self) -> Router<FrontDoorState> {
        Router::new()
    }
    fn route_claims(&self) -> &'static [RouteClaim] {
        TERMINAL_WRITE_CLAIMS
    }
    fn claim_enabled(&self, state: &FrontDoorState) -> bool {
        state.runtime.is_some()
    }
    fn serve_claim<'a>(
        &'a self,
        state: &'a FrontDoorState,
        peer: SocketAddr,
        request: Request<Body>,
    ) -> BoxFut<'a, Result<Request<Body>, Response<Body>>> {
        Box::pin(async move {
            let Some(runtime) = state.runtime.as_deref() else {
                return Ok(request);
            };
            Err(terminal_api::handle(runtime, peer, request).await)
        })
    }
}

/// Lease administration routes. The rows match by path; the method is checked by the
/// handler, so a wrong method keeps the handler's answer.
#[cfg(unix)]
pub struct LeaseAdminFamily;

#[cfg(unix)]
impl RouteFamily<FrontDoorState> for LeaseAdminFamily {
    fn name(&self) -> &'static str {
        "lease_admin"
    }
    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }
    fn router(&self) -> Router<FrontDoorState> {
        Router::new()
    }
    fn route_claims(&self) -> &'static [RouteClaim] {
        LEASE_ADMIN_CLAIMS
    }
    fn claim_enabled(&self, state: &FrontDoorState) -> bool {
        state.runtime.is_some()
    }
    fn serve_claim<'a>(
        &'a self,
        state: &'a FrontDoorState,
        _peer: SocketAddr,
        request: Request<Body>,
    ) -> BoxFut<'a, Result<Request<Body>, Response<Body>>> {
        Box::pin(async move {
            let Some(runtime) = state.runtime.as_deref() else {
                return Ok(request);
            };
            if !request.headers().contains_key("x-gobby-key-id") {
                return Err(auth::rejection(
                    axum::http::StatusCode::UNAUTHORIZED,
                    "authentication_required",
                    "A valid API key is required for native lease administration",
                ));
            }
            Err(admin_api::handle(runtime, request).await)
        })
    }
}

/// Node-internal routes, answered only for loopback peers.
pub struct NodeInternalFamily;

impl RouteFamily<FrontDoorState> for NodeInternalFamily {
    fn name(&self) -> &'static str {
        "node_internal"
    }
    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }
    fn router(&self) -> Router<FrontDoorState> {
        Router::new()
    }
    fn route_claims(&self) -> &'static [RouteClaim] {
        NODE_INTERNAL_CLAIMS
    }
    fn serve_claim<'a>(
        &'a self,
        state: &'a FrontDoorState,
        peer: SocketAddr,
        request: Request<Body>,
    ) -> BoxFut<'a, Result<Request<Body>, Response<Body>>> {
        Box::pin(async move {
            Err(internal_api::handle(&state.native, &state.auth, request, peer).await)
        })
    }
}

/// Runtime handshake challenge route.
pub struct RuntimeChallengeFamily;

impl RouteFamily<FrontDoorState> for RuntimeChallengeFamily {
    fn name(&self) -> &'static str {
        "runtime_challenge"
    }
    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }
    fn router(&self) -> Router<FrontDoorState> {
        Router::new()
    }
    fn route_claims(&self) -> &'static [RouteClaim] {
        RUNTIME_CHALLENGE_CLAIMS
    }
    fn serve_claim<'a>(
        &'a self,
        state: &'a FrontDoorState,
        _peer: SocketAddr,
        request: Request<Body>,
    ) -> BoxFut<'a, Result<Request<Body>, Response<Body>>> {
        Box::pin(async move {
            challenge::answer(&state.auth.bootstrap_path, request)
                .await
                .map_err(|response| *response)
        })
    }
}

/// Node channel route, served only by a hub.
pub struct NodeChannelFamily;

impl RouteFamily<FrontDoorState> for NodeChannelFamily {
    fn name(&self) -> &'static str {
        "node_channel"
    }
    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }
    fn router(&self) -> Router<FrontDoorState> {
        Router::new()
    }
    fn route_claims(&self) -> &'static [RouteClaim] {
        NODE_CHANNEL_CLAIMS
    }
    fn claim_enabled(&self, state: &FrontDoorState) -> bool {
        matches!(state.native, NativeRoutes::Hub(_))
    }
    fn serve_claim<'a>(
        &'a self,
        state: &'a FrontDoorState,
        _peer: SocketAddr,
        request: Request<Body>,
    ) -> BoxFut<'a, Result<Request<Body>, Response<Body>>> {
        Box::pin(async move {
            match &state.native {
                NativeRoutes::Hub(nodes) => Err(channel::handle(nodes.clone(), request).await),
                NativeRoutes::Standalone(_) => Ok(request),
            }
        })
    }
}

/// Machines routes, under `/api/machines` and below.
pub struct MachinesFamily;

impl RouteFamily<FrontDoorState> for MachinesFamily {
    fn name(&self) -> &'static str {
        "machines"
    }
    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }
    fn router(&self) -> Router<FrontDoorState> {
        Router::new()
    }
    fn route_claims(&self) -> &'static [RouteClaim] {
        MACHINES_CLAIMS
    }
    fn serve_claim<'a>(
        &'a self,
        state: &'a FrontDoorState,
        _peer: SocketAddr,
        request: Request<Body>,
    ) -> BoxFut<'a, Result<Request<Body>, Response<Body>>> {
        Box::pin(async move {
            Err(machines_api::handle(machines_api::router(state.native.clone()), request).await)
        })
    }
}

/// Browser and listener WebSocket upgrades.
pub struct WsNativeFamily;

impl RouteFamily<FrontDoorState> for WsNativeFamily {
    fn name(&self) -> &'static str {
        "ws_native"
    }
    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }
    fn router(&self) -> Router<FrontDoorState> {
        Router::new()
    }
    fn route_claims(&self) -> &'static [RouteClaim] {
        WS_NATIVE_CLAIMS
    }
    fn serve_claim<'a>(
        &'a self,
        state: &'a FrontDoorState,
        peer: SocketAddr,
        request: Request<Body>,
    ) -> BoxFut<'a, Result<Request<Body>, Response<Body>>> {
        Box::pin(async move {
            let entry = if state.ws_listener {
                EntryMode::Direct
            } else if is_browser_ws(request.uri().path()) {
                EntryMode::Browser
            } else {
                return Ok(request);
            };
            Err(state.native_ws.accept(request, entry, peer).await)
        })
    }
}
