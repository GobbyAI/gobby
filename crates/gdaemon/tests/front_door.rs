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
use tokio::io::{AsyncReadExt, AsyncWriteExt};
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

/// Read one chunked HTTP/1.1 message, head through the trailer section.
async fn read_chunked_message(stream: &mut TcpStream) -> String {
    let mut buffer = Vec::new();
    loop {
        let text = String::from_utf8_lossy(&buffer);
        if let Some(last_chunk) = text.find("\r\n0\r\n")
            && text[last_chunk + 3..].contains("\r\n\r\n")
        {
            return text.into_owned();
        }
        let mut chunk = [0_u8; 1024];
        let read = stream.read(&mut chunk).await.expect("read message");
        assert_ne!(read, 0, "closed mid-message: {text}");
        buffer.extend_from_slice(&chunk[..read]);
    }
}

/// The trailer section: everything after the last-chunk line.
fn trailer_section(message: &str) -> String {
    let last_chunk = message.find("\r\n0\r\n").expect("last chunk");
    message[last_chunk + 5..].to_ascii_lowercase()
}

#[tokio::test]
async fn connection_listed_trailers_are_stripped_both_ways() {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind backend");
    let backend = listener.local_addr().expect("backend addr");
    let (seen_tx, seen_rx) = oneshot::channel::<String>();
    tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.expect("accept");
        let request = read_chunked_message(&mut stream).await;
        stream
            .write_all(
                b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\nconnection: x-private\r\n\
                  trailer: x-private, x-kept\r\n\r\n5\r\nhello\r\n0\r\n\
                  x-private: secret\r\nx-kept: yes\r\n\r\n",
            )
            .await
            .expect("write response");
        let _ = seen_tx.send(request);
    });
    let front_door = start_front_door(backend).await;

    let mut client = TcpStream::connect(front_door)
        .await
        .expect("connect front door");
    client
        .write_all(
            b"POST /api/upload HTTP/1.1\r\nhost: localhost\r\ntransfer-encoding: chunked\r\n\
              connection: x-private\r\nte: trailers\r\ntrailer: x-private, x-kept\r\n\r\n\
              5\r\nhello\r\n0\r\nx-private: secret\r\nx-kept: yes\r\n\r\n",
        )
        .await
        .expect("write request");
    let response = tokio::time::timeout(TIMEOUT, read_chunked_message(&mut client))
        .await
        .expect("response timed out");
    let request = tokio::time::timeout(TIMEOUT, seen_rx)
        .await
        .expect("backend capture timed out")
        .expect("backend capture");

    let request_trailers = trailer_section(&request);
    assert!(request_trailers.contains("x-kept: yes"), "{request}");
    assert!(!request_trailers.contains("x-private"), "{request}");
    let response_trailers = trailer_section(&response);
    assert!(response_trailers.contains("x-kept: yes"), "{response}");
    assert!(!response_trailers.contains("x-private"), "{response}");
}

#[tokio::test]
async fn refused_upgrade_strips_connection_listed_fields() {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind backend");
    let backend = listener.local_addr().expect("backend addr");
    tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.expect("accept");
        let mut head = Vec::new();
        while !head.windows(4).any(|window| window == b"\r\n\r\n") {
            let mut chunk = [0_u8; 1024];
            let read = stream.read(&mut chunk).await.expect("read upgrade");
            assert_ne!(read, 0, "closed before the end of the upgrade head");
            head.extend_from_slice(&chunk[..read]);
        }
        stream
            .write_all(
                b"HTTP/1.1 403 Forbidden\r\nconnection: x-private\r\nx-private: secret\r\n\
                  x-kept: yes\r\ncontent-length: 0\r\n\r\n",
            )
            .await
            .expect("write refusal");
    });
    let front_door = start_front_door(backend).await;

    let (_stream, head, _) = tokio::time::timeout(TIMEOUT, ws_client(front_door, "/ws"))
        .await
        .expect("refusal timed out");

    assert!(head.starts_with("HTTP/1.1 403"), "{head}");
    assert!(head_has(&head, "x-kept: yes"), "{head}");
    assert!(!head.to_ascii_lowercase().contains("x-private"), "{head}");
}

/// A `gdaemon serve` child and its home, killed on drop so a failed assertion
/// leaks no process.
#[cfg(unix)]
struct ServeChild {
    process: std::process::Child,
    _home: tempfile::TempDir,
}

#[cfg(unix)]
impl Drop for ServeChild {
    fn drop(&mut self) {
        let _ = self.process.kill();
        let _ = self.process.wait();
    }
}

/// Spawn `gdaemon serve` on a fresh public pair with the pipe's read end as stdin,
/// and wait until both ports accept.
#[cfg(unix)]
fn spawn_serve(parent_fd: Option<&str>, reader: std::io::PipeReader) -> (ServeChild, [u16; 2]) {
    use std::process::{Command, Stdio};
    use std::time::{Duration, Instant};

    let mut ports = Vec::new();
    while ports.len() < 2 {
        let probe = std::net::TcpListener::bind("127.0.0.1:0").expect("bind probe");
        let port = probe.local_addr().expect("probe addr").port();
        // Leave room for the +100 backend pair gdaemon derives.
        if port <= 65435 && !ports.contains(&port) {
            ports.push(port);
        }
    }
    let ports = [ports[0], ports[1]];
    let home = tempfile::tempdir().expect("tempdir");
    std::fs::write(
        home.path().join("bootstrap.yaml"),
        format!(
            "bind_host: 127.0.0.1\ndaemon_port: {}\nwebsocket_port: {}\n",
            ports[0], ports[1]
        ),
    )
    .expect("write bootstrap");
    let mut command = Command::new(env!("CARGO_BIN_EXE_gdaemon"));
    command
        .arg("serve")
        .env("GOBBY_HOME", home.path())
        .env_remove("GOBBY_PARENT_FD")
        .stdin(Stdio::from(reader));
    if let Some(fd) = parent_fd {
        command.env("GOBBY_PARENT_FD", fd);
    }
    let mut child = ServeChild {
        process: command.spawn().expect("spawn gdaemon serve"),
        _home: home,
    };
    let deadline = Instant::now() + TIMEOUT;
    while !ports
        .iter()
        .all(|port| std::net::TcpStream::connect(("127.0.0.1", *port)).is_ok())
    {
        assert!(
            child.process.try_wait().expect("poll serve").is_none(),
            "serve exited before binding"
        );
        assert!(Instant::now() < deadline, "serve did not bind {ports:?}");
        std::thread::sleep(Duration::from_millis(50));
    }
    (child, ports)
}

#[cfg(unix)]
#[test]
fn parent_fd_eof_stops_serve() {
    use std::time::{Duration, Instant};

    // Without GOBBY_PARENT_FD there is no watch: EOF on the same pipe changes nothing.
    let (reader, writer) = std::io::pipe().expect("pipe");
    let (mut unwatched, ports) = spawn_serve(None, reader);
    drop(writer);
    std::thread::sleep(Duration::from_millis(500));
    assert!(
        unwatched.process.try_wait().expect("poll serve").is_none(),
        "serve without GOBBY_PARENT_FD must keep running"
    );
    assert!(std::net::TcpStream::connect(("127.0.0.1", ports[0])).is_ok());
    drop(unwatched);

    // With it, the owner's end closing stops serve cleanly and frees both public ports.
    let (reader, writer) = std::io::pipe().expect("pipe");
    let (mut watched, ports) = spawn_serve(Some("0"), reader);
    drop(writer);
    let deadline = Instant::now() + TIMEOUT;
    let status = loop {
        if let Some(status) = watched.process.try_wait().expect("poll serve") {
            break status;
        }
        assert!(Instant::now() < deadline, "serve ignored parent EOF");
        std::thread::sleep(Duration::from_millis(50));
    };
    assert!(status.success(), "serve exited with {status}");
    for port in ports {
        assert!(
            std::net::TcpListener::bind(("127.0.0.1", port)).is_ok(),
            "public port {port} still held after serve exited"
        );
    }
}
