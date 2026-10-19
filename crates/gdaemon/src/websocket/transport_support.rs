//! Free helpers for the native transport: handshake validation, close frames,
//! and the address checks behind the proof and the leg headers.

use std::net::IpAddr;
use std::time::Duration;

use axum::body::Body;
use axum::http::header::{CONNECTION, SEC_WEBSOCKET_KEY, SEC_WEBSOCKET_VERSION, UPGRADE};
use axum::http::{HeaderValue, Request, Response, StatusCode};
use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use futures_util::SinkExt;
use futures_util::stream::SplitSink;
use hyper::upgrade::OnUpgrade;
use hyper_util::rt::TokioIo;
use tokio::time::timeout;
use tokio_tungstenite::WebSocketStream;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::handshake::server::create_response_with_body;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::{CloseFrame, Role};

use super::transport::{Bridge, Public, RESTART};

const PRIVATE_SEND_TIMEOUT: Duration = Duration::from_secs(2);
const CLOSE_TIMEOUT: Duration = Duration::from_secs(1);

/// Validates the upgrade request as tungstenite does, plus the checks it
/// leaves out: no duplicated handshake header, version 13 (426 otherwise), and
/// a key that decodes to 16 bytes. Every other failure is 400.
pub(super) fn handshake(request: &Request<Body>) -> Result<Response<Body>, Box<Response<Body>>> {
    let headers = request.headers();
    let duplicated = [
        UPGRADE,
        CONNECTION,
        SEC_WEBSOCKET_KEY,
        SEC_WEBSOCKET_VERSION,
    ]
    .iter()
    .any(|name| headers.get_all(name).iter().count() > 1);
    if duplicated {
        return Err(Box::new(status_only(StatusCode::BAD_REQUEST)));
    }
    let version = headers
        .get(SEC_WEBSOCKET_VERSION)
        .and_then(|value| value.to_str().ok());
    if version != Some("13") {
        let mut response = status_only(StatusCode::UPGRADE_REQUIRED);
        response
            .headers_mut()
            .insert(SEC_WEBSOCKET_VERSION, HeaderValue::from_static("13"));
        return Err(Box::new(response));
    }
    let key = headers
        .get(SEC_WEBSOCKET_KEY)
        .and_then(|value| STANDARD.decode(value.as_bytes()).ok());
    if key.is_none_or(|key| key.len() != 16) {
        return Err(Box::new(status_only(StatusCode::BAD_REQUEST)));
    }
    create_response_with_body(request, Body::empty)
        .map_err(|_| Box::new(status_only(StatusCode::BAD_REQUEST)))
}

/// Answers a refused browser upgrade: the 101 is already on its way, so the
/// client gets the close frame right after it.
pub(super) fn close_after_upgrade(upgrade: OnUpgrade, frame: CloseFrame) {
    tokio::spawn(async move {
        let Ok(upgraded) = upgrade.await else {
            return;
        };
        let mut socket =
            WebSocketStream::from_raw_socket(TokioIo::new(upgraded), Role::Server, None).await;
        let _ = timeout(CLOSE_TIMEOUT, socket.close(Some(frame))).await;
    });
}

/// Closes a connection that lost its generation before it registered. Both
/// sides close 1012, as if the connection had been retired.
pub(super) async fn close_unregistered(mut sink: SplitSink<Public, Message>, mut leg: Bridge) {
    let frame = close_frame(CloseCode::Restart, RESTART);
    let _ = sink.send(Message::Close(Some(frame.clone()))).await;
    let _ = sink.close().await;
    let _ = timeout(CLOSE_TIMEOUT, leg.close(Some(frame))).await;
}

pub(super) async fn send_leg(leg: &mut SplitSink<Bridge, Message>, message: Message) -> bool {
    matches!(
        timeout(PRIVATE_SEND_TIMEOUT, leg.send(message)).await,
        Ok(Ok(()))
    )
}

pub(super) async fn close_leg(leg: &mut SplitSink<Bridge, Message>, frame: CloseFrame) {
    let _ = timeout(CLOSE_TIMEOUT, leg.send(Message::Close(Some(frame)))).await;
}

pub(super) fn close_frame(code: CloseCode, reason: &'static str) -> CloseFrame {
    CloseFrame {
        code,
        reason: reason.into(),
    }
}

/// A response with no body, for refusals that carry only a status.
pub(super) fn status_only(status: StatusCode) -> Response<Body> {
    let mut response = Response::new(Body::empty());
    *response.status_mut() = status;
    response
}

pub(super) fn is_loopback(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => ip.is_loopback(),
        IpAddr::V6(ip) => {
            ip.is_loopback()
                || ip
                    .to_ipv4_mapped()
                    .is_some_and(|mapped| mapped.is_loopback())
        }
    }
}

/// The IPv4 form of an IPv4-mapped address; other addresses pass unchanged.
pub(super) fn canonical(ip: IpAddr) -> IpAddr {
    match ip {
        IpAddr::V6(ip) => ip.to_ipv4_mapped().map_or(IpAddr::V6(ip), IpAddr::V4),
        IpAddr::V4(ip) => IpAddr::V4(ip),
    }
}

/// Equality that inspects every byte of equal-length inputs.
pub(super) fn same_bytes(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}
