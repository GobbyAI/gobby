//! `gdaemon serve` front door against in-process stub backends.

mod common;

use std::convert::Infallible;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};

use bytes::Bytes;
use common::{
    TIMEOUT, frame, head_has, read_exact_into, refused_addr, start_front_door, ws_backend,
    ws_client,
};
use http_body_util::{BodyExt, Full};
use hyper::body::{Frame, Incoming};
use hyper::server::conn::http1 as server_http1;
use hyper::service::service_fn;
use hyper::{Request, Response, StatusCode};
use hyper_util::rt::TokioIo;
use tokio::io::AsyncWriteExt;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, oneshot};

/// A response body fed chunk by chunk from a channel.
struct ChannelBody(mpsc::Receiver<Bytes>);

impl hyper::body::Body for ChannelBody {
    type Data = Bytes;
    type Error = Infallible;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, Infallible>>> {
        self.0
            .poll_recv(cx)
            .map(|chunk| chunk.map(|bytes| Ok(Frame::data(bytes))))
    }
}

struct SeenRequest {
    method: String,
    path_and_query: String,
    headers: hyper::HeaderMap,
    body: Bytes,
}

async fn send(
    front_door: std::net::SocketAddr,
    request: Request<Full<Bytes>>,
) -> Response<Incoming> {
    let stream = TcpStream::connect(front_door)
        .await
        .expect("connect front door");
    let (mut sender, connection) = hyper::client::conn::http1::handshake(TokioIo::new(stream))
        .await
        .expect("client handshake");
    tokio::spawn(connection);
    sender.send_request(request).await.expect("send request")
}

#[tokio::test]
async fn http_passthrough_preserves_headers_and_body() {
    // The backend streams its body in two chunks and holds the second until the client
    // has read the first, so a buffering proxy would stall and time out.
    let (seen_tx, seen_rx) = oneshot::channel::<SeenRequest>();
    let (chunk_tx, chunk_rx) = mpsc::channel::<Bytes>(1);
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind backend");
    let backend = listener.local_addr().expect("backend addr");
    let slot = Arc::new(Mutex::new(Some((seen_tx, chunk_rx))));
    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.expect("accept");
        let service = service_fn(move |request: Request<Incoming>| {
            let (seen_tx, chunk_rx) = slot.lock().expect("slot").take().expect("single request");
            async move {
                let (parts, body) = request.into_parts();
                let body = body.collect().await.expect("request body").to_bytes();
                let _ = seen_tx.send(SeenRequest {
                    method: parts.method.to_string(),
                    path_and_query: parts.uri.to_string(),
                    headers: parts.headers,
                    body,
                });
                let body = ChannelBody(chunk_rx);
                Ok::<_, Infallible>(
                    Response::builder()
                        .status(StatusCode::MULTI_STATUS)
                        .header("x-backend", "python")
                        .header("set-cookie", "a=1")
                        .header("keep-alive", "timeout=5")
                        .body(body)
                        .expect("response"),
                )
            }
        });
        let _ = server_http1::Builder::new()
            .serve_connection(TokioIo::new(stream), service)
            .await;
    });
    let front_door = start_front_door(backend).await;

    let request = Request::post("/api/sessions/list?limit=5&q=a%20b")
        .header("host", "localhost:60887")
        .header("x-custom", "kept")
        .header("authorization", "Bearer token")
        .header("connection", "keep-alive, x-hop")
        .header("x-hop", "dropped")
        .body(Full::new(Bytes::from_static(b"{\"payload\":true}")))
        .expect("request");
    let response = tokio::time::timeout(TIMEOUT, send(front_door, request))
        .await
        .expect("response timed out");

    let seen = seen_rx.await.expect("backend saw the request");
    assert_eq!(seen.method, "POST");
    assert_eq!(seen.path_and_query, "/api/sessions/list?limit=5&q=a%20b");
    assert_eq!(seen.headers["host"], "localhost:60887");
    assert_eq!(seen.headers["x-custom"], "kept");
    assert_eq!(seen.headers["authorization"], "Bearer token");
    assert!(!seen.headers.contains_key("x-hop"));
    assert_eq!(seen.body, Bytes::from_static(b"{\"payload\":true}"));

    assert_eq!(response.status(), StatusCode::MULTI_STATUS);
    assert_eq!(response.headers()["x-backend"], "python");
    assert_eq!(response.headers()["set-cookie"], "a=1");
    assert!(!response.headers().contains_key("keep-alive"));

    let mut body = response.into_body();
    chunk_tx
        .send(Bytes::from_static(b"first "))
        .await
        .expect("chunk 1");
    let first = tokio::time::timeout(TIMEOUT, body.frame())
        .await
        .expect("first chunk was not streamed")
        .expect("frame")
        .expect("frame ok")
        .into_data()
        .expect("data frame");
    assert_eq!(first, Bytes::from_static(b"first "));
    chunk_tx
        .send(Bytes::from_static(b"second"))
        .await
        .expect("chunk 2");
    drop(chunk_tx);
    let rest = tokio::time::timeout(TIMEOUT, body.collect())
        .await
        .expect("rest of body timed out")
        .expect("rest of body")
        .to_bytes();
    assert_eq!(rest, Bytes::from_static(b"second"));
}

#[tokio::test]
async fn ws_splice_preserves_deflate_and_close() {
    const TEXT_COMPRESSED: u8 = 0xC1; // FIN + RSV1 (permessage-deflate) + text
    const PING: u8 = 0x89;
    const PONG: u8 = 0x8A;
    const CLOSE: u8 = 0x88;
    let deflated = [0xf2, 0x48, 0xcd, 0xc9, 0xc9, 0x07, 0x00];
    let close_payload = [&4001_u16.to_be_bytes()[..], b"bye"].concat();

    let client_frames = [
        frame(TEXT_COMPRESSED, &deflated, true),
        frame(PING, b"p", true),
        frame(CLOSE, &close_payload, true),
    ]
    .concat();
    let backend_frames = [
        frame(TEXT_COMPRESSED, &deflated, false),
        frame(PONG, b"p", false),
        frame(CLOSE, &close_payload, false),
    ]
    .concat();

    let (backend, captured) = ws_backend(client_frames.len(), backend_frames.clone()).await;
    let front_door = start_front_door(backend).await;
    let (mut stream, head, mut received) = ws_client(front_door, "/ws").await;

    assert!(head.starts_with("HTTP/1.1 101"), "{head}");
    assert!(
        head_has(&head, "sec-websocket-extensions: permessage-deflate"),
        "{head}"
    );
    stream.write_all(&client_frames).await.expect("send frames");
    tokio::time::timeout(
        TIMEOUT,
        read_exact_into(&mut stream, &mut received, backend_frames.len()),
    )
    .await
    .expect("backend frames timed out");
    let capture = tokio::time::timeout(TIMEOUT, captured)
        .await
        .expect("capture timed out")
        .expect("capture");

    assert!(
        head_has(
            &capture.request_head,
            "sec-websocket-extensions: permessage-deflate"
        ),
        "{}",
        capture.request_head
    );
    assert!(head_has(&capture.request_head, "upgrade: websocket"));
    assert_eq!(capture.received, client_frames);
    assert_eq!(received, backend_frames);
}

#[tokio::test]
async fn refused_backend_returns_typed_503() {
    let backend = refused_addr().await;
    let front_door = start_front_door(backend).await;

    let request = Request::get("/api/sessions")
        .header("host", "localhost")
        .body(Full::new(Bytes::new()))
        .expect("request");
    let response = tokio::time::timeout(TIMEOUT, send(front_door, request))
        .await
        .expect("response timed out");

    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(response.headers()["retry-after"], "1");
    let body = response
        .into_body()
        .collect()
        .await
        .expect("body")
        .to_bytes();
    let body: serde_json::Value = serde_json::from_slice(&body).expect("json body");
    assert_eq!(
        body,
        serde_json::json!({
            "status": "unavailable",
            "backend": {"state": "down", "target": backend.to_string()},
        })
    );
}
