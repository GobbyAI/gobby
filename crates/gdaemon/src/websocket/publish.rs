//! The private bind and publish endpoints the Python bridge calls. A request
//! proves itself before its body is read, and it either applies completely or
//! not at all.

use std::net::SocketAddr;

use axum::body::Body;
use axum::http::header::{CONTENT_LENGTH, CONTENT_TYPE};
use axum::http::{HeaderMap, HeaderValue, Method, Request, Response, StatusCode};
use http_body_util::{BodyExt, LengthLimitError, Limited};
use serde::Deserialize;
use serde::de::DeserializeOwned;
use serde_json::{Map, Value, json};

use super::broker::{Batch, ConnectionId, MAX_QUEUED_BYTES};
use super::transport::{NativeWs, Op, Settings, Site, status_only};

const BIND: &str = "/_gobby/ws/bind";
const PUBLISH: &str = "/_gobby/ws/publish";
/// A bind body is a handful of integers; anything larger is refused unread.
const BIND_LIMIT: usize = 4 * 1024;
/// The largest publish body the bridge may send.
const PUBLISH_LIMIT: usize = 128 * 1024 * 1024;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PublishRequest {
    generation: String,
    ops: Vec<WireOp>,
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "lowercase")]
enum WireOp {
    Publish {
        filter: Map<String, Value>,
        frames: Vec<String>,
    },
    Subscribe {
        leg: u64,
        events: Vec<String>,
    },
}

/// Answers one private request. Any path other than the two endpoints, and any
/// method other than POST, is 404. Every refusal is decided before the request
/// changes state.
pub async fn handle(native: &NativeWs, request: Request<Body>, peer: SocketAddr) -> Response<Body> {
    let (parts, body) = request.into_parts();
    let path = parts.uri.path();
    if (path != BIND && path != PUBLISH) || parts.method != Method::POST {
        return status_only(StatusCode::NOT_FOUND);
    }
    let Some(token) = native.prove(&parts.headers, peer) else {
        return status_only(StatusCode::FORBIDDEN);
    };
    if path == BIND {
        bind(native, &token, &parts.headers, body).await
    } else {
        publish(native, &token, &parts.headers, body).await
    }
}

async fn bind(native: &NativeWs, token: &str, headers: &HeaderMap, body: Body) -> Response<Body> {
    let body = match read_bounded(headers, body, BIND_LIMIT).await {
        Ok(body) => body,
        Err(refusal) => return refusal,
    };
    let Some(settings) = parse_object::<Settings>(&body) else {
        return status_only(StatusCode::BAD_REQUEST);
    };
    let generation = format!("{:032x}", rand::random::<u128>());
    native.pass(Site::Bind).await;
    match native.commit_bind(token, settings, generation.clone()) {
        Ok(()) => json_response(&json!({"generation": generation})),
        Err(refusal) => status_only(refusal.status()),
    }
}

async fn publish(
    native: &NativeWs,
    token: &str,
    headers: &HeaderMap,
    body: Body,
) -> Response<Body> {
    let body = match read_bounded(headers, body, PUBLISH_LIMIT).await {
        Ok(body) => body,
        Err(refusal) => return refusal,
    };
    let Some(request) = parse_object::<PublishRequest>(&body) else {
        return status_only(StatusCode::BAD_REQUEST);
    };
    // A stale generation has no bound of its own; the commit refuses it with 409.
    let limit = native
        .frame_limit(&request.generation)
        .unwrap_or(MAX_QUEUED_BYTES);
    let mut ops = Vec::with_capacity(request.ops.len());
    for op in request.ops {
        match op {
            WireOp::Publish { filter, frames } => {
                if frames.iter().any(|frame| frame.len() > limit) {
                    return status_only(StatusCode::PAYLOAD_TOO_LARGE);
                }
                let Ok(batch) = Batch::new(frames) else {
                    return status_only(StatusCode::PAYLOAD_TOO_LARGE);
                };
                ops.push(Op::Publish {
                    filter: Value::Object(filter),
                    batch,
                });
            }
            WireOp::Subscribe { leg, events } => ops.push(Op::Subscribe {
                leg: ConnectionId::new(leg),
                events,
            }),
        }
    }
    native.pass(Site::Publish).await;
    match native.commit_publish(token, &request.generation, ops) {
        Ok(()) => status_only(StatusCode::NO_CONTENT),
        Err(refusal) => status_only(refusal.status()),
    }
}

/// Parses one JSON object into `T`. Serde also reads a struct from a sequence,
/// in field order, so the body must open with `{` before any field is read.
fn parse_object<T: DeserializeOwned>(body: &[u8]) -> Option<T> {
    if body.trim_ascii_start().first() != Some(&b'{') {
        return None;
    }
    serde_json::from_slice(body).ok()
}

/// Reads at most `limit` bytes. A declared length over the limit is refused
/// before any byte is read, and a body that grows past it is refused too.
async fn read_bounded(
    headers: &HeaderMap,
    body: Body,
    limit: usize,
) -> Result<Vec<u8>, Response<Body>> {
    let declared = headers
        .get(CONTENT_LENGTH)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<usize>().ok());
    if declared.is_some_and(|length| length > limit) {
        return Err(status_only(StatusCode::PAYLOAD_TOO_LARGE));
    }
    match Limited::new(body, limit).collect().await {
        Ok(collected) => Ok(collected.to_bytes().to_vec()),
        Err(error) if error.is::<LengthLimitError>() => {
            Err(status_only(StatusCode::PAYLOAD_TOO_LARGE))
        }
        Err(_) => Err(status_only(StatusCode::BAD_REQUEST)),
    }
}

fn json_response(body: &Value) -> Response<Body> {
    let mut response = Response::new(Body::from(body.to_string()));
    response
        .headers_mut()
        .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    response
}
