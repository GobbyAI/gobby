//! `gdaemon serve` front door: owns the public ports and proxies to the Python
//! backend on `127.0.0.1:<port + 100>`.

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
    pub target: SocketAddr,
    pub backend_state: BackendState,
    pub client: ProxyClient,
}

impl FrontDoorState {
    pub fn new(target: SocketAddr, backend_state: BackendState) -> Self {
        Self {
            target,
            backend_state,
            client: proxy::client(),
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
        if ws::is_upgrade(request.headers()) {
            ws::splice(&self.state, request).await
        } else {
            self.table.dispatch(&self.state, request).await
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
