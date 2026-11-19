//! The claim runner: the backend door's ordered pass over the native surfaces' claim rows.
//!
//! A request passes four claim phases in order. Each phase consults at most one claim,
//! the most specific row of that phase that its family's `claim_enabled` admits. A claim
//! that answers ends the request. A claim that passes hands the request to the next
//! phase. The refusal gate runs between the second phase and the third, while the runtime
//! is present. Authentication runs once: before the first claim that needs it, and before
//! the last phase. A request that no phase answers goes to the fallback, which splices
//! upgrades, answers the upgrade-only listener's plain requests, and dispatches the rest
//! through the routing table.

use std::net::{IpAddr, SocketAddr};

use axum::body::Body;
use axum::http::{Request, Response, StatusCode};
use gobby_core::route_family::{Credential, Gate, PeerRule};

use super::FrontDoorState;
use super::health::{self, BackendState};
use super::routes::RouteTable;
use super::ws;
#[cfg(unix)]
use crate::lifecycle::backend::BackendStatus;
use crate::nodes::internal_api;

/// What the claim runner decided for one backend-door request.
pub enum Flow {
    /// A claim answered the request.
    Answer(Response<Body>),
    /// No claim answered, so the request continues to the fallback.
    Continue(Request<Body>),
}

/// The gate and credential that select the rows of one phase.
type Phase = (Gate, Credential);

const BEFORE_REFUSAL_UNCHECKED: Phase = (Gate::BeforeRefusal, Credential::Unchecked);
const BEFORE_REFUSAL_AUTHENTICATED: Phase = (Gate::BeforeRefusal, Credential::AfterAuthenticate);
const AFTER_REFUSAL_UNCHECKED: Phase = (Gate::AfterRefusal, Credential::Unchecked);
const AFTER_REFUSAL_AUTHENTICATED: Phase = (Gate::AfterRefusal, Credential::AfterAuthenticate);

/// Runs every claim phase for one backend-door request, in order. The state is the
/// request's own copy, so the backend state the refusal gate writes reaches the fallback.
pub async fn run_claims(
    table: &RouteTable,
    state: &mut FrontDoorState,
    peer: SocketAddr,
    request: Request<Body>,
) -> Flow {
    match claim_phases(table, state, peer, request).await {
        Ok(request) => Flow::Continue(request),
        Err(response) => Flow::Answer(response),
    }
}

async fn claim_phases(
    table: &RouteTable,
    state: &mut FrontDoorState,
    peer: SocketAddr,
    request: Request<Body>,
) -> Result<Request<Body>, Response<Body>> {
    let mut authenticated = false;
    let request = serve_phase(
        table,
        state,
        peer,
        request,
        BEFORE_REFUSAL_UNCHECKED,
        &mut authenticated,
    )
    .await?;
    let request = serve_phase(
        table,
        state,
        peer,
        request,
        BEFORE_REFUSAL_AUTHENTICATED,
        &mut authenticated,
    )
    .await?;
    if let Some(response) = refusal_gate(state) {
        return Err(response);
    }
    let request = serve_phase(
        table,
        state,
        peer,
        request,
        AFTER_REFUSAL_UNCHECKED,
        &mut authenticated,
    )
    .await?;
    let request = authenticate_once(state, request, &mut authenticated).await?;
    serve_phase(
        table,
        state,
        peer,
        request,
        AFTER_REFUSAL_AUTHENTICATED,
        &mut authenticated,
    )
    .await
}

/// Consults the one claim of `phase` that answers the request, if any. `Ok` passes the
/// request on; `Err` answers it.
async fn serve_phase(
    table: &RouteTable,
    state: &mut FrontDoorState,
    peer: SocketAddr,
    request: Request<Body>,
    phase: Phase,
    authenticated: &mut bool,
) -> Result<Request<Body>, Response<Body>> {
    let (gate, credential) = phase;
    let upgrade = ws::is_upgrade(request.headers());
    let Some((family, claim)) = table.claimant(
        state,
        request.method(),
        request.uri().path(),
        upgrade,
        gate,
        credential,
    ) else {
        return Ok(request);
    };
    if !peer_admitted(claim.peer, peer.ip()) {
        return Err(internal_api::not_found());
    }
    let request = if credential == Credential::AfterAuthenticate {
        authenticate_once(state, request, authenticated).await?
    } else {
        request
    };
    family.serve_claim(state, peer, request).await
}

/// Whether a claim's peer rule admits the peer. The loopback test is the canonical one,
/// so an IPv4-mapped loopback address counts as loopback.
fn peer_admitted(rule: PeerRule, peer: IpAddr) -> bool {
    match rule {
        PeerRule::Any => true,
        PeerRule::LoopbackOnly => peer.to_canonical().is_loopback(),
    }
}

/// Authenticates the request the first time it is called and returns it unchanged after.
async fn authenticate_once(
    state: &FrontDoorState,
    mut request: Request<Body>,
    authenticated: &mut bool,
) -> Result<Request<Body>, Response<Body>> {
    if !*authenticated {
        state
            .auth
            .authenticate(request.headers_mut())
            .await
            .map_err(|response| *response)?;
        *authenticated = true;
    }
    Ok(request)
}

/// The refusal and standby gate. While the runtime is present it records the backend
/// state on the request's state and answers 503 while the backend is refused or the
/// node is on standby.
#[cfg(unix)]
fn refusal_gate(state: &mut FrontDoorState) -> Option<Response<Body>> {
    let runtime = state.runtime.as_ref()?;
    let backend = runtime.backend.borrow().clone();
    state.backend_state = match &backend {
        BackendStatus::Starting { .. } => BackendState::Starting,
        _ => BackendState::Down,
    };
    let refusal = match backend {
        BackendStatus::Refused { reason } => Some(
            serde_json::json!({"error": "backend_refused", "status": "unavailable", "backend": {
                "state": "refused", "refusal": reason, "target": state.target.to_string()
            }}),
        ),
        _ if runtime.standby() => {
            let mut body = serde_json::json!({
                "error": "standby", "status": "unavailable", "state": "standby", "backend": {
                    "state": "standby", "target": state.target.to_string()
                }
            });
            if let Some(wait) = runtime.backend_wait.borrow().clone() {
                body["backend"]["wait"] = serde_json::json!(wait);
            }
            Some(body)
        }
        _ => None,
    };
    refusal.map(|body| health::json_response(StatusCode::SERVICE_UNAVAILABLE, body, true))
}

/// Without a runtime there is no refusal gate.
#[cfg(not(unix))]
fn refusal_gate(_state: &mut FrontDoorState) -> Option<Response<Body>> {
    None
}
