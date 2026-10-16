#![cfg(unix)]

use std::net::TcpListener;
use std::path::Path;
use std::process::Stdio;
use std::time::{Duration, Instant};

use bytes::Bytes;
use gobby_core::bootstrap::parse_hub_database_bootstrap;
use gobby_daemon::state::{NodeServices, RuntimeState};
use hmac::{Hmac, Mac};
use http_body_util::{BodyExt, Full};
use hyper::{Request, StatusCode};
use hyper_util::rt::TokioIo;
use sha2::Sha256;
use tokio::process::{Child, Command};

const KEY: &str = "gobby_003aUlTJC7tjlCTQj2uNU3MFagCXG9LRKRcwGkBIDlf1Yo7hP";
const HUB: &str = "http://127.0.0.1:1";
const TIMEOUT: Duration = Duration::from_secs(10);

fn bootstrap(text: &str) -> gobby_core::bootstrap::HubDatabaseBootstrap {
    parse_hub_database_bootstrap(text)
        .expect("parse bootstrap")
        .expect("bootstrap")
}

#[test]
fn node_prepare_takes_only_the_claim() {
    let node = bootstrap(&format!(
        "datastore_mode: remote\nhub_daemon_url: {HUB}\napi_key: {KEY}\n"
    ));
    let home = tempfile::tempdir().expect("node home");
    let path = home.path().join("bootstrap.yaml");
    let (services, claim) = NodeServices::prepare(&node, &path, home.path())
        .expect("prepare node")
        .expect("PID claim");
    assert_eq!(services.bootstrap_path, path);
    assert_eq!(services.relay.origin, HUB);
    assert!(claim.fileno().is_some());
    assert!(home.path().join("gobby.pid").exists());
    drop(claim);

    let home = tempfile::tempdir().expect("unenrolled home");
    let path = home.path().join("bootstrap.yaml");
    let mut unenrolled = node.clone();
    unenrolled.api_key = None;
    let error = NodeServices::prepare(&unenrolled, &path, home.path())
        .err()
        .expect("missing key refusal");
    assert_eq!(
        error.to_string(),
        format!(
            "node bootstrap {} has no api_key; run `gobby auth login` to enroll",
            path.display()
        )
    );
    assert!(
        !home.path().join("gobby.pid.lock").exists(),
        "unenrolled node took a claim"
    );

    let home = tempfile::tempdir().expect("runtime node home");
    let error = RuntimeState::prepare(&node, home.path(), "secret", None)
        .err()
        .expect("native node refusal");
    assert_eq!(
        error.to_string(),
        "a node takes no native runtime; NodeServices::prepare claims its PID file"
    );
    assert!(
        !home.path().join("gobby.pid.lock").exists(),
        "native node path took a claim"
    );

    for text in [
        "datastore_mode: local\n",
        "datastore_mode: local\nhub: true\n",
    ] {
        let home = tempfile::tempdir().expect("native home");
        let error = RuntimeState::prepare(&bootstrap(text), home.path(), "secret", None)
            .err()
            .expect("missing database refusal");
        assert_eq!(
            error.to_string(),
            "native runtime ownership requires bootstrap database_url"
        );
    }
}

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .expect("port probe")
        .local_addr()
        .expect("probe address")
        .port()
}

struct ServeChild(Child);

fn spawn(home: &Path, log: &str) -> ServeChild {
    let stderr = std::fs::File::create(home.join(log)).expect("stderr log");
    let marker_command = serde_json::to_string(&[
        "/usr/bin/touch",
        home.join("backend-started").to_str().expect("marker path"),
    ])
    .expect("backend command");
    ServeChild(
        Command::new(env!("CARGO_BIN_EXE_gdaemon"))
            .kill_on_drop(true)
            .arg("serve")
            .env("GOBBY_HOME", home)
            .env("GOBBY_BACKEND_COMMAND", marker_command)
            .env_remove("GOBBY_PARENT_FD")
            .env_remove("GOBBY_SERVICE_LAUNCH")
            .env_remove("GOBBY_SERVICE_NONCE")
            .stdout(Stdio::null())
            .stderr(stderr)
            .spawn()
            .expect("spawn serve"),
    )
}

async fn ready(child: &mut ServeChild, port: u16, home: &Path, log: &str) -> bool {
    let deadline = Instant::now() + TIMEOUT;
    loop {
        if child.0.try_wait().expect("poll serve").is_some() {
            return false;
        }
        if tokio::net::TcpStream::connect(("127.0.0.1", port))
            .await
            .is_ok()
        {
            return true;
        }
        assert!(
            Instant::now() < deadline,
            "serve did not bind: {}",
            std::fs::read_to_string(home.join(log)).expect("read stderr")
        );
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
}

async fn request(port: u16, request: Request<Full<Bytes>>) -> (StatusCode, serde_json::Value) {
    let stream = tokio::net::TcpStream::connect(("127.0.0.1", port))
        .await
        .expect("connect");
    let (mut client, connection) = hyper::client::conn::http1::handshake(TokioIo::new(stream))
        .await
        .expect("handshake");
    let connection = tokio::spawn(connection);
    let response = tokio::time::timeout(TIMEOUT, client.send_request(request))
        .await
        .expect("response deadline")
        .expect("response");
    let status = response.status();
    let body = response
        .into_body()
        .collect()
        .await
        .expect("body")
        .to_bytes();
    connection.abort();
    (status, serde_json::from_slice(&body).expect("JSON body"))
}

async fn challenge(port: u16) {
    let challenge_request = Request::post("/api/runtime/handshake/challenge")
        .header("host", "localhost")
        .body(Full::new(Bytes::from_static(
            br#"{"kind":"interactive","nonce":"aGVsbG8"}"#,
        )))
        .expect("challenge");
    let (status, body) = request(port, challenge_request).await;
    let mut key = Hmac::<Sha256>::new_from_slice(KEY.as_bytes()).expect("HMAC key");
    key.update(b"gobby-interactive-proof-v1");
    let mut proof =
        Hmac::<Sha256>::new_from_slice(&key.finalize().into_bytes()).expect("proof key");
    proof.update(b"hello");
    let proof: String = proof
        .finalize()
        .into_bytes()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body, serde_json::json!({"proof": proof}));
}

fn write_node(home: &Path, ports: [u16; 2], extra: &str) {
    std::fs::write(
        home.join("bootstrap.yaml"),
        format!(
            "datastore_mode: remote\nhub_daemon_url: {HUB}\napi_key: {KEY}\n\
             bind_host: 127.0.0.1\ndaemon_port: {}\nwebsocket_port: {}\n{extra}",
            ports[0], ports[1]
        ),
    )
    .expect("write bootstrap");
}

#[tokio::test]
async fn node_serve_takes_only_the_claim() {
    let home = tempfile::tempdir().expect("home");
    let probe = TcpListener::bind("127.0.0.1:0").expect("HTTP port probe");
    let ports = [
        probe.local_addr().expect("HTTP address").port(),
        free_port(),
    ];
    drop(probe);
    write_node(
        home.path(),
        ports,
        "database_url: not a PostgreSQL DSN\nfront_door:\n  routes:\n    terminal_ws: native\n",
    );
    let mut first = spawn(home.path(), "first.log");
    assert!(
        ready(&mut first, ports[0], home.path(), "first.log").await,
        "node exited: {}",
        std::fs::read_to_string(home.path().join("first.log")).expect("log")
    );
    challenge(ports[0]).await;
    let bearer = Request::get("/api/mcp/servers")
        .header("host", "localhost")
        .header("authorization", format!("Bearer {KEY}"))
        .body(Full::new(Bytes::new()))
        .expect("bearer request");
    let (status, body) = request(ports[0], bearer).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body["backend"]["target"], HUB);
    assert_eq!(body["status"], "unavailable");
    assert!(first.0.try_wait().expect("poll first").is_none());
    assert!(!home.path().join("backend-started").exists());
    let mut second = spawn(home.path(), "second.log");
    let status = tokio::time::timeout(TIMEOUT, second.0.wait())
        .await
        .expect("second node exit deadline")
        .expect("second node status");
    assert!(
        status.success(),
        "second node failed: {}",
        std::fs::read_to_string(home.path().join("second.log")).expect("log")
    );
    assert!(first.0.try_wait().expect("first still running").is_none());
}

#[tokio::test]
async fn node_serve_derives_no_backend_ports() {
    let home = tempfile::tempdir().expect("node home");
    let websocket_port = free_port();
    write_node(home.path(), [65535, websocket_port], "");
    let mut node = spawn(home.path(), "node.log");
    if ready(&mut node, websocket_port, home.path(), "node.log").await {
        challenge(websocket_port).await;
    } else {
        let log = std::fs::read_to_string(home.path().join("node.log")).expect("node log");
        assert!(log.contains("failed to bind"), "unexpected refusal: {log}");
    }
    let log = std::fs::read_to_string(home.path().join("node.log")).expect("node log");
    assert!(
        !log.contains("exceed 65535"),
        "node derived backend ports: {log}"
    );

    let home = tempfile::tempdir().expect("standalone home");
    std::fs::write(
        home.path().join("bootstrap.yaml"),
        "datastore_mode: local\nbind_host: 127.0.0.1\ndaemon_port: 65500\n",
    )
    .expect("standalone bootstrap");
    let mut standalone = spawn(home.path(), "standalone.log");
    let status = tokio::time::timeout(TIMEOUT, standalone.0.wait())
        .await
        .expect("standalone exit deadline")
        .expect("standalone status");
    assert!(!status.success());
    let log = std::fs::read_to_string(home.path().join("standalone.log")).expect("standalone log");
    assert!(
        log.contains("exceed 65535"),
        "standalone overflow changed: {log}"
    );
}
