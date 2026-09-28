//! `gdaemon serve` front door: owns the public ports and proxies to the Python
//! backend on `127.0.0.1:<port + 100>`.

pub mod health;
pub mod proxy;
pub mod routes;
pub mod ws;

use std::net::SocketAddr;
use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, Response};

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

    pub async fn handle(&self, request: Request<Body>) -> Response<Body> {
        if ws::is_upgrade(request.headers()) {
            ws::splice(&self.state, request).await
        } else {
            self.table.dispatch(&self.state, request).await
        }
    }
}
