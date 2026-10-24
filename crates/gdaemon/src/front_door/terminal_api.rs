//! The loopback write route's door: `POST /api/internal/terminals/write`.
//! Loopback peers presenting the current backend token reach the terminal
//! route the family published; every other request is refused here.

use std::net::SocketAddr;
use std::sync::PoisonError;

use axum::body::{Body, to_bytes};
use axum::http::{Request, Response, StatusCode};
use gobby_terminals::write_api::{MAX_ROUTE_BODY, WriteRefused};

use super::health::json_response;
use crate::state::RuntimeState;

pub const PATH: &str = "/api/internal/terminals/write";
const TOKEN_HEADER: &str = "x-gobby-write-token";

pub async fn handle(
    runtime: &RuntimeState,
    peer: SocketAddr,
    request: Request<Body>,
) -> Response<Body> {
    // The loopback predicate is the one the plaintext listener already applies.
    if !crate::serve::plaintext_allowed(peer.ip()) {
        return refusal(
            StatusCode::FORBIDDEN,
            "not_loopback",
            "the write route accepts loopback peers only",
        );
    }
    let presented = request
        .headers()
        .get(TOKEN_HEADER)
        .and_then(|value| value.to_str().ok());
    let token = match runtime.write_token.admit(presented) {
        Ok(token) => token,
        Err(error) => {
            let (status, code) = match error {
                WriteRefused::NoBackend => (StatusCode::SERVICE_UNAVAILABLE, "no_backend"),
                WriteRefused::MissingToken => (StatusCode::UNAUTHORIZED, "missing_token"),
                WriteRefused::WrongToken => (StatusCode::UNAUTHORIZED, "wrong_token"),
            };
            return refusal(status, code, &error.to_string());
        }
    };
    let Ok(body) = to_bytes(request.into_body(), MAX_ROUTE_BODY).await else {
        return refusal(
            StatusCode::PAYLOAD_TOO_LARGE,
            "request_too_large",
            "the body exceeds the route's bound",
        );
    };
    // A token retired while the body arrived never reaches the route.
    if !runtime.write_token.is_current(&token) {
        return refusal(
            StatusCode::UNAUTHORIZED,
            "retired_token",
            "the write token was retired",
        );
    }
    let route = runtime
        .terminal_route
        .read()
        .unwrap_or_else(PoisonError::into_inner)
        .clone();
    let Some(route) = route else {
        return refusal(
            StatusCode::SERVICE_UNAVAILABLE,
            "route_unavailable",
            "the terminal write route is not published",
        );
    };
    let Ok(call) = serde_json::from_slice::<serde_json::Value>(&body) else {
        return refusal(
            StatusCode::BAD_REQUEST,
            "malformed_json",
            "the body is not JSON",
        );
    };
    json_response(StatusCode::OK, route.handle(&token, call).await, false)
}

fn refusal(status: StatusCode, code: &str, detail: &str) -> Response<Body> {
    let retry = status == StatusCode::SERVICE_UNAVAILABLE;
    json_response(
        status,
        serde_json::json!({"status": "refusal", "stage": "none", "code": code, "detail": detail}),
        retry,
    )
}
