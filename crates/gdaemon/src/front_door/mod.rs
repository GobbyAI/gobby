//! `gdaemon serve` front door: owns the public ports and proxies to the Python
//! backend on `127.0.0.1:<port + 100>`.

pub mod auth;
pub mod challenge;
pub mod health;
pub mod proxy;
pub mod routes;
pub mod tls;
pub mod ws;

use std::net::SocketAddr;
use std::sync::Arc;

use axum::body::Body;
use axum::http::header::{CONNECTION, HeaderName, HeaderValue};
use axum::http::{HeaderMap, Request, Response};

use health::BackendState;
use proxy::ProxyClient;
use routes::RouteTable;

/// Shared state for one listener: its backend target and the proxy client.
#[derive(Clone)]
pub struct FrontDoorState {
    #[cfg(unix)]
    pub runtime: Option<Arc<crate::state::RuntimeState>>,
    pub target: SocketAddr,
    pub backend_state: BackendState,
    pub client: ProxyClient,
    pub auth: Arc<auth::AuthState>,
}

impl FrontDoorState {
    pub fn new(
        target: SocketAddr,
        backend_state: BackendState,
        auth: Arc<auth::AuthState>,
    ) -> Self {
        Self {
            #[cfg(unix)]
            runtime: None,
            target,
            backend_state,
            client: proxy::client(),
            auth,
        }
    }
}

/// A listener's request handler: upgrades are spliced, everything else is routed.
#[derive(Clone)]
pub struct FrontDoor {
    state: FrontDoorState,
    table: Arc<RouteTable>,
}

impl FrontDoor {
    pub fn new(state: FrontDoorState, table: RouteTable) -> Self {
        Self {
            state,
            table: Arc::new(table),
        }
    }

    /// Serve one request from `peer`, the address the connection was accepted
    /// from; `https` says whether it arrived over TLS.
    pub async fn handle(
        &self,
        mut request: Request<Body>,
        peer: SocketAddr,
        https: bool,
    ) -> Response<Body> {
        observe_peer(request.headers_mut(), peer, https);
        let state = self.state.clone();
        #[cfg(unix)]
        let mut state = state;
        #[cfg(unix)]
        if let Some(runtime) = &state.runtime {
            if crate::lease::admin_api::is_route(request.uri().path()) {
                if let Err(response) = state.auth.authenticate(request.headers_mut()).await {
                    return *response;
                }
                if !request.headers().contains_key("x-gobby-key-id") {
                    return auth::rejection(
                        axum::http::StatusCode::UNAUTHORIZED,
                        "authentication_required",
                        "A valid API key is required for native lease administration",
                    );
                }
                return crate::lease::admin_api::handle(runtime, request).await;
            }
            let backend = runtime.backend.borrow().clone();
            state.backend_state = match &backend {
                crate::lifecycle::backend::BackendStatus::Starting { .. } => BackendState::Starting,
                _ => BackendState::Down,
            };
            let refusal = match backend {
                crate::lifecycle::backend::BackendStatus::Refused { reason } => Some(
                    serde_json::json!({"error": "backend_refused", "status": "unavailable", "backend": {
                        "state": "refused", "refusal": reason, "target": state.target.to_string()
                    }}),
                ),
                _ if runtime.standby() => Some(serde_json::json!({
                    "error": "standby", "status": "unavailable", "state": "standby", "backend": {
                        "state": "standby", "target": state.target.to_string()
                    }
                })),
                _ => None,
            };
            if let Some(body) = refusal {
                return health::json_response(
                    axum::http::StatusCode::SERVICE_UNAVAILABLE,
                    body,
                    true,
                );
            }
        }
        if request.method() == axum::http::Method::POST
            && request.uri().path() == "/api/runtime/handshake/challenge"
        {
            request = match challenge::answer(&self.state.auth.bootstrap_path, request).await {
                Ok(request) => request,
                Err(response) => return *response,
            };
        }
        if let Err(response) = self.state.auth.authenticate(request.headers_mut()).await {
            return *response;
        }
        if ws::is_upgrade(request.headers()) {
            ws::splice(&state, request).await
        } else {
            self.table.dispatch(&state, request).await
        }
    }
}

/// Client-supplied forwarding headers. Each is dropped before any route or
/// splice sees the request, so the backend only learns the observed peer.
const FORWARDING_HEADERS: [&str; 5] = [
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-forwarded-host",
    "x-real-ip",
];

/// Replace every forwarding header with the transport's own view: the peer's
/// IP in `X-Forwarded-For` and the scheme in `X-Forwarded-Proto`. A client
/// `Connection` field naming a forwarding header is dropped from it, or the
/// downstream hop-by-hop strip would delete the observed values.
fn observe_peer(headers: &mut HeaderMap, peer: SocketAddr, https: bool) {
    for name in FORWARDING_HEADERS {
        headers.remove(name);
    }
    let nominations: Vec<String> = headers
        .get_all(CONNECTION)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .map(str::trim)
        .filter(|token| {
            !token.is_empty()
                && !FORWARDING_HEADERS
                    .iter()
                    .any(|n| token.eq_ignore_ascii_case(n))
        })
        .map(str::to_owned)
        .collect();
    headers.remove(CONNECTION);
    if let Ok(value) = HeaderValue::try_from(nominations.join(", "))
        && !nominations.is_empty()
    {
        headers.insert(CONNECTION, value);
    }
    if let Ok(value) = HeaderValue::try_from(peer.ip().to_canonical().to_string()) {
        headers.insert(HeaderName::from_static("x-forwarded-for"), value);
    }
    let scheme = if https { "https" } else { "http" };
    headers.insert(
        HeaderName::from_static("x-forwarded-proto"),
        HeaderValue::from_static(scheme),
    );
}
