//! Interactive nonce proofs are answered before any credential is forwarded.

use super::auth::rejection;
use axum::body::{Body, to_bytes};
use axum::http::{HeaderValue, Request, Response, StatusCode};
use base64::Engine;
use base64::engine::general_purpose::{URL_SAFE, URL_SAFE_NO_PAD};
use gobby_core::bootstrap::parse_hub_database_bootstrap;
use hmac::{Hmac, Mac};
use serde::Deserialize;
use sha2::Sha256;
use std::path::Path;
use std::time::Duration;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ChallengeRequest {
    nonce: String,
    #[serde(default = "interactive")]
    kind: String,
    caller: Option<ChallengeCaller>,
    #[serde(rename = "claims")]
    _claims: Option<serde_json::Map<String, serde_json::Value>>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ChallengeCaller {
    #[serde(default = "interactive")]
    kind: String,
    #[serde(rename = "claims")]
    _claims: Option<serde_json::Map<String, serde_json::Value>>,
}

fn interactive() -> String {
    "interactive".into()
}

pub(super) async fn answer(
    bootstrap: &Path,
    request: Request<Body>,
) -> Result<Request<Body>, Response<Body>> {
    if request.headers().contains_key("authorization") {
        return Err(grant_rejection(
            StatusCode::UNAUTHORIZED,
            "credential_before_proof",
            "no credential attaches before challenge proof",
        ));
    }
    let (parts, body) = request.into_parts();
    // Public proof requests carry only a nonce and caller claims. Bound both
    // allocation and body-read time before examining the unauthenticated JSON.
    let bytes = tokio::time::timeout(Duration::from_secs(10), to_bytes(body, 64 * 1024))
        .await
        .map_err(|_| invalid_request())?
        .map_err(|_| invalid_request())?;
    let body: ChallengeRequest = serde_json::from_slice(&bytes).map_err(|_| invalid_request())?;
    if body.nonce.len() > 44 {
        return Err(invalid_nonce());
    }
    let nonce = URL_SAFE
        .decode(&body.nonce)
        .or_else(|_| URL_SAFE_NO_PAD.decode(&body.nonce))
        .map_err(|_| invalid_nonce())?;
    let kind = body
        .caller
        .as_ref()
        .map_or(body.kind.as_str(), |caller| caller.kind.as_str());
    match kind {
        "managed" => return Ok(Request::from_parts(parts, Body::from(bytes))),
        "interactive" => {}
        kind => {
            return Err(grant_rejection(
                StatusCode::FORBIDDEN,
                "claims_mismatch",
                &format!("unknown challenge kind {kind}"),
            ));
        }
    }
    let contents =
        tokio::time::timeout(Duration::from_secs(5), tokio::fs::read_to_string(bootstrap))
            .await
            .map_err(|_| key_unavailable())?
            .map_err(|_| key_unavailable())?;
    let key = parse_hub_database_bootstrap(&contents)
        .map_err(|_| key_unavailable())?
        .and_then(|bootstrap| bootstrap.api_key)
        .ok_or_else(key_unavailable)?;
    let mut mac = Hmac::<Sha256>::new_from_slice(key.as_bytes()).map_err(|_| key_unavailable())?;
    // Caller-selected nonces must never expose another API-key-derived secret.
    mac.update(b"gobby-interactive-proof-v1");
    let proof_key = mac.finalize().into_bytes();
    let mut mac = Hmac::<Sha256>::new_from_slice(&proof_key).map_err(|_| key_unavailable())?;
    mac.update(&nonce);
    let proof: String = mac
        .finalize()
        .into_bytes()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    let mut response = json_response(StatusCode::OK, serde_json::json!({"proof": proof}));
    response
        .headers_mut()
        .insert("cache-control", HeaderValue::from_static("no-store"));
    Err(response)
}

fn invalid_request() -> Response<Body> {
    json_response(
        StatusCode::BAD_REQUEST,
        serde_json::json!({"detail": "invalid challenge request"}),
    )
}

fn invalid_nonce() -> Response<Body> {
    json_response(
        StatusCode::BAD_REQUEST,
        serde_json::json!({"detail": "invalid nonce"}),
    )
}

fn key_unavailable() -> Response<Body> {
    rejection(
        StatusCode::SERVICE_UNAVAILABLE,
        "key_resolver_unavailable",
        "API key resolver unavailable",
    )
}

fn grant_rejection(status: StatusCode, code: &str, message: &str) -> Response<Body> {
    json_response(
        status,
        serde_json::json!({"code": code, "error": code, "message": message}),
    )
}

fn json_response(status: StatusCode, body: serde_json::Value) -> Response<Body> {
    let mut response = Response::new(Body::from(body.to_string()));
    *response.status_mut() = status;
    response
        .headers_mut()
        .insert("content-type", HeaderValue::from_static("application/json"));
    response
}
