#![cfg(unix)]

mod common;

use std::convert::Infallible;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};

use bytes::Bytes;
use common::{
    TIMEOUT, TestCert, WsCapture, frame, read_exact_into, read_head, self_signed, ws_upgrade,
};
use gobby_core::bootstrap::parse_hub_database_bootstrap;
use gobby_daemon::{
    front_door::{FrontDoor, relay::HubRelay},
    lifecycle::pid_file::PidFileClaim,
    serve::serve_doors,
    state::NodeServices,
};
use http_body_util::{BodyExt, Full};
use hyper::{Request, Response, StatusCode, service::service_fn};
use hyper_util::rt::TokioIo;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::{mpsc, oneshot},
    task::JoinHandle,
};
use tokio_rustls::TlsAcceptor;

const KEY: &str = "gobby_003aUlTJC7tjlCTQj2uNU3MFagCXG9LRKRcwGkBIDlf1Yo7hP";

struct Hub {
    addr: SocketAddr,
    cert: TestCert,
    connections: Arc<AtomicUsize>,
    seen: mpsc::UnboundedReceiver<serde_json::Value>,
    task: JoinHandle<()>,
}

impl Drop for Hub {
    fn drop(&mut self) {
        self.task.abort();
    }
}

async fn hub() -> Hub {
    let cert = self_signed("127.0.0.1", &[]);
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("hub bind");
    let addr = listener.local_addr().expect("hub address");
    let connections = Arc::new(AtomicUsize::new(0));
    let count = connections.clone();
    let acceptor = TlsAcceptor::from(cert.loaded.config.clone());
    let (sent, seen) = mpsc::unbounded_channel();
    let task = tokio::spawn(async move {
        loop {
            let (stream, _) = listener.accept().await.expect("hub accept");
            count.fetch_add(1, Ordering::Relaxed);
            let acceptor = acceptor.clone();
            let sent = sent.clone();
            tokio::spawn(async move {
                let Ok(stream) = acceptor.accept(stream).await else {
                    return;
                };
                let service = service_fn(move |request: Request<hyper::body::Incoming>| {
                    let sent = sent.clone();
                    async move {
                        let path = request.uri().to_string();
                        let headers = request.headers().clone();
                        let body = request
                            .into_body()
                            .collect()
                            .await
                            .expect("hub body")
                            .to_bytes();
                        let value = serde_json::json!({
                            "path": path,
                            "host": headers.get("host").and_then(|v| v.to_str().ok()),
                            "authorization": headers.get("authorization")
                                .and_then(|v| v.to_str().ok()),
                            "break_glass": headers.get("x-gobby-break-glass")
                                .and_then(|v| v.to_str().ok()),
                            "key_id": headers.get("x-gobby-key-id").and_then(|v| v.to_str().ok()),
                            "body": String::from_utf8(body.to_vec()).expect("UTF-8 body"),
                        });
                        sent.send(value.clone()).expect("capture request");
                        Ok::<_, Infallible>(Response::new(Full::new(Bytes::from(
                            value.to_string(),
                        ))))
                    }
                });
                let _ = hyper::server::conn::http1::Builder::new()
                    .serve_connection(TokioIo::new(stream), service)
                    .await;
            });
        }
    });
    Hub {
        addr,
        cert,
        connections,
        seen,
        task,
    }
}

struct Node {
    addr: SocketAddr,
    task: JoinHandle<()>,
    _claim: PidFileClaim,
    _home: tempfile::TempDir,
}

impl Drop for Node {
    fn drop(&mut self) {
        self.task.abort();
    }
}

fn write_bootstrap(
    home: &Path,
    origin: &str,
    pin: Option<&Path>,
    ports: Option<[u16; 2]>,
) -> PathBuf {
    let pin = pin.map_or(String::new(), |path| {
        format!("hub_cert: {}\n", path.display())
    });
    let ports = ports.map_or(String::new(), |ports| {
        format!("daemon_port: {}\nwebsocket_port: {}\n", ports[0], ports[1])
    });
    let path = home.join("bootstrap.yaml");
    std::fs::write(
        &path,
        format!(
            "datastore_mode: remote\nhub_daemon_url: {origin}\napi_key: {KEY}\n\
             bind_host: 127.0.0.1\n{pin}{ports}"
        ),
    )
    .expect("node bootstrap");
    path
}

async fn node(origin: &str, pin: Option<&Path>, ws_listener: bool) -> Node {
    let home = tempfile::tempdir().expect("node home");
    let path = write_bootstrap(home.path(), origin, pin, None);
    let bootstrap =
        parse_hub_database_bootstrap(&std::fs::read_to_string(&path).expect("read node"))
            .expect("parse node")
            .expect("node config");
    let (services, claim) = NodeServices::prepare(&bootstrap, &path, home.path())
        .expect("prepare node")
        .expect("node claim");
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("node bind");
    let addr = listener.local_addr().expect("node address");
    let task = tokio::spawn(async move {
        serve_doors(
            vec![(listener, FrontDoor::node(services, ws_listener))],
            None,
            std::future::pending(),
        )
        .await
        .expect("node serve");
    });
    Node {
        addr,
        task,
        _claim: claim,
        _home: home,
    }
}

async fn send(addr: SocketAddr, request: Request<Full<Bytes>>) -> (StatusCode, serde_json::Value) {
    tokio::time::timeout(TIMEOUT, async {
        let stream = TcpStream::connect(addr).await.expect("connect node");
        let (mut client, connection) = hyper::client::conn::http1::handshake(TokioIo::new(stream))
            .await
            .expect("client handshake");
        let task = tokio::spawn(connection);
        let response = client.send_request(request).await.expect("node response");
        let status = response.status();
        let body = response
            .into_body()
            .collect()
            .await
            .expect("response body")
            .to_bytes();
        task.abort();
        (
            status,
            serde_json::from_slice(&body).expect("response JSON"),
        )
    })
    .await
    .expect("request deadline")
}

fn get(path: &str) -> Request<Full<Bytes>> {
    Request::get(path)
        .header("host", "node.invalid")
        .header("authorization", format!("Bearer {KEY}"))
        .body(Full::new(Bytes::new()))
        .expect("get request")
}

#[tokio::test]
async fn node_relays_over_pinned_tls() {
    let mut hub = hub().await;
    let origin = format!("https://{}", hub.addr);
    let front = node(
        &origin,
        Some(&hub.cert.dir.path().join("front_door.crt")),
        false,
    )
    .await;
    for path in ["/api/one?x=1", "/api/two?y=2"] {
        let (status, body) = send(front.addr, get(path)).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["path"], path);
        assert_eq!(body["authorization"], format!("Bearer {KEY}"));
        assert_eq!(body["host"], hub.addr.to_string());
        assert!(body["key_id"].is_null(), "node authenticated the key");
        let seen = tokio::time::timeout(TIMEOUT, hub.seen.recv())
            .await
            .expect("hub capture deadline")
            .expect("hub request");
        assert_eq!(seen, body);
    }
    assert_eq!(
        hub.connections.load(Ordering::Relaxed),
        1,
        "TLS connection was not reused"
    );
    let refused = common::refused_addr().await;
    let refused_origin = format!("https://{refused}");
    let down = node(
        &refused_origin,
        Some(&hub.cert.dir.path().join("front_door.crt")),
        false,
    )
    .await;
    let (status, body) = send(down.addr, get("/api/health")).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body["backend"]["target"], refused_origin);
}

#[tokio::test]
async fn node_refuses_an_unpinned_hub() {
    let mut hub = hub().await;
    let wrong = self_signed("127.0.0.1", &[]);
    let node = node(
        &format!("https://{}", hub.addr),
        Some(&wrong.dir.path().join("front_door.crt")),
        false,
    )
    .await;
    let (status, _) = send(node.addr, get("/api/secret")).await;
    assert_eq!(status, StatusCode::BAD_GATEWAY);
    assert!(
        hub.seen.try_recv().is_err(),
        "unpinned hub received the request"
    );
}

async fn ws_hub(
    expected: usize,
    reply: Vec<u8>,
) -> (TestCert, SocketAddr, oneshot::Receiver<WsCapture>) {
    let cert = self_signed("127.0.0.1", &[]);
    let acceptor = TlsAcceptor::from(cert.loaded.config.clone());
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("WS hub bind");
    let addr = listener.local_addr().expect("WS hub address");
    let (done, capture) = oneshot::channel();
    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.expect("WS hub accept");
        let mut stream = acceptor.accept(stream).await.expect("WS hub TLS");
        let (request_head, mut received) = read_head(&mut stream).await;
        stream
            .write_all(
                b"HTTP/1.1 101 Switching Protocols\r\nupgrade: websocket\r\n\
                  connection: Upgrade\r\nsec-websocket-accept: stub\r\n\
                  sec-websocket-extensions: permessage-deflate\r\n\r\n",
            )
            .await
            .expect("WS 101");
        while received.len() < expected {
            let mut chunk = [0u8; 4096];
            let count = stream.read(&mut chunk).await.expect("WS hub read");
            assert_ne!(count, 0, "WS client closed before frames");
            received.extend_from_slice(&chunk[..count]);
        }
        stream.write_all(&reply).await.expect("WS hub reply");
        stream.shutdown().await.expect("WS hub shutdown");
        assert!(
            done.send(WsCapture {
                request_head,
                received
            })
            .is_ok(),
            "WS capture receiver closed"
        );
    });
    (cert, addr, capture)
}

fn corpus_frames(masked: bool) -> Vec<u8> {
    let dir =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/terminal_ws_golden");
    let manifest: serde_json::Value =
        serde_json::from_slice(&std::fs::read(dir.join("manifest.json")).expect("golden manifest"))
            .expect("manifest JSON");
    let names = manifest["fixtures"].as_array().expect("fixtures");
    assert!(!names.is_empty());
    let mut frames = Vec::new();
    for name in names {
        frames.extend(frame(
            0x81,
            &std::fs::read(dir.join(name.as_str().expect("fixture name"))).expect("golden payload"),
            masked,
        ));
    }
    frames.extend(frame(0x88, &1000u16.to_be_bytes(), masked));
    frames
}

async fn ws_roundtrip(ws_listener: bool, extra: &str) -> String {
    let outgoing = corpus_frames(true);
    let incoming = corpus_frames(false);
    let (cert, addr, capture) = ws_hub(outgoing.len(), incoming.clone()).await;
    let node = node(
        &format!("https://{addr}"),
        Some(&cert.dir.path().join("front_door.crt")),
        ws_listener,
    )
    .await;
    let mut stream = TcpStream::connect(node.addr)
        .await
        .expect("WS node connect");
    let (head, mut received) = tokio::time::timeout(
        TIMEOUT,
        ws_upgrade(
            &mut stream,
            node.addr,
            "/api/terminals/ws?token=opaque",
            extra,
        ),
    )
    .await
    .expect("WS upgrade deadline");
    assert!(head.starts_with("HTTP/1.1 101"), "{head}");
    assert!(
        head.to_ascii_lowercase()
            .contains("sec-websocket-extensions: permessage-deflate")
    );
    stream.write_all(&outgoing).await.expect("send corpus");
    tokio::time::timeout(
        TIMEOUT,
        read_exact_into(&mut stream, &mut received, incoming.len()),
    )
    .await
    .expect("WS read deadline");
    let seen = tokio::time::timeout(TIMEOUT, capture)
        .await
        .expect("WS capture deadline")
        .expect("WS capture result");
    assert_eq!(seen.received, outgoing);
    assert_eq!(received, incoming);
    assert!(
        seen.request_head.contains(&format!("host: {addr}\r\n")),
        "{}",
        seen.request_head
    );
    let path = if ws_listener {
        "/ws?token=opaque"
    } else {
        "/api/terminals/ws?token=opaque"
    };
    assert!(
        seen.request_head
            .starts_with(&format!("GET {path} HTTP/1.1\r\n")),
        "{}",
        seen.request_head
    );
    seen.request_head
}

#[tokio::test]
async fn node_relays_ws_over_pinned_tls() {
    ws_roundtrip(false, "").await;
    ws_roundtrip(true, "").await;
    for (origin, host, dial) in [
        ("https://hub.example", "hub.example", "hub.example:443"),
        ("http://hub.example", "hub.example", "hub.example:80"),
        ("https://[::1]:7443", "::1", "[::1]:7443"),
        ("http://[::1]", "::1", "[::1]:80"),
    ] {
        let cert = self_signed("127.0.0.1", &[]);
        let relay = HubRelay::new(origin, Some(&cert.cert_pem)).expect("origin parse");
        assert_eq!(relay.host, host);
        assert_eq!(relay.dial, dial);
    }
}

#[tokio::test]
async fn node_relays_managed_challenge_and_answers_interactive_natively() {
    let mut hub = hub().await;
    let node = node(
        &format!("https://{}", hub.addr),
        Some(&hub.cert.dir.path().join("front_door.crt")),
        false,
    )
    .await;
    let body = r#"{"nonce":"aGVsbG8","kind":"interactive"}"#;
    let challenge = |body: &str| {
        Request::post("/api/runtime/handshake/challenge")
            .header("host", "node.invalid")
            .body(Full::new(Bytes::copy_from_slice(body.as_bytes())))
            .expect("challenge")
    };
    let (status, proof) = send(node.addr, challenge(body)).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        proof["proof"],
        "4163c345e4d8622b215e6153831f84cd9f9f5d1dbac5f27bd3760568c3e3ee73"
    );
    assert_eq!(hub.connections.load(Ordering::Relaxed), 0);
    assert!(hub.seen.try_recv().is_err());
    let body = r#"{"nonce":"aGVsbG8","kind":"managed","claims":{}}"#;
    let (status, response) = send(node.addr, challenge(body)).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(response["path"], "/api/runtime/handshake/challenge");
    assert_eq!(response["body"], body);
}

#[tokio::test]
async fn node_startup_requires_a_pinned_hub() {
    let home = tempfile::tempdir().expect("missing pin home");
    let path = write_bootstrap(home.path(), "https://127.0.0.1:1", None, None);
    let bootstrap =
        parse_hub_database_bootstrap(&std::fs::read_to_string(&path).expect("bootstrap read"))
            .expect("parse")
            .expect("bootstrap");
    let error = NodeServices::prepare(&bootstrap, &path, home.path())
        .err()
        .expect("missing pin refusal");
    assert!(error.to_string().contains("gobby auth login"));
    assert!(
        error
            .to_string()
            .contains("has no hub_cert for an https hub")
    );
    assert!(!home.path().join("gobby.pid.lock").exists());
    let plain = node(
        "http://127.0.0.1:1",
        Some(Path::new("/missing/ignored/pin")),
        false,
    )
    .await;
    let (status, _) = send(plain.addr, get("/api/health")).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);

    let hub = hub().await;
    let home = tempfile::tempdir().expect("spawned node home");
    let first = std::net::TcpListener::bind("127.0.0.1:0").expect("HTTP probe");
    let second = std::net::TcpListener::bind("127.0.0.1:0").expect("WS probe");
    let ports = [
        first.local_addr().expect("HTTP port").port(),
        second.local_addr().expect("WS port").port(),
    ];
    write_bootstrap(
        home.path(),
        &format!("https://{}", hub.addr),
        Some(&hub.cert.dir.path().join("front_door.crt")),
        Some(ports),
    );
    drop((first, second));
    let log_path = home.path().join("serve.log");
    let mut child = tokio::process::Command::new(env!("CARGO_BIN_EXE_gdaemon"))
        .kill_on_drop(true)
        .arg("serve")
        .env("GOBBY_HOME", home.path())
        .env_remove("GOBBY_PARENT_FD")
        .env_remove("GOBBY_SERVICE_LAUNCH")
        .env_remove("GOBBY_SERVICE_NONCE")
        .stderr(std::fs::File::create(&log_path).expect("serve log"))
        .spawn()
        .expect("spawn node");
    let stream = tokio::time::timeout(TIMEOUT, async {
        loop {
            assert!(
                child.try_wait().expect("poll serve").is_none(),
                "serve exited: {}",
                std::fs::read_to_string(&log_path).expect("stderr")
            );
            if let Ok(stream) = TcpStream::connect(("127.0.0.1", ports[0])).await {
                break stream;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("serve startup deadline");
    drop(stream);
    let (status, body) = send(
        ([127, 0, 0, 1], ports[0]).into(),
        get("/api/spawned?node=1"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["path"], "/api/spawned?node=1");
    child.kill().await.expect("stop node");
}

#[tokio::test]
async fn node_strips_break_glass_before_the_hub() {
    let hub = hub().await;
    let node = node(
        &format!("https://{}", hub.addr),
        Some(&hub.cert.dir.path().join("front_door.crt")),
        false,
    )
    .await;
    let mut request = get("/api/mcp/servers");
    request
        .headers_mut()
        .insert("x-gobby-break-glass", "privileged".parse().expect("header"));
    let (status, body) = send(node.addr, request).await;
    assert_eq!(status, StatusCode::OK);
    assert!(body["break_glass"].is_null());
    let head = ws_roundtrip(true, "x-gobby-break-glass: privileged\r\n").await;
    assert!(
        !head.to_ascii_lowercase().contains("x-gobby-break-glass"),
        "{head}"
    );
}
