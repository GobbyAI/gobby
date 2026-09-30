//! WebSocket (and any other HTTP/1.1 upgrade) splice to the Python backend.

use axum::body::Body;
use axum::http::header::{CONNECTION, UPGRADE};
use axum::http::{HeaderMap, Request, Response, StatusCode, Uri};
use hyper::client::conn::http1;
use hyper_util::rt::TokioIo;
use tokio::net::TcpStream;

use super::FrontDoorState;
use super::health::{bad_gateway, unavailable};
use super::proxy::filter_response;

/// True when the request asks to switch protocols (`Connection: upgrade` plus `Upgrade`).
pub fn is_upgrade(headers: &HeaderMap) -> bool {
    headers.contains_key(UPGRADE)
        && headers
            .get_all(CONNECTION)
            .iter()
            .filter_map(|value| value.to_str().ok())
            .flat_map(|value| value.split(','))
            .any(|token| token.trim().eq_ignore_ascii_case("upgrade"))
}

/// Forward the upgrade request on a dedicated backend connection. When the backend
/// answers 101, both legs are upgraded and bytes are copied verbatim until either
/// side closes, so extensions, ping/pong, close codes and backpressure pass through.
pub async fn splice(state: &FrontDoorState, mut request: Request<Body>) -> Response<Body> {
    let target = state.target;
    let client_upgrade = hyper::upgrade::on(&mut request);

    let stream = match TcpStream::connect(target).await {
        Ok(stream) => stream,
        Err(_) => return unavailable(target, state.backend_state),
    };
    let (mut sender, connection) = match http1::handshake(TokioIo::new(stream)).await {
        Ok(pair) => pair,
        Err(error) => return bad_gateway(target, error),
    };
    tokio::spawn(connection.with_upgrades());

    let (mut parts, body) = request.into_parts();
    if let Some(path_and_query) = parts.uri.path_and_query() {
        parts.uri = Uri::from(path_and_query.clone());
    }
    let mut response = match sender.send_request(Request::from_parts(parts, body)).await {
        Ok(response) => response,
        Err(error) => return bad_gateway(target, error),
    };
    if response.status() != StatusCode::SWITCHING_PROTOCOLS {
        // A refused upgrade is an ordinary response; the 101 handshake keeps its headers.
        return filter_response(response);
    }

    let backend_upgrade = hyper::upgrade::on(&mut response);
    tokio::spawn(async move {
        let (Ok(client), Ok(backend)) = tokio::join!(client_upgrade, backend_upgrade) else {
            return;
        };
        let mut client = TokioIo::new(client);
        let mut backend = TokioIo::new(backend);
        // Either leg ending, cleanly or by reset, ends the splice; there is no one to report to.
        let _ = tokio::io::copy_bidirectional(&mut client, &mut backend).await;
    });
    response.map(Body::new)
}
