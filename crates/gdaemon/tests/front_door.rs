//! `gdaemon serve` front door against in-process stub backends.

mod common;

use std::convert::Infallible;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};
use std::time::Duration;

use std::collections::BTreeMap;
use std::net::{IpAddr, SocketAddr};
use std::time::Instant;

use axum::body::Body;
use bytes::Bytes;
use common::{
    TIMEOUT, frame, head_has, local_non_loopback_ipv4, read_exact_into, refused_addr, self_signed,
    self_signed_settings, start_front_door, start_front_door_on, start_tls_front_door, tls_connect,
    ws_backend, ws_client, ws_upgrade,
};
use gobby_core::bootstrap::{RouteBackend, TlsBootstrap, TlsMode, parse_hub_database_bootstrap};
use gobby_daemon::front_door::auth::{
    AuthState, KeyIdentity, KeyResolver, PostgresKeyResolver, ResolveFuture,
};
use gobby_daemon::front_door::health::BackendState;
use gobby_daemon::front_door::routes::{FAMILIES, RouteTable};
use gobby_daemon::front_door::tls;
use gobby_daemon::front_door::{FrontDoor, FrontDoorState};
use gobby_daemon::serve::{bind, companion_addr, plaintext_allowed};
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
const TEST_API_KEY: &str = "gobby_003aUlTJC7tjlCTQj2uNU3MFagCXG9LRKRcwGkBIDlf1Yo7hP";

async fn request_challenge(door: &FrontDoor, body: serde_json::Value) -> Response<Body> {
    let request = Request::post("/api/runtime/handshake/challenge")
        .header("content-type", "application/json")
        .header("x-gobby-user-id", "forged-user")
        .header("x-gobby-machine-id", "forged-machine")
        .header("x-gobby-key-id", "forged-key")
        .header("x-gobby-front-door", "forged-secret")
        .body(Body::from(body.to_string()))
        .expect("challenge");
    door.handle(request, "127.0.0.1:1234".parse().expect("peer"), false)
        .await
}

async fn response_json(response: Response<Body>) -> serde_json::Value {
    let body = response
        .into_body()
        .collect()
        .await
        .expect("body")
        .to_bytes();
    serde_json::from_slice(&body).expect("json")
}

#[tokio::test]
async fn interactive_challenge_cannot_expose_managed_signing_key() {
    use hmac::{Hmac, Mac};
    use sha2::Sha256;

    let home = tempfile::tempdir().expect("home");
    let path = home.path().join("bootstrap.yaml");
    std::fs::write(&path, format!("api_key: {TEST_API_KEY}\n")).expect("key");
    let (backend, mut seen) = header_backend().await;
    let auth = Arc::new(AuthState::new("boot-secret".into(), None, path).expect("auth"));
    let state = FrontDoorState::new(backend, BackendState::Down, auth);
    let table = RouteTable::new(FAMILIES, &BTreeMap::new(), &state);
    let door = FrontDoor::new(state, table);
    let response = request_challenge(
        &door,
        serde_json::json!({"nonce": "Z29iYnktbWFuYWdlZC10b2tlbi12MQ=="}),
    )
    .await;
    assert_eq!(response.status(), StatusCode::OK);
    let mut mac = Hmac::<Sha256>::new_from_slice(TEST_API_KEY.as_bytes()).expect("HMAC key");
    mac.update(b"gobby-managed-token-v1");
    let signing_key: String = mac
        .finalize()
        .into_bytes()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    assert_ne!(response_json(response).await["proof"], signing_key);
    assert!(seen.try_recv().is_err(), "native challenge reached Python");
}

#[tokio::test]
async fn interactive_challenge_answered_locally() {
    let home = tempfile::tempdir().expect("home");
    let path = home.path().join("bootstrap.yaml");
    std::fs::write(&path, format!("api_key: {TEST_API_KEY}\n")).expect("key");
    let (backend, mut seen) = header_backend().await;
    let auth = Arc::new(AuthState::new("boot-secret".into(), None, path.clone()).expect("auth"));
    let state = FrontDoorState::new(backend, BackendState::Down, auth);
    let table = RouteTable::new(FAMILIES, &BTreeMap::new(), &state);
    let door = FrontDoor::new(state, table);
    for body in [
        serde_json::json!({"nonce": "aGVsbG8="}),
        serde_json::json!({"nonce": "aGVsbG8"}),
        serde_json::json!({"nonce": "aGVsbG8", "kind": "managed", "caller": {"kind": "interactive"}}),
        serde_json::json!({"nonce": "aGVsbG8", "kind": "unknown", "caller": {}}),
    ] {
        let response = request_challenge(&door, body).await;
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(response.headers()["cache-control"], "no-store");
        assert_eq!(response.headers()["content-type"], "application/json");
        assert_eq!(
            response_json(response).await,
            serde_json::json!({"proof": "4163c345e4d8622b215e6153831f84cd9f9f5d1dbac5f27bd3760568c3e3ee73"})
        );
    }
    std::fs::write(
        &path,
        "api_key: gobby_00000000000000000000000000000000000000000002CZclj\n",
    )
    .expect("rotate");
    let response = request_challenge(&door, serde_json::json!({"nonce": "aGVsbG8"})).await;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response_json(response).await["proof"],
        "06c880760459f24b9d4814114c4e6d088427b1f1af1e48552f20bfa222fb9dbd"
    );
    for nonce in ["a".repeat(45), "!invalid!".into(), "aGVsbG8===".into()] {
        let response = request_challenge(&door, serde_json::json!({"nonce": nonce})).await;
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    }
    let response = request_challenge(
        &door,
        serde_json::json!({"nonce": "aGVsbG8", "unknown": true}),
    )
    .await;
    assert!(response.status().is_client_error());
    let response = request_challenge(
        &door,
        serde_json::json!({"nonce": "aGVsbG8", "caller": {"unknown": true}}),
    )
    .await;
    assert!(response.status().is_client_error());
    for body in [
        serde_json::json!({"nonce": "aGVsbG8", "kind": "unknown"}),
        serde_json::json!({"nonce": "aGVsbG8", "kind": "interactive", "caller": {"kind": "unknown"}}),
    ] {
        let response = request_challenge(&door, body).await;
        assert_eq!(response.status(), StatusCode::FORBIDDEN);
        assert_eq!(response_json(response).await["code"], "claims_mismatch");
    }
    for authorization in ["Bearer unknown", ""] {
        let request = Request::post("/api/runtime/handshake/challenge")
            .header("authorization", authorization)
            .body(Body::from("not JSON"))
            .expect("request");
        let response = door
            .handle(request, "127.0.0.1:1234".parse().expect("peer"), false)
            .await;
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
        assert_eq!(
            response_json(response).await["code"],
            "credential_before_proof"
        );
    }
    for content in ["api_key: ''\n", "bind_host: 127.0.0.1\n", "not: [valid"] {
        std::fs::write(&path, content).expect("unavailable key");
        let response = request_challenge(&door, serde_json::json!({"nonce": "aGVsbG8"})).await;
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(
            response_json(response).await["code"],
            "key_resolver_unavailable"
        );
    }
    std::fs::remove_file(&path).expect("remove bootstrap");
    let response = request_challenge(&door, serde_json::json!({"nonce": "aGVsbG8"})).await;
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert!(seen.try_recv().is_err(), "native challenges reached Python");
    for body in [
        serde_json::json!({"nonce": "aGVsbG8", "kind": "managed", "claims": {}}),
        serde_json::json!({"nonce": "aGVsbG8", "kind": "unknown", "caller": {"kind": "managed", "claims": {}}}),
    ] {
        let response = request_challenge(&door, body).await;
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response
                .into_body()
                .collect()
                .await
                .expect("managed body")
                .to_bytes(),
            Bytes::from_static(b"ok")
        );
        let (path, headers) = tokio::time::timeout(TIMEOUT, seen.recv())
            .await
            .expect("managed forwarding")
            .expect("headers");
        assert_eq!(path, "/api/runtime/handshake/challenge");
        for name in [
            "x-gobby-user-id",
            "x-gobby-machine-id",
            "x-gobby-key-id",
            "x-gobby-front-door",
        ] {
            assert!(!headers.contains_key(name), "{name}");
        }
    }
}

struct FixedResolver {
    calls: Mutex<Vec<String>>,
    identity: Option<KeyIdentity>,
}

impl KeyResolver for FixedResolver {
    fn resolve<'a>(&'a self, key_hash: &'a str) -> ResolveFuture<'a> {
        Box::pin(async move {
            self.calls
                .lock()
                .expect("resolver calls")
                .push(key_hash.to_owned());
            Ok(self.identity.clone())
        })
    }
}

struct FailedResolver;

#[tokio::test]
async fn postgres_resolver_and_node_mode_fail_closed() {
    let target = refused_addr().await;
    let resolver = Arc::new(PostgresKeyResolver::new("not a PostgreSQL DSN"));
    let front_door = authenticated_front_door(target, resolver);
    let auth = Arc::new(
        AuthState::new("boot-secret".into(), None, Default::default()).expect("node auth"),
    );
    let state = FrontDoorState::new(target, BackendState::Down, auth);
    let table = RouteTable::new(FAMILIES, &BTreeMap::new(), &state);
    let node = FrontDoor::new(state, table);
    for door in [front_door, node] {
        let request = Request::builder()
            .uri("/api/mcp/servers")
            .header("authorization", format!("Bearer {TEST_API_KEY}"))
            .body(Body::empty())
            .expect("request");
        let response = door
            .handle(request, "127.0.0.1:1234".parse().expect("peer"), false)
            .await;
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
        let body = response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes();
        let body: serde_json::Value = serde_json::from_slice(&body).expect("json");
        assert_eq!(body["code"], "key_resolver_unavailable");
    }
}

impl KeyResolver for FailedResolver {
    fn resolve<'a>(&'a self, _key_hash: &'a str) -> ResolveFuture<'a> {
        Box::pin(async { anyhow::bail!("isolated resolver failure") })
    }
}

#[cfg(unix)]
#[tokio::test]
async fn resolver_error_is_503_and_missing_secret_refuses_start() {
    let front_door = authenticated_front_door(refused_addr().await, Arc::new(FailedResolver));
    let request = Request::builder()
        .uri("/api/mcp/servers")
        .header("authorization", format!("Bearer {TEST_API_KEY}"))
        .body(Body::empty())
        .expect("request");
    let response = front_door
        .handle(request, "127.0.0.1:1234".parse().expect("peer"), false)
        .await;
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    let body = response
        .into_body()
        .collect()
        .await
        .expect("body")
        .to_bytes();
    let body: serde_json::Value = serde_json::from_slice(&body).expect("json");
    assert_eq!(body["code"], "key_resolver_unavailable");

    let home = tempfile::tempdir().expect("home");
    let mut ports = Vec::new();
    while ports.len() < 2 {
        let probe = std::net::TcpListener::bind("127.0.0.1:0").expect("probe");
        let port = probe.local_addr().expect("probe addr").port();
        if port <= 65435 && port != 60891 && !ports.contains(&port) {
            ports.push(port);
        }
    }
    std::fs::write(
        home.path().join("bootstrap.yaml"),
        format!("daemon_port: {}\nwebsocket_port: {}\n", ports[0], ports[1]),
    )
    .expect("bootstrap");
    for secret in [None, Some("")] {
        let (reader, writer) = std::io::pipe().expect("pipe");
        drop(writer);
        let mut command = std::process::Command::new(env!("CARGO_BIN_EXE_gdaemon"));
        command
            .arg("serve")
            .env("GOBBY_HOME", home.path())
            .env("GOBBY_PARENT_FD", "0")
            .env_remove("GOBBY_FRONT_DOOR_SECRET")
            .stdin(std::process::Stdio::from(reader));
        if let Some(secret) = secret {
            command.env("GOBBY_FRONT_DOOR_SECRET", secret);
        }
        let output = command.output().expect("serve refusal");
        assert!(!output.status.success(), "serve accepted {secret:?}");
        assert!(String::from_utf8_lossy(&output.stderr).contains("GOBBY_FRONT_DOOR_SECRET"));
    }
}

#[tokio::test]
async fn break_glass_header_passes_through_when_resolver_fails() {
    let (backend, mut seen) = header_backend().await;
    let front_door = authenticated_front_door(backend, Arc::new(FailedResolver));
    let request = Request::builder()
        .uri("/api/mcp/servers")
        .header("x-gobby-break-glass", "recovery-secret")
        .header("x-gobby-user-id", "forged-user")
        .header("x-gobby-machine-id", "forged-machine")
        .header("x-gobby-key-id", "forged-key")
        .header("x-gobby-front-door", "forged-secret")
        .body(Body::empty())
        .expect("request");
    let response = front_door
        .handle(request, "127.0.0.1:1234".parse().expect("peer"), false)
        .await;
    assert_eq!(response.status(), StatusCode::OK);
    let (_, headers) = tokio::time::timeout(TIMEOUT, seen.recv())
        .await
        .expect("backend request")
        .expect("headers");
    assert_eq!(headers["x-gobby-break-glass"], "recovery-secret");
    for name in [
        "x-gobby-user-id",
        "x-gobby-machine-id",
        "x-gobby-key-id",
        "x-gobby-front-door",
    ] {
        assert!(!headers.contains_key(name), "{name}");
    }
}

#[tokio::test]
async fn invalid_keys_skip_resolver_and_revoked_keys_refuse() {
    let resolver = Arc::new(FixedResolver {
        calls: Mutex::new(Vec::new()),
        identity: None,
    });
    let front_door = authenticated_front_door(refused_addr().await, resolver.clone());
    for key in [
        "gobby_invalid",
        "gobby_003aUlTJC7tjlCTQj2uNU3MFagCXG9LRKRcwGkBIDlf1Yo7hQ",
        "unknown-token",
        "",
    ] {
        let request = Request::builder()
            .uri("/api/mcp/servers")
            .header("authorization", format!("Bearer {key}"))
            .body(Body::empty())
            .expect("request");
        let response = front_door
            .handle(request, "127.0.0.1:1234".parse().expect("peer"), false)
            .await;
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED, "{key}");
        let body = response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes();
        let body: serde_json::Value = serde_json::from_slice(&body).expect("json");
        assert_eq!(body["code"], "missing_auth");
    }
    assert!(resolver.calls.lock().expect("calls").is_empty());
    let request = Request::builder()
        .uri("/api/mcp/servers")
        .header("authorization", format!("Bearer {TEST_API_KEY}"))
        .body(Body::empty())
        .expect("request");
    let response = front_door
        .handle(request, "127.0.0.1:1234".parse().expect("peer"), false)
        .await;
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    assert_eq!(resolver.calls.lock().expect("calls").len(), 1);
}

fn authenticated_front_door(target: SocketAddr, resolver: Arc<dyn KeyResolver>) -> FrontDoor {
    let state = FrontDoorState::new(
        target,
        BackendState::Down,
        Arc::new(
            AuthState::new("boot-secret".into(), Some(resolver), Default::default())
                .expect("auth state"),
        ),
    );
    let table = RouteTable::new(FAMILIES, &BTreeMap::new(), &state);
    FrontDoor::new(state, table)
}

async fn start_authenticated_front_door(backend: SocketAddr, auth: Arc<AuthState>) -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("front door");
    let addr = listener.local_addr().expect("front door addr");
    tokio::spawn(async move {
        gobby_daemon::serve::serve(
            vec![gobby_daemon::serve::PublicListener { listener, backend }],
            &BTreeMap::new(),
            None,
            auth,
            std::future::pending(),
        )
        .await
        .expect("serve");
    });
    addr
}

#[tokio::test]
async fn valid_key_forwards_identity_headers() {
    let (backend, mut seen) = header_backend().await;
    let resolver = Arc::new(FixedResolver {
        calls: Mutex::new(Vec::new()),
        identity: Some(KeyIdentity {
            user_id: "operator-user".into(),
            machine_id: "node-machine".into(),
            key_id: "operator-key".into(),
        }),
    });
    let front_door = authenticated_front_door(backend, resolver.clone());
    let request = Request::builder()
        .uri("/api/mcp/servers")
        .header("authorization", format!("Bearer {TEST_API_KEY}"))
        .header("x-gobby-user-id", "forged-user")
        .header("x-gobby-machine-id", "forged-machine")
        .header("x-gobby-key-id", "forged-key")
        .header("x-gobby-front-door", "forged-secret")
        .body(Body::empty())
        .expect("request");
    let response = front_door
        .handle(request, "127.0.0.1:1234".parse().expect("peer"), false)
        .await;
    assert_eq!(response.status(), StatusCode::OK);
    let (_, headers) = tokio::time::timeout(TIMEOUT, seen.recv())
        .await
        .expect("backend request")
        .expect("headers");
    assert_eq!(headers["x-gobby-user-id"], "operator-user");
    assert_eq!(headers["x-gobby-machine-id"], "node-machine");
    assert_eq!(headers["x-gobby-key-id"], "operator-key");
    assert_eq!(headers["x-gobby-front-door"], "boot-secret");
    assert_eq!(headers["authorization"], format!("Bearer {TEST_API_KEY}"));
    assert_eq!(
        *resolver.calls.lock().expect("calls"),
        vec![gobby_core::api_key_format::hash(TEST_API_KEY)]
    );

    let (backend, capture) = ws_backend(0, Vec::new()).await;
    let auth = Arc::new(
        AuthState::new(
            "boot-secret".into(),
            Some(resolver.clone()),
            Default::default(),
        )
        .expect("auth"),
    );
    let address = start_authenticated_front_door(backend, auth).await;
    let mut client = TcpStream::connect(address).await.expect("client");
    let forged = format!(
        "authorization: Bearer {TEST_API_KEY}\r\nx-gobby-user-id: forged\r\nx-gobby-machine-id: forged\r\nx-gobby-key-id: forged\r\nx-gobby-front-door: forged\r\n"
    );
    let (response, _) =
        tokio::time::timeout(TIMEOUT, ws_upgrade(&mut client, address, "/ws", &forged))
            .await
            .expect("upgrade");
    assert!(response.starts_with("HTTP/1.1 101"), "{response}");
    let capture = tokio::time::timeout(TIMEOUT, capture)
        .await
        .expect("capture")
        .expect("backend");
    for line in [
        "x-gobby-user-id: operator-user",
        "x-gobby-machine-id: node-machine",
        "x-gobby-key-id: operator-key",
        "x-gobby-front-door: boot-secret",
    ] {
        assert!(
            head_has(&capture.request_head, line),
            "{line}: {}",
            capture.request_head
        );
    }
    assert!(!capture.request_head.contains("forged"));
    assert_eq!(resolver.calls.lock().expect("calls").len(), 2);

    let auth = Arc::new(
        AuthState::new(
            "boot-secret".into(),
            Some(resolver.clone()),
            Default::default(),
        )
        .expect("auth"),
    );
    let address = start_authenticated_front_door(refused_addr().await, auth).await;
    for key in ["unknown", "gobby_bad-checksum"] {
        let mut client = TcpStream::connect(address).await.expect("client");
        let (response, _) = tokio::time::timeout(
            TIMEOUT,
            ws_upgrade(
                &mut client,
                address,
                "/ws",
                &format!("authorization: Bearer {key}\r\n"),
            ),
        )
        .await
        .expect("refusal");
        assert!(response.starts_with("HTTP/1.1 401"), "{response}");
    }
    assert_eq!(resolver.calls.lock().expect("calls").len(), 2);
}

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
        .header("authorization", "Bearer gobby-agent-v1.passthrough")
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
    assert_eq!(
        seen.headers["authorization"],
        "Bearer gobby-agent-v1.passthrough"
    );
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
/// A keep-alive backend that, like uvicorn at its keep-alive expiry, closes a
/// connection without answering when a request arrives after `idle_limit` idle.
async fn expiring_keep_alive_backend(idle_limit: Duration) -> std::net::SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind backend");
    let addr = listener.local_addr().expect("backend addr");
    tokio::spawn(async move {
        loop {
            let (mut stream, _) = listener.accept().await.expect("accept");
            tokio::spawn(async move {
                let mut answered: Option<tokio::time::Instant> = None;
                let mut buffer = Vec::new();
                loop {
                    while !buffer.windows(4).any(|window| window == b"\r\n\r\n") {
                        let mut chunk = [0_u8; 1024];
                        match stream.read(&mut chunk).await {
                            Ok(0) | Err(_) => return,
                            Ok(read) => buffer.extend_from_slice(&chunk[..read]),
                        }
                    }
                    buffer.clear();
                    if answered.is_some_and(|at| at.elapsed() >= idle_limit) {
                        return;
                    }
                    let reply = b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok";
                    if stream.write_all(reply).await.is_err() {
                        return;
                    }
                    answered = Some(tokio::time::Instant::now());
                }
            });
        }
    });
    addr
}

#[tokio::test]
async fn backend_keep_alive_expiry_does_not_surface_502() {
    // Outlasts the front door's pooled-connection idle timeout.
    let idle_gap = Duration::from_millis(2500);
    let backend = expiring_keep_alive_backend(idle_gap).await;
    let front_door = start_front_door(backend).await;

    for attempt in 0..2 {
        if attempt > 0 {
            // Let the pool reclaim the first connection, then idle it on the paused
            // clock that both the pool timer and the backend read.
            tokio::task::yield_now().await;
            tokio::time::pause();
            tokio::time::advance(idle_gap).await;
            tokio::time::resume();
        }
        let request = Request::get("/api/hooks/execute")
            .header("host", "localhost")
            .body(Full::new(Bytes::new()))
            .expect("request");
        let response = tokio::time::timeout(TIMEOUT, send(front_door, request))
            .await
            .expect("response timed out");
        let status = response.status();
        let body = response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes();
        assert_eq!(status, StatusCode::OK, "attempt {attempt}: {body:?}");
        assert_eq!(body, "ok", "attempt {attempt}");
    }
}

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
        .env("GOBBY_FRONT_DOOR_SECRET", "test-front-door-secret")
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

/// The child's exit status if it exits within `within`; `None` if it is still running.
#[cfg(unix)]
fn wait_for_exit(
    process: &mut std::process::Child,
    within: Duration,
) -> Option<std::process::ExitStatus> {
    let deadline = std::time::Instant::now() + within;
    loop {
        if let Some(status) = process.try_wait().expect("poll serve") {
            return Some(status);
        }
        if std::time::Instant::now() >= deadline {
            return None;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[cfg(unix)]
#[test]
fn parent_fd_eof_stops_serve() {
    // Without GOBBY_PARENT_FD there is no watch: EOF on the same pipe changes nothing.
    let (reader, writer) = std::io::pipe().expect("pipe");
    let (mut unwatched, ports) = spawn_serve(None, reader);
    drop(writer);
    assert_eq!(
        wait_for_exit(&mut unwatched.process, Duration::from_millis(500)),
        None,
        "serve without GOBBY_PARENT_FD must keep running"
    );
    assert!(std::net::TcpStream::connect(("127.0.0.1", ports[0])).is_ok());
    drop(unwatched);

    // With it, the owner's end closing stops serve cleanly and frees both public ports.
    let (reader, writer) = std::io::pipe().expect("pipe");
    let (mut watched, ports) = spawn_serve(Some("0"), reader);
    drop(writer);
    let status = wait_for_exit(&mut watched.process, TIMEOUT).expect("serve ignored parent EOF");
    assert!(status.success(), "serve exited with {status}");
    for port in ports {
        assert!(
            std::net::TcpListener::bind(("127.0.0.1", port)).is_ok(),
            "public port {port} still held after serve exited"
        );
    }
}

// ---- Front-door TLS (plan 4.1) ----

fn get(path: &str) -> Request<Full<Bytes>> {
    Request::get(path)
        .header("host", "localhost")
        .body(Full::new(Bytes::new()))
        .expect("request")
}

/// Send one request on an already-open stream (plaintext or TLS).
async fn send_on<S>(stream: S, request: Request<Full<Bytes>>) -> Response<Incoming>
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send + 'static,
{
    let (mut sender, connection) = hyper::client::conn::http1::handshake(TokioIo::new(stream))
        .await
        .expect("client handshake");
    tokio::spawn(connection);
    sender.send_request(request).await.expect("send request")
}

/// A backend answering every request `200 ok` and reporting each request's
/// path and headers.
async fn header_backend() -> (
    SocketAddr,
    mpsc::UnboundedReceiver<(String, hyper::HeaderMap)>,
) {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind backend");
    let addr = listener.local_addr().expect("backend addr");
    let (seen_tx, seen_rx) = mpsc::unbounded_channel();
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            let seen_tx = seen_tx.clone();
            tokio::spawn(async move {
                let service = service_fn(move |request: Request<Incoming>| {
                    let _ =
                        seen_tx.send((request.uri().path().to_owned(), request.headers().clone()));
                    async {
                        Ok::<_, Infallible>(Response::new(Full::new(Bytes::from_static(b"ok"))))
                    }
                });
                let _ = server_http1::Builder::new()
                    .serve_connection(TokioIo::new(stream), service)
                    .await;
            });
        }
    });
    (addr, seen_rx)
}

async fn body_text(response: Response<Incoming>) -> String {
    let body = response
        .into_body()
        .collect()
        .await
        .expect("body")
        .to_bytes();
    String::from_utf8(body.to_vec()).expect("utf8 body")
}

#[tokio::test]
async fn ws_splice_over_self_signed_tls() {
    let cert = self_signed("127.0.0.1", &[]);

    let refused = refused_addr().await;
    let front_door =
        start_tls_front_door(refused, cert.loaded.config.clone(), BTreeMap::new()).await;
    let stream = tls_connect(front_door, &cert.cert_pem)
        .await
        .expect("pinned handshake");
    let response = tokio::time::timeout(TIMEOUT, send_on(stream, get("/api/sessions")))
        .await
        .expect("typed 503 timed out");
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    let body: serde_json::Value =
        serde_json::from_str(&body_text(response).await).expect("json body");
    assert_eq!(body["backend"]["target"], refused.to_string());

    let (backend, mut seen) = header_backend().await;
    let front_door =
        start_tls_front_door(backend, cert.loaded.config.clone(), BTreeMap::new()).await;
    let stream = tls_connect(front_door, &cert.cert_pem)
        .await
        .expect("pinned handshake");
    let response = tokio::time::timeout(TIMEOUT, send_on(stream, get("/api/sessions?limit=1")))
        .await
        .expect("passthrough timed out");
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(body_text(response).await, "ok");
    let (path, _) = seen.recv().await.expect("backend saw the request");
    assert_eq!(path, "/api/sessions");

    let close_payload = [&1000_u16.to_be_bytes()[..], b"done"].concat();
    let client_frames = [frame(0x81, b"hi", true), frame(0x88, &close_payload, true)].concat();
    let backend_frames = [
        frame(0x81, b"hi", false),
        frame(0x88, &close_payload, false),
    ]
    .concat();
    let (ws_backend_addr, captured) = ws_backend(client_frames.len(), backend_frames.clone()).await;
    let front_door =
        start_tls_front_door(ws_backend_addr, cert.loaded.config.clone(), BTreeMap::new()).await;
    let mut stream = tls_connect(front_door, &cert.cert_pem)
        .await
        .expect("pinned handshake");
    let (head, mut received) = ws_upgrade(&mut stream, front_door, "/ws", "").await;
    assert!(head.starts_with("HTTP/1.1 101"), "{head}");
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
    assert_eq!(capture.received, client_frames);
    assert_eq!(received, backend_frames);
}

#[tokio::test]
async fn pinned_client_rejects_unpinned_cert() {
    let served = self_signed("127.0.0.1", &[]);
    let other = self_signed("127.0.0.1", &[]);
    let front_door = start_tls_front_door(
        refused_addr().await,
        served.loaded.config.clone(),
        BTreeMap::new(),
    )
    .await;

    // The pinned root store holds only `other`, so no system root can vouch
    // for the served certificate.
    let error = tls_connect(front_door, &other.cert_pem)
        .await
        .expect_err("a certificate other than the pinned one must be rejected");
    assert_eq!(error.kind(), std::io::ErrorKind::InvalidData, "{error}");
    assert!(tls_connect(front_door, &served.cert_pem).await.is_ok());

    for fingerprint in [&served.loaded.fingerprint, &other.loaded.fingerprint] {
        let hex = fingerprint.strip_prefix("sha256:").expect("sha256: prefix");
        assert_eq!(hex.len(), 64, "{fingerprint}");
        assert!(
            hex.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
            "{fingerprint}"
        );
    }
    assert_ne!(served.loaded.fingerprint, other.loaded.fingerprint);
    assert_eq!(
        tls::fingerprint(&served.cert_pem).expect("fingerprint"),
        served.loaded.fingerprint
    );
}

#[tokio::test]
async fn plaintext_only_from_loopback_peers() {
    for (peer, allowed) in [
        ("127.0.0.1", true),
        ("127.8.9.10", true),
        ("::1", true),
        ("::ffff:127.0.0.1", true),
        ("100.64.0.10", false),
        ("2001:db8::1", false),
        ("::ffff:100.64.0.10", false),
    ] {
        let ip: IpAddr = peer.parse().expect("ip");
        assert_eq!(plaintext_allowed(ip), allowed, "{peer}");
    }

    let cert = self_signed("127.0.0.1", &[]);
    let refused = refused_addr().await;
    // IPv4 and IPv6 loopback peers, then an IPv4 loopback peer reaching a
    // dual-stack `[::]` listener as `::ffff:127.0.0.1`.
    for (bind, dial) in [
        ("127.0.0.1:0", "127.0.0.1"),
        ("[::1]:0", "::1"),
        ("[::]:0", "127.0.0.1"),
    ] {
        let bind: SocketAddr = bind.parse().expect("bind addr");
        if TcpListener::bind(bind).await.is_err() {
            eprintln!("{bind} is unavailable here; skipping that family");
            continue;
        }
        let front_door = start_front_door_on(
            bind,
            refused,
            Some(cert.loaded.config.clone()),
            BTreeMap::new(),
        )
        .await;
        let target = SocketAddr::new(dial.parse().expect("dial ip"), front_door.port());
        let Ok(stream) = TcpStream::connect(target).await else {
            eprintln!("{target} is unreachable from {bind}; skipping");
            continue;
        };
        let response = tokio::time::timeout(TIMEOUT, send_on(stream, get("/api/sessions")))
            .await
            .expect("loopback plaintext timed out");
        assert_eq!(
            response.status(),
            StatusCode::SERVICE_UNAVAILABLE,
            "{target}"
        );
        if dial == "::1" || bind.ip().is_unspecified() {
            continue; // The certificate's 127.0.0.1 SAN covers the IPv4 TLS case below.
        }
        let stream = tls_connect(target, &cert.cert_pem)
            .await
            .expect("TLS on the same port");
        let response = tokio::time::timeout(TIMEOUT, send_on(stream, get("/api/sessions")))
            .await
            .expect("loopback TLS timed out");
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    let Some(ip) = local_non_loopback_ipv4() else {
        eprintln!("no non-loopback IPv4 address; the live non-loopback refusal is skipped");
        return;
    };
    let public_cert = self_signed(&ip.to_string(), &[]);
    let front_door = start_front_door_on(
        SocketAddr::new(ip, 0),
        refused,
        Some(public_cert.loaded.config.clone()),
        BTreeMap::new(),
    )
    .await;
    let mut stream = TcpStream::connect(front_door)
        .await
        .expect("connect from a non-loopback address");
    stream
        .write_all(b"GET /api/sessions HTTP/1.1\r\nhost: localhost\r\n\r\n")
        .await
        .expect("write plaintext request");
    let mut buffer = [0_u8; 64];
    let read = tokio::time::timeout(TIMEOUT, stream.read(&mut buffer))
        .await
        .expect("non-loopback plaintext connection was left open");
    assert!(
        matches!(read, Ok(0) | Err(_)),
        "a non-loopback plaintext peer was served"
    );
    let stream = tls_connect(front_door, &public_cert.cert_pem)
        .await
        .expect("TLS from a non-loopback peer");
    let response = tokio::time::timeout(TIMEOUT, send_on(stream, get("/api/sessions")))
        .await
        .expect("non-loopback TLS timed out");
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
}

#[tokio::test]
async fn concrete_bind_adds_loopback_listener() {
    for (bound, expected) in [
        ("0.0.0.0:60887", None),
        ("[::]:60887", None),
        ("127.0.0.1:60887", None),
        ("[::1]:60887", None),
        ("100.64.0.10:60887", Some("127.0.0.1:60887")),
        ("[2001:db8::1]:60887", Some("[::1]:60887")),
    ] {
        let expected: Option<SocketAddr> = expected.map(|addr| addr.parse().expect("addr"));
        assert_eq!(
            companion_addr(bound.parse().expect("addr")),
            expected,
            "{bound}"
        );
    }

    let wildcard = bind("0.0.0.0", 0, 1).await.expect("wildcard bind");
    assert_eq!(wildcard.len(), 1, "a wildcard bind gets no companion");
    let port = wildcard[0].listener.local_addr().expect("addr").port();
    TcpStream::connect(("127.0.0.1", port))
        .await
        .expect("a wildcard listener accepts loopback");
    let loopback = bind("127.0.0.1", 0, 1).await.expect("loopback bind");
    assert_eq!(loopback.len(), 1, "a loopback bind gets no companion");

    let Some(ip) = local_non_loopback_ipv4() else {
        eprintln!("no non-loopback IPv4 address; the live companion check is skipped");
        return;
    };
    let listeners = bind(&ip.to_string(), 0, 1).await.expect("concrete bind");
    assert_eq!(listeners.len(), 2, "a concrete bind adds a companion");
    let primary = listeners[0].listener.local_addr().expect("primary addr");
    let companion = listeners[1].listener.local_addr().expect("companion addr");
    assert_eq!(primary.ip(), ip);
    assert_eq!(
        companion,
        SocketAddr::from(([127, 0, 0, 1], primary.port()))
    );
    TcpStream::connect(companion)
        .await
        .expect("the companion accepts loopback");
}

fn forged(path: &str) -> Request<Full<Bytes>> {
    Request::get(path)
        .header("host", "localhost")
        .header("forwarded", "for=6.6.6.6;proto=https")
        .header("x-forwarded-for", "6.6.6.6")
        .header("x-real-ip", "6.6.6.6")
        .header("x-forwarded-proto", "https")
        .header("x-forwarded-host", "evil.example")
        // Nominating the observed fields as hop-by-hop, across duplicate
        // fields and mixed case, must not let them be stripped downstream.
        .header("connection", "X-Forwarded-For, keep-alive")
        .header("connection", "x-FORWARDED-proto")
        .body(Full::new(Bytes::new()))
        .expect("request")
}

fn assert_observed(headers: &hyper::HeaderMap, proto: &str) {
    assert_eq!(
        headers
            .get_all("x-forwarded-for")
            .iter()
            .collect::<Vec<_>>(),
        ["127.0.0.1"]
    );
    assert_eq!(headers["x-forwarded-proto"], proto);
    for name in ["forwarded", "x-real-ip", "x-forwarded-host"] {
        assert!(!headers.contains_key(name), "{name} reached the backend");
    }
}

#[tokio::test]
async fn forwarding_headers_carry_only_observed_peer() {
    let loopback: SocketAddr = "127.0.0.1:0".parse().expect("addr");

    // Proxy path, plaintext.
    let (backend, mut seen) = header_backend().await;
    let front_door = start_front_door_on(loopback, backend, None, BTreeMap::new()).await;
    let stream = TcpStream::connect(front_door).await.expect("connect");
    tokio::time::timeout(TIMEOUT, send_on(stream, forged("/api/sessions")))
        .await
        .expect("proxy timed out");
    let (_, headers) = seen.recv().await.expect("backend saw the request");
    assert_observed(&headers, "http");

    // Proxy path over TLS.
    let cert = self_signed("127.0.0.1", &[]);
    let front_door =
        start_tls_front_door(backend, cert.loaded.config.clone(), BTreeMap::new()).await;
    let stream = tls_connect(front_door, &cert.cert_pem)
        .await
        .expect("pinned handshake");
    tokio::time::timeout(TIMEOUT, send_on(stream, forged("/api/sessions")))
        .await
        .expect("TLS proxy timed out");
    let (_, headers) = seen.recv().await.expect("backend saw the TLS request");
    assert_observed(&headers, "https");

    // Native health path.
    let routes = BTreeMap::from([("health".to_owned(), RouteBackend::Native)]);
    let front_door = start_front_door_on(loopback, backend, None, routes).await;
    let stream = TcpStream::connect(front_door).await.expect("connect");
    let response = tokio::time::timeout(TIMEOUT, send_on(stream, forged("/api/health")))
        .await
        .expect("native health timed out");
    assert_eq!(response.headers()["x-gobby-served-by"], "gdaemon");
    let (path, headers) = seen
        .recv()
        .await
        .expect("native health reached the backend");
    assert_eq!(path, "/api/health");
    assert_observed(&headers, "http");

    // WS splice path.
    let (ws_backend_addr, captured) = ws_backend(0, Vec::new()).await;
    let front_door = start_front_door_on(loopback, ws_backend_addr, None, BTreeMap::new()).await;
    let mut stream = TcpStream::connect(front_door).await.expect("connect");
    let (head, _) = ws_upgrade(
        &mut stream,
        front_door,
        "/ws",
        "forwarded: for=6.6.6.6\r\nx-forwarded-for: 6.6.6.6\r\nx-real-ip: 6.6.6.6\r\n\
         x-forwarded-proto: https\r\nx-forwarded-host: evil.example\r\n\
         connection: X-Forwarded-For, x-FORWARDED-proto\r\n",
    )
    .await;
    assert!(head.starts_with("HTTP/1.1 101"), "{head}");
    let capture = tokio::time::timeout(TIMEOUT, captured)
        .await
        .expect("capture timed out")
        .expect("capture");
    let request_head = capture.request_head.to_ascii_lowercase();
    assert!(
        head_has(&request_head, "x-forwarded-for: 127.0.0.1"),
        "{request_head}"
    );
    assert!(
        head_has(&request_head, "x-forwarded-proto: http"),
        "{request_head}"
    );
    assert!(!request_head.contains("6.6.6.6"), "{request_head}");
    assert!(!request_head.contains("evil.example"), "{request_head}");
}

#[tokio::test]
async fn stalled_preauth_connections_expire_without_blocking() {
    let cert = self_signed("127.0.0.1", &[]);
    let front_door = start_tls_front_door(
        refused_addr().await,
        cert.loaded.config.clone(),
        BTreeMap::new(),
    )
    .await;
    let started = Instant::now();
    let mut silent = TcpStream::connect(front_door)
        .await
        .expect("connect silent");
    let mut partial = TcpStream::connect(front_door)
        .await
        .expect("connect partial");
    partial
        .write_all(&[0x16, 0x03, 0x01])
        .await
        .expect("partial ClientHello");

    let stream = TcpStream::connect(front_door)
        .await
        .expect("connect health");
    let response =
        tokio::time::timeout(Duration::from_secs(2), send_on(stream, get("/api/health")))
            .await
            .expect("a stalled peer blocked the listener");
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);

    for (name, stream) in [
        ("zero-byte", &mut silent),
        ("partial ClientHello", &mut partial),
    ] {
        let mut buffer = [0_u8; 16];
        let read = tokio::time::timeout(Duration::from_secs(20), stream.read(&mut buffer))
            .await
            .unwrap_or_else(|_| panic!("the {name} connection was never closed"));
        assert!(
            matches!(read, Ok(0) | Err(_)),
            "the {name} connection received data"
        );
        assert!(
            started.elapsed() >= Duration::from_secs(9),
            "the {name} connection closed before PREAUTH_DEADLINE"
        );
    }
}

#[test]
fn tls_pair_load_or_refuse() {
    let a = self_signed("127.0.0.1", &[]);
    let b = self_signed("127.0.0.1", &[]);
    let a_key = std::fs::read(a.dir.path().join("front_door.key")).expect("key a");
    let b_key = std::fs::read(b.dir.path().join("front_door.key")).expect("key b");
    let files = |dir: &std::path::Path| TlsBootstrap {
        mode: TlsMode::Files,
        ..self_signed_settings(dir, &[])
    };
    let refusal = |settings: &TlsBootstrap| {
        let error = tls::load(settings, "127.0.0.1")
            .err()
            .expect("the pair must be refused");
        format!("{error:#}")
    };

    /// Certificate bytes, key bytes, and the file name the error must name.
    type PairCase<'a> = (Option<&'a [u8]>, Option<&'a [u8]>, &'a str);
    let cases: [PairCase<'_>; 4] = [
        (Some(a.cert_pem.as_slice()), None, "front_door.key"),
        (None, Some(a_key.as_slice()), "front_door.crt"),
        (
            Some(b"not a certificate".as_slice()),
            Some(a_key.as_slice()),
            "front_door.crt",
        ),
        (
            Some(a.cert_pem.as_slice()),
            Some(b_key.as_slice()),
            "front_door.crt",
        ),
    ];
    for (cert, key, named) in cases {
        let dir = tempfile::tempdir().expect("tempdir");
        let cert_path = dir.path().join("front_door.crt");
        let key_path = dir.path().join("front_door.key");
        if let Some(cert) = cert {
            std::fs::write(&cert_path, cert).expect("write cert");
        }
        if let Some(key) = key {
            std::fs::write(&key_path, key).expect("write key");
        }
        let error = refusal(&self_signed_settings(dir.path(), &[]));
        assert!(
            error.contains(&dir.path().join(named).display().to_string()),
            "{error}"
        );
        assert_eq!(std::fs::read(&cert_path).ok().as_deref(), cert, "{error}");
        assert_eq!(std::fs::read(&key_path).ok().as_deref(), key, "{error}");
    }

    let loaded = tls::load(&files(a.dir.path()), "127.0.0.1")
        .expect("operator pair")
        .expect("files mode loads a pair");
    assert_eq!(loaded.fingerprint, a.loaded.fingerprint);

    let mismatched = tempfile::tempdir().expect("tempdir");
    std::fs::write(mismatched.path().join("front_door.crt"), &a.cert_pem).expect("cert");
    std::fs::write(mismatched.path().join("front_door.key"), &b_key).expect("key");
    assert!(refusal(&files(mismatched.path())).contains("does not match"));

    let missing = tempfile::tempdir().expect("tempdir");
    let error = refusal(&files(missing.path()));
    assert!(
        error.contains(&missing.path().join("front_door.crt").display().to_string()),
        "{error}"
    );
    assert!(!missing.path().join("front_door.crt").exists());

    assert!(
        tls::load(&TlsBootstrap::default(), "127.0.0.1")
            .expect("off")
            .is_none()
    );
}

#[test]
fn self_signed_san_policy() {
    let sans = ["hub.example.test".to_owned()];
    assert_eq!(
        tls::self_signed_sans("0.0.0.0", Some("hub-host"), &sans),
        [
            "localhost",
            "hub-host",
            "127.0.0.1",
            "::1",
            "hub.example.test"
        ]
    );
    assert_eq!(
        tls::self_signed_sans("::", None, &[]),
        ["localhost", "127.0.0.1", "::1"]
    );
    assert_eq!(
        tls::self_signed_sans("100.64.0.10", Some("hub-host"), &sans),
        [
            "localhost",
            "hub-host",
            "127.0.0.1",
            "::1",
            "100.64.0.10",
            "hub.example.test"
        ]
    );
    assert_eq!(
        tls::self_signed_sans("[2001:db8::1]", None, &[]),
        ["localhost", "127.0.0.1", "::1", "2001:db8::1"]
    );

    let generated = self_signed("100.64.0.10", &["hub.example.test", "100.64.0.11"]);
    let expected = tls::self_signed_sans(
        "100.64.0.10",
        tls::machine_hostname().as_deref(),
        &["hub.example.test".to_owned(), "100.64.0.11".to_owned()],
    );
    assert!(
        tls::uncovered_names(&generated.cert_pem, &expected)
            .expect("parse")
            .is_empty()
    );
    let extra = ["10.9.8.7".to_owned(), "other.example.test".to_owned()];
    assert_eq!(
        tls::uncovered_names(&generated.cert_pem, &extra).expect("parse"),
        extra
    );
    let wildcard = self_signed("0.0.0.0", &[]);
    assert_eq!(
        tls::uncovered_names(
            &wildcard.cert_pem,
            &["0.0.0.0".to_owned(), "100.64.0.10".to_owned()]
        )
        .expect("parse"),
        ["0.0.0.0", "100.64.0.10"]
    );

    let reloaded = tls::load(
        &self_signed_settings(
            generated.dir.path(),
            &["hub.example.test", "100.64.0.11", "new.example.test"],
        ),
        "100.64.0.10",
    )
    .expect("reuse")
    .expect("pair");
    assert_eq!(reloaded.missing_sans, ["new.example.test"]);
    assert_eq!(reloaded.fingerprint, generated.loaded.fingerprint);

    for san in ["0.0.0.0", "::"] {
        let error = parse_hub_database_bootstrap(&format!(
            "front_door:\n  tls:\n    mode: self-signed\n    sans: ['{san}']\n"
        ))
        .unwrap_err();
        assert!(error.to_string().contains("unspecified address"), "{error}");
    }
}

/// A spawned `gdaemon serve`, killed on drop.
#[cfg(unix)]
struct TlsServe(std::process::Child);

#[cfg(unix)]
impl Drop for TlsServe {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

/// Spawn `gdaemon serve` with `home` as `GOBBY_HOME`, stderr captured in
/// `log`, and wait until both public ports accept; returns the captured stderr.
#[cfg(unix)]
fn spawn_tls_serve(home: &std::path::Path, ports: [u16; 2], log: &str) -> (TlsServe, String) {
    use std::process::{Command, Stdio};

    let log = home.join(log);
    let mut child = TlsServe(
        Command::new(env!("CARGO_BIN_EXE_gdaemon"))
            .arg("serve")
            .env("GOBBY_HOME", home)
            .env("GOBBY_FRONT_DOOR_SECRET", "test-front-door-secret")
            .env_remove("GOBBY_PARENT_FD")
            .stdin(Stdio::null())
            .stderr(Stdio::from(std::fs::File::create(&log).expect("log file")))
            .spawn()
            .expect("spawn gdaemon serve"),
    );
    let deadline = Instant::now() + TIMEOUT;
    while !ports
        .iter()
        .all(|port| std::net::TcpStream::connect(("127.0.0.1", *port)).is_ok())
    {
        if child.0.try_wait().expect("poll serve").is_some() {
            panic!(
                "serve exited before binding: {}",
                std::fs::read_to_string(&log).unwrap_or_default()
            );
        }
        assert!(Instant::now() < deadline, "serve did not bind {ports:?}");
        std::thread::sleep(Duration::from_millis(50));
    }
    (child, std::fs::read_to_string(&log).expect("read log"))
}

#[cfg(unix)]
fn printed_fingerprint(stderr: &str) -> String {
    stderr
        .lines()
        .find_map(|line| line.strip_prefix("front door certificate "))
        .unwrap_or_else(|| panic!("no fingerprint line in {stderr:?}"))
        .to_owned()
}

#[cfg(unix)]
#[tokio::test]
async fn self_signed_generated_once_and_reused() {
    use std::os::unix::fs::PermissionsExt;

    let mut ports = Vec::new();
    let mut held_backends = Vec::new();
    while ports.len() < 2 {
        let probe = std::net::TcpListener::bind("127.0.0.1:0").expect("bind probe");
        let port = probe.local_addr().expect("probe addr").port();
        if port > 65435
            || [60891, 60892].contains(&port)
            || [60791, 60792].contains(&port)
            || ports.contains(&port)
            || ports.contains(&(port + 100))
        {
            continue;
        }
        // Keep the derived backend unavailable throughout both daemon launches.
        let backend = tokio::net::TcpSocket::new_v4().expect("backend socket");
        if backend
            .bind(SocketAddr::from(([127, 0, 0, 1], port + 100)))
            .is_err()
        {
            continue;
        }
        held_backends.push(backend);
        ports.push(port);
    }
    let ports = [ports[0], ports[1]];
    let home = tempfile::tempdir().expect("tempdir");
    let cert = home.path().join("tls").join("front_door.crt");
    let key = home.path().join("tls").join("front_door.key");
    let write_bootstrap = |sans: &str| {
        std::fs::write(
            home.path().join("bootstrap.yaml"),
            format!(
                "bind_host: 127.0.0.1\ndaemon_port: {}\nwebsocket_port: {}\nfront_door:\n  tls:\n    \
                 mode: self-signed\n    cert: {}\n    key: {}\n    sans: [{sans}]\n",
                ports[0],
                ports[1],
                cert.display(),
                key.display()
            ),
        )
        .expect("write bootstrap");
    };

    write_bootstrap("hub.example.test");
    let (first, stderr) = spawn_tls_serve(home.path(), ports, "first.log");
    let fingerprint = printed_fingerprint(&stderr);
    for path in [&cert, &key] {
        let mode = std::fs::metadata(path)
            .expect("generated file")
            .permissions()
            .mode();
        assert_eq!(mode & 0o777, 0o600, "{}", path.display());
    }
    let cert_pem = std::fs::read(&cert).expect("read cert");
    assert_eq!(
        fingerprint,
        tls::fingerprint(&cert_pem).expect("fingerprint")
    );
    drop(first);

    write_bootstrap("hub.example.test, new.example.test");
    let (_second, stderr) = spawn_tls_serve(home.path(), ports, "second.log");
    assert_eq!(printed_fingerprint(&stderr), fingerprint);
    assert_eq!(std::fs::read(&cert).expect("reread cert"), cert_pem);
    assert!(
        stderr.contains("front_door.tls.sans entry new.example.test is missing"),
        "{stderr}"
    );

    let stream = tls_connect(SocketAddr::from(([127, 0, 0, 1], ports[0])), &cert_pem)
        .await
        .expect("pinned handshake with the reused pair");
    let response = tokio::time::timeout(TIMEOUT, send_on(stream, get("/api/health")))
        .await
        .expect("pinned request timed out");
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
}
