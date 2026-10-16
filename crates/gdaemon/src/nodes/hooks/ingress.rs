//! Node-local hook ingress and durable forwarding.

use super::{ledger::Ledger, receipts};
use crate::state::NodeServices;
use axum::{
    body::{Body, Bytes, to_bytes},
    http::{Request, Response, StatusCode},
};
use futures_util::Stream;
use serde_json::{Value, json};
use std::{
    convert::Infallible,
    error::Error as _,
    pin::Pin,
    sync::Arc,
    task::{Context, Poll},
    time::Duration,
};
use tokio::{sync::oneshot, task::JoinHandle};

// Hook envelopes include tool output. Bound pre-auth allocation and upload time.
const MAX_HOOK_BODY_BYTES: usize = 16 * 1024 * 1024;
const HOOK_BODY_READ_TIMEOUT: Duration = Duration::from_secs(10);

/// Buffered bytes for HTTP ingress or the spool drain. Only HTTP releases receipts.
pub struct HubOutcome {
    pub response: Response<Bytes>,
    receipt: Option<Value>,
}

impl HubOutcome {
    fn new(response: Response<Bytes>) -> Self {
        let receipt = serde_json::from_slice::<Value>(response.body())
            .ok()
            .and_then(|value| value.get("_gobby_delivery_receipt").cloned())
            .filter(Value::is_object);
        Self { response, receipt }
    }

    fn into_http(self, node: Arc<NodeServices>) -> Response<Body> {
        let (parts, bytes) = self.response.into_parts();
        let body = match self.receipt {
            Some(receipt) => Body::from_stream(DeliveryBody {
                bytes: Some(bytes),
                receipt: Some(receipt),
                node,
            }),
            None => Body::from(bytes),
        };
        Response::from_parts(parts, body)
    }
}

pub async fn execute(
    node: Arc<NodeServices>,
    ledger: Ledger,
    request: Request<Body>,
) -> Response<Body> {
    let (parts, body) = request.into_parts();
    let body =
        match tokio::time::timeout(HOOK_BODY_READ_TIMEOUT, to_bytes(body, MAX_HOOK_BODY_BYTES))
            .await
        {
            Ok(Ok(body)) => body,
            Ok(Err(error)) => {
                let status = if error
                    .source()
                    .is_some_and(|source| source.is::<http_body_util::LengthLimitError>())
                {
                    StatusCode::PAYLOAD_TOO_LARGE
                } else {
                    StatusCode::BAD_REQUEST
                };
                return failure(status, error).map(Body::from);
            }
            Err(error) => return failure(StatusCode::REQUEST_TIMEOUT, error).map(Body::from),
        };
    let payload = serde_json::from_slice::<Value>(&body).unwrap_or(Value::Null);
    let id = parts
        .headers
        .get("x-gobby-envelope-id")
        .and_then(|value| value.to_str().ok())
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_owned);
    let request = Request::from_parts(parts, Body::from(body));
    if payload["response_capability"] != "hook-response.v1" || id.is_none() {
        return node.relay.forward(request).await;
    }
    let (sender, receiver) = oneshot::channel();
    // The forwarding task owns the claim even if axum drops this handler.
    tokio::spawn(async move {
        let outcome =
            forward_claimed(&node, &ledger, id.as_deref().unwrap_or_default(), request).await;
        // A failed send drops the guarded body and releases its receipt, just as
        // dropping a response before its last frame does after a successful send.
        let _ = sender.send(outcome.into_http(node));
    });
    receiver
        .await
        .unwrap_or_else(|error| failure(StatusCode::BAD_GATEWAY, error).map(Body::from))
}

pub async fn forward_claimed(
    node: &NodeServices,
    ledger: &Ledger,
    envelope_id: &str,
    request: Request<Body>,
) -> HubOutcome {
    let response = forward(node, ledger, envelope_id, request)
        .await
        .unwrap_or_else(|error| {
            eprintln!("node hook {envelope_id} forwarding failed: {error:#}");
            json_response(StatusCode::SERVICE_UNAVAILABLE, json!({"status":"retry"}))
        });
    HubOutcome::new(response)
}

async fn forward(
    node: &NodeServices,
    ledger: &Ledger,
    envelope_id: &str,
    request: Request<Body>,
) -> anyhow::Result<Response<Bytes>> {
    let token = match claim_or_replay(ledger, envelope_id).await? {
        Claim::Owned(token) => token,
        Claim::Answer(response) => return Ok(response),
    };
    let _renewal = Renewal::start(ledger.clone(), envelope_id.to_owned(), token.clone());
    let result = async {
        let (parts, body) = request.into_parts();
        let body = to_bytes(body, MAX_HOOK_BODY_BYTES).await?;
        let payload: Value = serde_json::from_slice(&body)?;
        receipts::sweep_receipts(node, ledger).await?;
        let (parts, body) = tokio::time::timeout(Duration::from_secs(120), async {
            let response = node
                .relay
                .forward(Request::from_parts(parts, Body::from(body)))
                .await;
            let (parts, body) = response.into_parts();
            Ok::<_, axum::Error>((parts, to_bytes(body, usize::MAX).await?))
        })
        .await??;
        let response_json = serde_json::from_slice::<Value>(&body).ok();
        let hook = payload["hook_type"].as_str();
        if parts.status.is_success() {
            ledger
                .finalize(envelope_id, &token, response_json, hook)
                .await?;
            if matches!(hook, Some("session_start" | "SessionStart")) {
                ledger.bump_stop_epoch(None).await?;
            }
        } else if parts.status == StatusCode::CONFLICT
            && response_json
                .as_ref()
                .is_some_and(|value| value["status"] == "processed")
        {
            ledger.finalize(envelope_id, &token, None, hook).await?;
        } else {
            ledger.release(envelope_id, &token).await?;
        }
        Ok(Response::from_parts(parts, body))
    }
    .await;
    if result.is_err() {
        ledger.release(envelope_id, &token).await?;
    }
    result
}

enum Claim {
    Owned(String),
    Answer(Response<Bytes>),
}

async fn claim_or_replay(ledger: &Ledger, id: &str) -> std::io::Result<Claim> {
    if let Some(token) = ledger.claim(id).await? {
        return Ok(Claim::Owned(token));
    }
    if let Some(response) = ledger.terminal_response(id, None).await? {
        return Ok(Claim::Answer(json_response(StatusCode::OK, response)));
    }
    let marker = ledger.marker(id).await?;
    if (marker.is_none() || ledger.clear_stale(id, None).await?)
        && let Some(token) = ledger.claim(id).await?
    {
        return Ok(Claim::Owned(token));
    }
    let status = marker.as_ref().and_then(|value| value["status"].as_str());
    let (status, reason) = match status {
        None => ("malformed_marker", "duplicate envelope marker malformed"),
        Some("processing") => ("processing", "duplicate envelope already processing"),
        Some(status) => (status, "duplicate envelope previously processed"),
    };
    Ok(Claim::Answer(json_response(
        StatusCode::CONFLICT,
        json!({"status":status, "reason":reason}),
    )))
}

struct Renewal(JoinHandle<()>);

impl Renewal {
    fn start(ledger: Ledger, id: String, token: String) -> Self {
        Self(tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_secs(5));
            loop {
                interval.tick().await;
                match ledger.renew(&id, &token).await {
                    Ok(true) => (),
                    Ok(false) => break,
                    Err(error) => {
                        eprintln!("node hook {id} lease renewal failed: {error:#}");
                        break;
                    }
                }
            }
        }))
    }
}

impl Drop for Renewal {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// One buffered data frame: before it is yielded, cancellation loses delivery.
struct DeliveryBody {
    bytes: Option<Bytes>,
    receipt: Option<Value>,
    node: Arc<NodeServices>,
}

impl Stream for DeliveryBody {
    type Item = Result<Bytes, Infallible>;

    fn poll_next(mut self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let bytes = self.bytes.take();
        self.receipt.take();
        Poll::Ready(bytes.map(Ok))
    }
}

impl Drop for DeliveryBody {
    fn drop(&mut self) {
        if let Some(receipt) = self.receipt.take() {
            let node = self.node.clone();
            tokio::spawn(async move {
                if let Err(error) = receipts::release_receipt(&node, &receipt).await {
                    eprintln!("node hook undelivered receipt release failed: {error:#}");
                }
            });
        }
    }
}

fn json_response(status: StatusCode, value: Value) -> Response<Bytes> {
    let mut response = Response::new(Bytes::from(value.to_string()));
    *response.status_mut() = status;
    response.headers_mut().insert(
        axum::http::header::CONTENT_TYPE,
        axum::http::HeaderValue::from_static("application/json"),
    );
    response
}

fn failure(status: StatusCode, error: impl std::fmt::Display) -> Response<Bytes> {
    let mut response = Response::new(Bytes::from(error.to_string()));
    *response.status_mut() = status;
    response
}
