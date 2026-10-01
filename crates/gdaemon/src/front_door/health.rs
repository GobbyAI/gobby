//! Typed backend unavailability and the native `health` route family.

use std::error::Error;
use std::net::SocketAddr;

use axum::Router;
use axum::body::Body;
use axum::extract::{Request, State};
use axum::http::header::{CONTENT_TYPE, RETRY_AFTER};
use axum::http::{HeaderValue, Response, StatusCode};
use axum::routing::get;
use serde_json::json;

use super::FrontDoorState;
use super::proxy;
use super::routes::RouteFamily;

/// Response header naming the gdaemon handler that answered a native route.
pub const SERVED_BY_HEADER: &str = "x-gobby-served-by";

/// What the front door knows about the Python backend when it refuses connections.
/// `Starting` needs the 5.2 supervisor; until then the backend is always `Down`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BackendState {
    Down,
    Starting,
}

impl BackendState {
    pub fn as_str(self) -> &'static str {
        match self {
            BackendState::Down => "down",
            BackendState::Starting => "starting",
        }
    }
}

/// The typed 503 returned while the backend at `target` refuses connections.
pub fn unavailable(target: SocketAddr, state: BackendState) -> Response<Body> {
    let body = json!({
        "status": "unavailable",
        "backend": {"state": state.as_str(), "target": target.to_string()},
    });
    json_response(StatusCode::SERVICE_UNAVAILABLE, body, true)
}

/// The 502 returned when the backend accepted the connection but answered malformed.
/// `error` carries its whole source chain, so a hyper send failure names its cause.
pub fn bad_gateway(target: SocketAddr, error: impl Error) -> Response<Body> {
    let mut message = error.to_string();
    let mut source = error.source();
    while let Some(cause) = source {
        message.push_str(": ");
        message.push_str(&cause.to_string());
        source = cause.source();
    }
    let body = json!({
        "status": "bad_gateway",
        "backend": {"target": target.to_string()},
        "error": message,
    });
    json_response(StatusCode::BAD_GATEWAY, body, false)
}

fn json_response(status: StatusCode, body: serde_json::Value, retry: bool) -> Response<Body> {
    let mut response = Response::new(Body::from(body.to_string()));
    *response.status_mut() = status;
    let headers = response.headers_mut();
    headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    if retry {
        headers.insert(RETRY_AFTER, HeaderValue::from_static("1"));
    }
    response
}

/// `/api/health`: the first native family. While Python is down it answers with the
/// typed 503; once Python listens, Python's own health body passes through untouched.
pub struct HealthFamily;

impl RouteFamily for HealthFamily {
    fn name(&self) -> &'static str {
        "health"
    }

    fn prefixes(&self) -> &'static [&'static str] {
        &["/api/health"]
    }

    fn router(&self) -> Router<FrontDoorState> {
        Router::new().route("/api/health", get(native_health))
    }
}

async fn native_health(State(state): State<FrontDoorState>, request: Request) -> Response<Body> {
    let mut response = proxy::forward(&state, request).await;
    response
        .headers_mut()
        .insert(SERVED_BY_HEADER, HeaderValue::from_static("gdaemon"));
    response
}

#[cfg(test)]
mod tests {
    use std::fmt;

    use http_body_util::BodyExt;

    use super::*;

    #[derive(Debug)]
    struct Wrapped(std::io::Error);

    impl fmt::Display for Wrapped {
        fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
            formatter.write_str("client error (SendRequest)")
        }
    }

    impl Error for Wrapped {
        fn source(&self) -> Option<&(dyn Error + 'static)> {
            Some(&self.0)
        }
    }

    #[tokio::test]
    async fn bad_gateway_error_names_its_source_chain() {
        let cause = std::io::Error::other("connection closed before message completed");
        let target = SocketAddr::from(([127, 0, 0, 1], 60987));
        let response = bad_gateway(target, Wrapped(cause));

        assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
        let body = response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes();
        let body: serde_json::Value = serde_json::from_slice(&body).expect("json body");
        assert_eq!(
            body,
            json!({
                "status": "bad_gateway",
                "backend": {"target": "127.0.0.1:60987"},
                "error": "client error (SendRequest): connection closed before message completed",
            })
        );
    }
}
