//! The native surfaces the front door claims. Each surface is a route family that
//! carries claim rows only: it has no table path (empty prefixes and an empty router),
//! so the routing table never selects it, and the claim runner is the only route to it.
//! The routing table pins every surface to `Native`, whatever the `front_door.routes`
//! map says.

use axum::Router;
use axum::http::Method;
use gobby_core::route_family::{
    Credential, Gate, Kind, PathMatch, PeerRule, RouteClaim, RouteFamily,
};

use super::FrontDoorState;

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
    path: PathMatch::Exact("/api/internal/terminals/write"),
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
}

/// Node channel route.
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
}
