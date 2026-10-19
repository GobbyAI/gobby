//! Loopback-only backend bridge. Parsing rejects malformed commands; execution
//! admission (including credentials and sandbox policy) remains on the node.
use super::NativeRoutes;
use crate::{
    front_door::{auth::AuthState, health::json_response},
    node_commands::{
        CONTRACT_VERSION, CommandFrame, ErrReply, ErrorBody, FalseFlag, ReplyFrame, ReplyTag, Stage,
    },
};
use axum::{
    body::{Body, to_bytes},
    http::{Method, Request, Response, StatusCode},
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{net::SocketAddr, time::Duration};
use uuid::Uuid;

const PREFIX: &str = "/api/internal/nodes/";

pub fn is_route(path: &str) -> bool {
    path.starts_with(PREFIX)
}

pub fn not_found() -> Response<Body> {
    json_response(StatusCode::NOT_FOUND, json!({"error":"not_found"}), true)
}

fn invalid_request() -> Response<Body> {
    json_response(
        StatusCode::BAD_REQUEST,
        json!({"error":"invalid_request"}),
        true,
    )
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CommandRequest {
    command: String,
    payload: serde_json::Map<String, Value>,
    #[serde(default = "default_timeout")]
    timeout_ms: u64,
}

fn default_timeout() -> u64 {
    30_000
}

fn failure(request_id: Uuid, code: &str, stage: Option<Stage>) -> ReplyFrame {
    ReplyFrame::Err(ErrReply {
        frame_type: ReplyTag,
        v: CONTRACT_VERSION,
        request_id: request_id.to_string(),
        ok: FalseFlag,
        error: ErrorBody {
            code: code.into(),
            message: code.into(),
            stage,
        },
    })
}

pub async fn handle(
    native: &NativeRoutes,
    auth: &AuthState,
    request: Request<Body>,
    peer: SocketAddr,
) -> Response<Body> {
    // Admission precedes even path and body parsing. Never accept forwarded peer
    // headers, API keys, or a well-typed command as authority for this bridge.
    let NativeRoutes::Hub(nodes) = native else {
        return not_found();
    };
    if !peer.ip().to_canonical().is_loopback()
        || !request
            .headers()
            .get("x-gobby-backend-secret")
            .is_some_and(|value| auth.backend_secret_matches(value.as_bytes()))
    {
        return not_found();
    }
    let path = request.uri().path();
    if request.method() == Method::GET && path == "/api/internal/nodes/connected" {
        return json_response(
            StatusCode::OK,
            json!({"machine_ids":nodes.registry.connected_ids()}),
            true,
        );
    }
    let Some(machine) = path
        .strip_prefix(PREFIX)
        .and_then(|path| path.strip_suffix("/commands"))
    else {
        return not_found();
    };
    if request.method() != Method::POST {
        return not_found();
    }
    let Ok(machine) = machine.parse::<Uuid>() else {
        return invalid_request();
    };
    let Ok(bytes) = to_bytes(request.into_body(), 2 * 1024 * 1024).await else {
        return invalid_request();
    };
    let Ok(body) = serde_json::from_slice::<CommandRequest>(&bytes) else {
        return invalid_request();
    };
    if !(1..=600_000).contains(&body.timeout_ms) {
        return invalid_request();
    }
    let id = Uuid::new_v4();
    let Ok(frame) = serde_json::from_value::<CommandFrame>(json!({
        "type":"command", "v":CONTRACT_VERSION, "request_id":id,
        "command":body.command, "payload":body.payload,
    })) else {
        return invalid_request();
    };
    let reply = match nodes.registry.command_sender(machine) {
        None => failure(id, "node_offline", Some(Stage::None)),
        Some(sender) => match tokio::time::timeout(
            Duration::from_millis(body.timeout_ms),
            sender.request(id, frame),
        )
        .await
        {
            Ok(Some(reply)) => reply,
            Ok(None) => failure(id, "channel_closed", None),
            Err(_) => failure(id, "node_timeout", None),
        },
    };
    match serde_json::to_value(reply) {
        Ok(reply) => json_response(StatusCode::OK, reply, true),
        Err(error) => {
            eprintln!("could not encode node reply: {error}");
            json_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                json!({"error":"reply_encoding"}),
                true,
            )
        }
    }
}
