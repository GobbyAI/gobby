#![cfg(unix)]

mod common;

use std::convert::Infallible;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};

use anyhow::{Context, Result, ensure};
use bytes::Bytes;
use common::{
    TIMEOUT, TestCert, WsCapture, frame, read_exact_into, read_head, self_signed, ws_upgrade,
};
use futures_util::{FutureExt, SinkExt, StreamExt};
use gobby_core::bootstrap::parse_hub_database_bootstrap;
use gobby_core::postgres_pool::{Pool, PoolSettings};
use gobby_daemon::{
    front_door::routes::{FAMILIES, RouteTable},
    front_door::{
        FrontDoorState,
        auth::{AuthState, PostgresKeyResolver},
        health::BackendState,
    },
    nodes::{
        HubNodes, NativeRoutes,
        channel::{self, AckOutcome, ChannelTiming},
        registry::{PersistOutcome, Registry},
    },
};
use gobby_daemon::{
    front_door::{FrontDoor, relay::HubRelay},
    lifecycle::pid_file::PidFileClaim,
    serve::serve_doors,
    state::NodeServices,
};
use http_body_util::{BodyExt, Full};
use hyper::{Request, Response, StatusCode, service::service_fn};
use hyper_util::rt::TokioIo;
use tokio::time::{Duration, Instant, sleep, timeout, timeout_at};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::{mpsc, oneshot},
    task::JoinHandle,
};
use tokio_postgres::{Client as PgClient, NoTls};
use tokio_rustls::TlsAcceptor;
use tokio_tungstenite::{
    WebSocketStream,
    tungstenite::{Message as PeerMessage, client::IntoClientRequest},
};
use uuid::Uuid;

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

// Channel acceptance tests use real monotonic time and their own schema. Database
// waits must not auto-advance Tokio's timers while PostgreSQL is still running.
struct ChannelDb {
    admin: PgClient,
    url: String,
    schema: String,
    machine: Uuid,
    key_id: Uuid,
}

fn channel_timing() -> ChannelTiming {
    ChannelTiming {
        ping_interval: Duration::from_millis(40),
        backoff_start: Duration::from_millis(50),
        backoff_max: Duration::from_millis(200),
        attempt_timeout: Duration::from_millis(400),
        hello_timeout: Duration::from_secs(3),
        idle_timeout: Duration::from_secs(2),
        key_recheck: Duration::from_millis(600),
        recheck_deadline: Duration::from_millis(200),
        heartbeat_write: Duration::from_millis(150),
    }
}

async fn channel_database(case: impl AsyncFnOnce(&ChannelDb) -> Result<()>) -> Result<()> {
    let url = std::env::var("DATABASE_URL")?;
    let config: tokio_postgres::Config = url.parse()?;
    ensure!(
        config.get_dbname() == Some("gobby_test")
            && config.get_hosts() == [tokio_postgres::config::Host::Tcp("127.0.0.1".into())]
            && config.get_ports() == [60892]
            && std::env::var("GOBBY_TEST_PROTECT").as_deref() == Ok("1"),
        "node channel tests require the protected isolated test hub"
    );
    let (admin, driver) = config.connect(NoTls).await?;
    let driver = tokio::spawn(driver);
    timeout(
        Duration::from_secs(30),
        admin.simple_query("SELECT pg_advisory_lock(hashtext('gdaemon-node-channel-tests'), 1)"),
    )
    .await
    .context("waiting for node-channel fixture lock")??;
    let schema = format!("test_node_channel_{}", Uuid::new_v4().simple());
    let machine = Uuid::new_v4();
    let key_id = Uuid::new_v4();
    let hash = gobby_core::api_key_format::hash(KEY);
    admin
        .batch_execute(&format!(
            "CREATE SCHEMA {schema}; GRANT USAGE ON SCHEMA {schema} TO gobby_daemon_runtime; \
         CREATE TABLE {schema}.machines(id uuid PRIMARY KEY, ref integer, \
         hostname text, os text, label text, tailscale_name text, owner_user_id uuid, \
         first_seen timestamptz, last_seen timestamptz, \
         last_heartbeat_at timestamptz, node_version text); \
         CREATE TABLE {schema}.api_keys(id uuid PRIMARY KEY, \
         user_id uuid NOT NULL, machine_id uuid NOT NULL, \
         key_hash text NOT NULL, revoked_at timestamptz, last_used_at timestamptz); \
         GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA {schema} \
         TO gobby_daemon_runtime; \
         INSERT INTO {schema}.machines(id) VALUES ('{machine}'); \
         INSERT INTO {schema}.api_keys(id,user_id,machine_id,key_hash) \
         VALUES ('{key_id}','{}','{machine}','{hash}')",
            Uuid::new_v4()
        ))
        .await?;
    let separator = if url.contains('?') { '&' } else { '?' };
    let db = ChannelDb {
        admin,
        schema: schema.clone(),
        url: format!("{url}{separator}options=-csearch_path%3D{schema}"),
        machine,
        key_id,
    };
    let outcome = std::panic::AssertUnwindSafe(case(&db)).catch_unwind().await;
    db.admin.batch_execute("ROLLBACK").await?;
    let cleanup = db
        .admin
        .batch_execute(&format!("DROP SCHEMA {schema} CASCADE"))
        .await;
    drop(db);
    driver.abort();
    let _ = driver.await;
    match outcome {
        Ok(result) => {
            cleanup?;
            result
        }
        Err(panic) => {
            if let Err(error) = cleanup {
                eprintln!("channel cleanup: {error}");
            }
            std::panic::resume_unwind(panic)
        }
    }
}

impl ChannelDb {
    fn pool(&self) -> Result<Pool> {
        Ok(Pool::build(
            &self.url,
            PoolSettings {
                max_size: 4,
                application_name: "gobby-gdaemon-nodes-test".into(),
                acquire_timeout: Duration::from_secs(2),
            },
        )?)
    }

    async fn row(&self) -> Result<(Option<f64>, Option<String>)> {
        let row = self
            .admin
            .query_one(
                &format!(
                    "SELECT extract(epoch FROM last_heartbeat_at)::double precision,node_version \
                 FROM {}.machines WHERE id=$1::text::uuid",
                    self.schema
                ),
                &[&self.machine.to_string()],
            )
            .await?;
        Ok((row.get(0), row.get(1)))
    }

    async fn revoke(&self) -> Result<()> {
        // Commit independently of the transaction holding a machine-row lock.
        let (admin, driver) = tokio_postgres::connect(&self.url, NoTls).await?;
        let driver = tokio::spawn(driver);
        let result = admin
            .execute(
                &format!(
                    "UPDATE {}.api_keys SET revoked_at=now() WHERE id=$1::text::uuid",
                    self.schema
                ),
                &[&self.key_id.to_string()],
            )
            .await;
        drop(admin);
        driver.abort();
        let _ = driver.await;
        result?;
        Ok(())
    }

    async fn lock_machine(&self) -> Result<()> {
        self.admin
            .batch_execute(&format!(
                "BEGIN; SELECT id FROM {}.machines FOR UPDATE",
                self.schema
            ))
            .await?;
        Ok(())
    }

    async fn lock_keys(&self) -> Result<()> {
        self.admin
            .batch_execute(&format!(
                "BEGIN; LOCK TABLE {}.api_keys IN ACCESS EXCLUSIVE MODE",
                self.schema
            ))
            .await?;
        Ok(())
    }

    async fn unlock(&self) -> Result<()> {
        self.admin.batch_execute("COMMIT").await?;
        Ok(())
    }

    async fn wait_blocked(&self, relation: &str) -> Result<()> {
        timeout(TIMEOUT, async {
            let mut poll = tokio::time::interval(Duration::from_millis(5));
            loop {
                // The locking transaction otherwise retains its first activity snapshot.
                self.admin
                    .simple_query("SELECT pg_stat_clear_snapshot()")
                    .await?;
                let row = self
                    .admin
                    .query_one(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity \
                     WHERE application_name='gobby-gdaemon-nodes-test' \
                     AND wait_event_type='Lock' AND query LIKE $1)",
                        &[&format!("%{relation}%")],
                    )
                    .await?;
                if row.get::<_, bool>(0) {
                    return Ok::<_, anyhow::Error>(());
                }
                poll.tick().await;
            }
        })
        .await
        .with_context(|| format!("waiting for blocked {relation} query"))??;
        Ok(())
    }
}

struct ChannelHub {
    addr: SocketAddr,
    nodes: Arc<HubNodes>,
    task: JoinHandle<()>,
    _home: tempfile::TempDir,
}
impl Drop for ChannelHub {
    fn drop(&mut self) {
        self.task.abort();
    }
}

async fn channel_hub(db: &ChannelDb, timing: ChannelTiming) -> Result<ChannelHub> {
    channel_door(db, timing, None).await
}

async fn channel_door(
    db: &ChannelDb,
    timing: ChannelTiming,
    native: Option<NativeRoutes>,
) -> Result<ChannelHub> {
    let nodes = Arc::new(HubNodes {
        db: db.pool()?,
        registry: Registry::default(),
        timing,
    });
    warm_channel_pool(&nodes.db).await?;
    let home = tempfile::tempdir()?;
    let path = write_bootstrap(home.path(), "http://127.0.0.1:1", None, None);
    let auth = Arc::new(AuthState::new(
        "test-secret".into(),
        Some(Arc::new(PostgresKeyResolver::new(&db.url))),
        path,
    )?);
    let state = FrontDoorState {
        native: native.unwrap_or_else(|| NativeRoutes::Hub(nodes.clone())),
        ..FrontDoorState::new("127.0.0.1:1".parse()?, BackendState::Down, auth)
    };
    let table = RouteTable::new(FAMILIES, &Default::default(), &state);
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let addr = listener.local_addr()?;
    let task = tokio::spawn(async move {
        serve_doors(
            vec![(listener, FrontDoor::new(state, table))],
            None,
            std::future::pending(),
        )
        .await
        .expect("channel hub");
    });
    Ok(ChannelHub {
        addr,
        nodes,
        task,
        _home: home,
    })
}

#[tokio::test]
async fn machines_api_lists_rows_with_connection_state() -> Result<()> {
    channel_database(async |db| {
        let user: Uuid = db
            .admin
            .query_one(
                &format!("SELECT user_id FROM {}.api_keys WHERE id=$1", db.schema),
                &[&db.key_id],
            )
            .await?
            .get(0);
        let early = Uuid::from_u128(1);
        let tied = Uuid::from_u128(2);
        let foreign = Uuid::from_u128(3);
        db.admin
            .execute(
                &format!(
                    "UPDATE {}.machines SET owner_user_id=$1, ref=9, \
                hostname='node', os='macos', label='Desk', tailscale_name='desk.test', \
                first_seen='2026-01-03T00:00:00Z', last_seen='2026-01-04T01:02:03+02:00', \
                last_heartbeat_at='2026-01-05T00:00:00Z', node_version='0.4.19' WHERE id=$2",
                    db.schema
                ),
                &[&user, &db.machine],
            )
            .await?;
        db.admin
            .execute(
                &format!(
                    "INSERT INTO {}.machines(id,owner_user_id,first_seen) VALUES \
                ($1,$4,'2026-01-01T00:00:00Z'), ($2,$4,'2026-01-01T00:00:00Z'), \
                ($3,$5,'2025-01-01T00:00:00Z')",
                    db.schema
                ),
                &[&early, &tied, &foreign, &user, &Uuid::new_v4()],
            )
            .await?;
        let hub = channel_hub(db, channel_timing()).await?;
        let (pending, _pending_close) = hub.nodes.registry.register(tied);
        // An admitted slot must remain disconnected until its acknowledgement.
        let (status, body) = send(hub.addr, get("/api/machines")).await;
        assert_eq!(status, StatusCode::OK);
        let rows = body["machines"].as_array().expect("machine list");
        assert_eq!(rows.len(), 3);
        assert_eq!(
            rows.iter()
                .map(|row| row["id"].as_str().unwrap())
                .collect::<Vec<_>>(),
            vec![early.to_string(), tied.to_string(), db.machine.to_string()]
        );
        let fields = [
            "id",
            "ref",
            "hostname",
            "os",
            "label",
            "tailscale_name",
            "first_seen",
            "last_seen",
            "last_heartbeat_at",
            "node_version",
            "connected",
        ];
        for row in rows {
            let object = row.as_object().expect("machine object");
            assert_eq!(object.len(), fields.len());
            assert!(fields.iter().all(|field| object.contains_key(*field)));
            assert_eq!(row["connected"], false);
            for field in ["first_seen", "last_seen", "last_heartbeat_at"] {
                if let Some(value) = row[field].as_str() {
                    time::OffsetDateTime::parse(
                        value,
                        &time::format_description::well_known::Rfc3339,
                    )?;
                } else {
                    assert!(row[field].is_null());
                }
            }
        }
        for field in [
            "ref",
            "hostname",
            "os",
            "label",
            "tailscale_name",
            "last_seen",
            "last_heartbeat_at",
            "node_version",
        ] {
            assert!(rows[0][field].is_null(), "{field} must preserve null");
        }
        for (field, expected) in [
            ("hostname", "node"),
            ("os", "macos"),
            ("label", "Desk"),
            ("tailscale_name", "desk.test"),
            ("node_version", "0.4.19"),
        ] {
            assert_eq!(rows[2][field], expected);
        }
        assert_eq!(rows[2]["ref"], 9);
        assert_eq!(
            time::OffsetDateTime::parse(
                rows[2]["last_seen"].as_str().unwrap(),
                &time::format_description::well_known::Rfc3339
            )?,
            time::OffsetDateTime::parse(
                "2026-01-03T23:02:03Z",
                &time::format_description::well_known::Rfc3339
            )?
        );
        assert!(hub.nodes.registry.mark_live(tied, pending));
        let path = format!("/api/machines/{tied}");
        let (status, live) = send(hub.addr, get(&path)).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(live["id"], tied.to_string());
        assert_eq!(live["connected"], true);
        assert_eq!(live.as_object().unwrap().len(), 11);
        let (_, list) = send(hub.addr, get("/api/machines")).await;
        assert_eq!(list["machines"][1], live);
        hub.nodes.registry.remove(tied, pending);
        assert_eq!(send(hub.addr, get(&path)).await.1["connected"], false);
        for id in [
            foreign.to_string(),
            Uuid::new_v4().to_string(),
            "malformed".into(),
        ] {
            let (status, body) = send(hub.addr, get(&format!("/api/machines/{id}"))).await;
            assert_eq!(status, StatusCode::NOT_FOUND);
            assert_eq!(body["code"], "machine_not_found");
        }
        let mut peer = open_channel(hub.addr).await?;
        hello(&mut peer, "inventory-live").await?;
        ack(&mut peer, db.machine).await?;
        let machine_path = format!("/api/machines/{}", db.machine);
        let (status, machine) = send(hub.addr, get(&machine_path)).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(machine["connected"], true);
        assert_eq!(machine["node_version"], "inventory-live");
        let standalone = channel_door(
            db,
            channel_timing(),
            Some(NativeRoutes::Standalone(Some(Arc::new(db.pool()?)))),
        )
        .await?;
        let (_, body) = send(standalone.addr, get("/api/machines")).await;
        assert_eq!(body["machines"].as_array().unwrap().len(), 3);
        assert!(
            body["machines"]
                .as_array()
                .unwrap()
                .iter()
                .all(|row| row["connected"] == false)
        );
        drop(peer);
        timeout(TIMEOUT, async {
            let mut poll = tokio::time::interval(Duration::from_millis(5));
            while hub.nodes.registry.is_connected(db.machine) {
                poll.tick().await;
            }
        })
        .await?;
        assert_eq!(
            send(hub.addr, get(&machine_path)).await.1["connected"],
            false
        );
        Ok(())
    })
    .await
}

#[tokio::test]
async fn machines_api_requires_a_key() -> Result<()> {
    channel_database(async |db| {
        let hub = channel_hub(db, channel_timing()).await?;
        let standalone = channel_door(db, channel_timing(), Some(NativeRoutes::default())).await?;
        for addr in [hub.addr, standalone.addr] {
            for path in ["/api/machines", "/api/machines/malformed"] {
                let mut request = get(path);
                request.headers_mut().remove("authorization");
                request
                    .headers_mut()
                    .insert("x-gobby-user-id", Uuid::new_v4().to_string().parse()?);
                let (status, body) = send(addr, request).await;
                assert_eq!(status, StatusCode::UNAUTHORIZED);
                assert_eq!(body["code"], "missing_auth");
                assert!(body["error"].is_string());
            }
        }
        for path in ["/api/machines", "/api/machines/malformed"] {
            let (status, body) = send(standalone.addr, get(path)).await;
            assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
            assert_eq!(body["code"], "machines_unavailable");
        }
        let (status, body) = send(hub.addr, get("/api/machines")).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body, serde_json::json!({"machines": []}));
        let mut held = Vec::new();
        for _ in 0..4 {
            held.push(hub.nodes.db.get().await?);
        }
        for path in [
            "/api/machines".to_string(),
            format!("/api/machines/{}", db.machine),
        ] {
            let (status, body) = send(hub.addr, get(&path)).await;
            assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
            assert_eq!(body["code"], "machines_unavailable");
        }
        drop(held);
        db.admin
            .batch_execute(&format!(
                "ALTER TABLE {}.machines DROP COLUMN ref",
                db.schema
            ))
            .await?;
        for path in [
            "/api/machines".to_string(),
            format!("/api/machines/{}", db.machine),
        ] {
            let (status, body) = send(hub.addr, get(&path)).await;
            assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
            assert_eq!(body["code"], "machines_unavailable");
        }
        Ok(())
    })
    .await
}

async fn warm_channel_pool(pool: &Pool) -> Result<()> {
    // Create both connections before starting the channel's timed proof. A held
    // heartbeat must leave a ready connection for the independent key check.
    let (first, second) = tokio::try_join!(pool.get(), pool.get())?;
    drop((first, second));
    assert!(pool.status().available >= 2);
    Ok(())
}

type ChannelPeer = WebSocketStream<TcpStream>;
async fn open_channel(addr: SocketAddr) -> Result<ChannelPeer> {
    let mut request = format!("ws://{addr}/api/nodes/channel").into_client_request()?;
    request
        .headers_mut()
        .insert("authorization", format!("Bearer {KEY}").parse()?);
    let stream = TcpStream::connect(addr).await?;
    let result = timeout(TIMEOUT, tokio_tungstenite::client_async(request, stream)).await?;
    assert!(
        result.is_ok(),
        "key-authenticated channel must upgrade: {result:?}"
    );
    Ok(result?.0)
}
async fn hello(peer: &mut ChannelPeer, version: &str) -> Result<()> {
    peer.send(PeerMessage::Text(
        serde_json::json!({"type":"hello","node_version":version,"platform":"test/os"})
            .to_string()
            .into(),
    ))
    .await?;
    Ok(())
}
async fn ack(peer: &mut ChannelPeer, machine: Uuid) -> Result<()> {
    let frame = timeout(TIMEOUT, peer.next())
        .await
        .context("waiting for channel ack")?
        .expect("ack frame")?;
    let value: serde_json::Value = serde_json::from_str(frame.to_text()?)?;
    assert_eq!(
        value,
        serde_json::json!({"type":"ack","machine_id":machine.to_string()})
    );
    Ok(())
}
async fn close_code(peer: &mut ChannelPeer, code: u16) -> Result<()> {
    timeout(TIMEOUT, async {
        loop {
            match peer.next().await {
                Some(Ok(PeerMessage::Close(Some(frame)))) => {
                    assert_eq!(u16::from(frame.code), code);
                    return Ok(());
                }
                Some(Ok(PeerMessage::Pong(_))) => {}
                other => anyhow::bail!("expected close {code}, got {other:?}"),
            }
        }
    })
    .await
    .with_context(|| format!("waiting for channel close {code}"))?
}
async fn wait_registered(nodes: &HubNodes, machine: Uuid) -> Result<Uuid> {
    timeout(TIMEOUT, async {
        loop {
            if let Some(id) = nodes.registry.connection_id(machine) {
                return id;
            }
            sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .map_err(Into::into)
}

#[tokio::test]
async fn channel_registers_and_replaces() -> Result<()> {
    channel_database(async |db| {
        // Count actual writes so consecutive pings cannot hide a second update
        // behind the first row read. This audit exists only in the test schema.
        db.admin
            .batch_execute(&format!(
                "ALTER TABLE {}.machines ADD COLUMN heartbeat_writes integer NOT NULL DEFAULT 0; \
             CREATE FUNCTION {}.count_heartbeat() RETURNS trigger LANGUAGE plpgsql AS \
             'BEGIN NEW.heartbeat_writes := OLD.heartbeat_writes + 1; RETURN NEW; END'; \
             CREATE TRIGGER count_heartbeat BEFORE UPDATE ON {}.machines \
             FOR EACH ROW EXECUTE FUNCTION {}.count_heartbeat()",
                db.schema, db.schema, db.schema, db.schema
            ))
            .await?;
        let timing = channel_timing();
        let hub = channel_hub(db, timing).await?;
        let mut first = open_channel(hub.addr).await?;
        hello(&mut first, "first-version").await?;
        ack(&mut first, db.machine).await?;
        // Send the suppression ping before database observations consume its interval.
        first.send(PeerMessage::Ping(vec![1].into())).await?;
        assert!(hub.nodes.registry.is_connected(db.machine));
        assert_eq!(hub.nodes.registry.connected_ids(), vec![db.machine]);
        let initial = db.row().await?;
        assert!(initial.0.is_some());
        assert_eq!(initial.1.as_deref(), Some("first-version"));
        assert!(matches!(
            timeout(TIMEOUT, first.next()).await?.unwrap()?,
            PeerMessage::Pong(_)
        ));
        assert_eq!(db.row().await?, initial);
        tokio::time::sleep_until(Instant::now() + timing.heartbeat_write).await;
        first.send(PeerMessage::Ping(vec![2].into())).await?;
        first.send(PeerMessage::Ping(vec![3].into())).await?;
        let updated = timeout(TIMEOUT, async {
            let mut poll = tokio::time::interval(Duration::from_millis(5));
            loop {
                let row = db.row().await?;
                if row.0 > initial.0 {
                    break Ok::<_, anyhow::Error>(row);
                }
                poll.tick().await;
            }
        })
        .await??;
        assert_eq!(updated.1, initial.1);
        loop {
            let frame = timeout(TIMEOUT, first.next()).await?.unwrap()?;
            if frame == PeerMessage::Pong(vec![2].into()) {
                break;
            }
            assert!(matches!(frame, PeerMessage::Pong(_)));
        }
        assert_eq!(
            timeout(TIMEOUT, first.next()).await?.unwrap()?,
            PeerMessage::Pong(vec![3].into())
        );
        assert_eq!(db.row().await?, updated);
        let writes: i32 = db
            .admin
            .query_one(
                &format!(
                    "SELECT heartbeat_writes FROM {}.machines WHERE id=$1",
                    db.schema
                ),
                &[&db.machine],
            )
            .await?
            .get(0);
        assert_eq!(writes, 2, "hello and the due ping each write exactly once");
        let mut second = open_channel(hub.addr).await?;
        hello(&mut second, "successor").await?;
        ack(&mut second, db.machine).await?;
        close_code(&mut first, 4409).await?;
        assert!(hub.nodes.registry.is_connected(db.machine));
        assert_eq!(db.row().await?.1.as_deref(), Some("successor"));
        Ok(())
    })
    .await
}

#[tokio::test]
async fn revoked_key_closes_channel_on_heartbeat() -> Result<()> {
    channel_database(async |db| {
        // Keep real database acquisition and runtime scheduling inside D. A 200 ms
        // budget can expire before a cold connection is created on a busy runner.
        let mut timing = channel_timing();
        timing.key_recheck = Duration::from_secs(4);
        timing.recheck_deadline = Duration::from_secs(1);
        // The unproven-check peer stays silent through a tick, its D timeout,
        // and another R while proving recovery; idle must cover that sequence.
        timing.idle_timeout = Duration::from_secs(12);
        assert!(timing.recheck_deadline < timing.key_recheck * 3 / 4);
        assert!(timing.idle_timeout > timing.key_recheck * 2);
        // The first tick is the only tick within revocation + R + D.
        for pong_only in [false, true] {
            db.admin
                .batch_execute(&format!(
                    "UPDATE {}.api_keys SET revoked_at=NULL",
                    db.schema
                ))
                .await?;
            let hub = channel_hub(db, timing).await?;
            let mut peer = open_channel(hub.addr).await?;
            let upgraded = Instant::now();
            hello(&mut peer, "revocation").await?;
            ack(&mut peer, db.machine).await?;
            assert!(
                upgraded.elapsed() < timing.key_recheck / 4,
                "raw admission took {:?}, past the intended R/4 revocation",
                upgraded.elapsed()
            );
            tokio::time::sleep_until(upgraded + timing.key_recheck / 4).await;
            db.revoke().await?;
            let revoked = Instant::now();
            let mut pongs = tokio::time::interval(timing.key_recheck / 20);
            let mut sent = 0;
            let mut last_pong = revoked;
            timeout_at(
                revoked + timing.key_recheck + timing.recheck_deadline,
                async {
                    loop {
                        tokio::select! {
                            biased;
                            result = close_code(&mut peer, 4401) => break result,
                            _ = pongs.tick(), if pong_only => {
                                peer.send(PeerMessage::Pong(vec![1].into())).await?;
                                sent += 1;
                                last_pong = Instant::now();
                            },
                        }
                    }
                },
            )
            .await??;
            if pong_only {
                assert!(sent > 1, "Pongs must continue across the recheck window");
                assert!(last_pong >= upgraded + timing.key_recheck * 3 / 4);
            }
            assert!(!hub.nodes.registry.is_connected(db.machine));
        }
        db.admin
            .batch_execute(&format!(
                "UPDATE {}.api_keys SET revoked_at=NULL",
                db.schema
            ))
            .await?;
        let mut client_timing = timing;
        client_timing.heartbeat_write = timing.ping_interval * 2;
        let hub = channel_hub(db, client_timing).await?;
        // Observe the actual client's close frame while forwarding its scheduled pings.
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let observed_addr = listener.local_addr()?;
        let hub_addr = hub.addr;
        let (upgraded_tx, upgraded_rx) = tokio::sync::oneshot::channel();
        let (closed_tx, closed_rx) = tokio::sync::oneshot::channel();
        let mut observer = tokio::spawn(async move {
            let (stream, _) = listener.accept().await?;
            let mut upstream = open_channel(hub_addr).await?;
            let _ = upgraded_tx.send(Instant::now());
            let mut downstream = tokio_tungstenite::accept_async(stream).await?;
            loop {
                tokio::select! {
                    frame = downstream.next() => {
                        let frame = frame.ok_or_else(|| anyhow::anyhow!("node disconnected"))??;
                        upstream.send(frame).await?;
                    },
                    frame = upstream.next() => {
                        let frame = frame.ok_or_else(|| anyhow::anyhow!("hub disconnected"))??;
                        if let PeerMessage::Close(Some(close)) = &frame {
                            let _ = closed_tx.send((u16::from(close.code), Instant::now()));
                            downstream.send(frame).await?;
                            return Ok::<(), anyhow::Error>(());
                        }
                        downstream.send(frame).await?;
                    },
                }
            }
        });
        let home = tempfile::tempdir()?;
        let path = write_bootstrap(home.path(), &format!("http://{observed_addr}"), None, None);
        let bootstrap = parse_hub_database_bootstrap(&std::fs::read_to_string(&path)?)?.unwrap();
        let (node, _claim) = NodeServices::prepare(&bootstrap, &path, home.path())?.unwrap();
        // This observer authenticates an extra upstream connection before accepting
        // the client. Keep setup out of the short attempt budget used by the backoff
        // test; the admitted channel still uses the same R + D revocation bound.
        let mut observed_timing = timing;
        observed_timing.attempt_timeout = timing.hello_timeout;
        let mut client = tokio::spawn(channel::run_client(node, observed_timing));
        let upgraded = timeout(TIMEOUT, upgraded_rx)
            .await
            .context("node observer upgrade")??;
        tokio::select! {
            result = &mut observer => {
                result.context("observer task")?.context("observer before admission")?;
                anyhow::bail!("observer ended before admission");
            },
            result = &mut client => anyhow::bail!("node client ended before admission: {result:?}"),
            result = timeout(TIMEOUT, async {
                let mut poll = tokio::time::interval(Duration::from_millis(5));
                while !hub.nodes.registry.is_connected(db.machine) {
                    poll.tick().await;
                }
            }) => {
                result.context("node observer admission")?;
            }
        }
        db.lock_machine().await?;
        db.wait_blocked("machines").await?;
        eprintln!(
            "observed client admitted and heartbeat blocked after {:?} since upgrade; pool {:?}",
            upgraded.elapsed(),
            hub.nodes.db.status()
        );
        assert!(
            upgraded.elapsed() < timing.key_recheck / 4,
            "client setup passed the intended R/4 revocation"
        );
        tokio::time::sleep_until(upgraded + timing.key_recheck / 4).await;
        // Revocation uses a separate admin connection while the row lock persists.
        db.revoke().await?;
        let revoked = Instant::now();
        let observed_close = timeout_at(
            revoked + timing.key_recheck + timing.recheck_deadline,
            closed_rx,
        )
        .await;
        let (code, closed) = match observed_close {
            Ok(close) => close?,
            Err(error) => {
                db.admin
                    .simple_query("SELECT pg_stat_clear_snapshot()")
                    .await?;
                let activity = db
                    .admin
                    .query(
                        "SELECT state,wait_event_type,query FROM pg_stat_activity \
                     WHERE application_name='gobby-gdaemon-nodes-test'",
                        &[],
                    )
                    .await?
                    .into_iter()
                    .map(|row| {
                        (
                            row.get::<_, Option<String>>(0),
                            row.get::<_, Option<String>>(1),
                            row.get::<_, Option<String>>(2),
                        )
                    })
                    .collect::<Vec<_>>();
                anyhow::bail!(
                    "observing node client revoked close: {error}; \
                    connection={:?}; pool={:?}; observer_finished={}; activity={activity:?}",
                    hub.nodes.registry.connection_id(db.machine),
                    hub.nodes.db.status(),
                    observer.is_finished()
                );
            }
        };
        assert_eq!(code, 4401);
        assert!(closed <= revoked + timing.key_recheck + timing.recheck_deadline);
        timeout_at(
            revoked + timing.key_recheck + timing.recheck_deadline,
            async {
                let mut poll = tokio::time::interval(Duration::from_millis(5));
                while hub.nodes.registry.connection_id(db.machine).is_some() {
                    poll.tick().await;
                }
            },
        )
        .await
        .context("removing revoked node client from registry")?;
        client.abort();
        let _ = client.await;
        observer.await??;
        db.unlock().await?;
        assert!(!hub.nodes.registry.is_connected(db.machine));
        // A timed-out key lookup is unproven and must keep an admitted channel.
        db.admin
            .batch_execute(&format!(
                "UPDATE {}.api_keys SET revoked_at=NULL",
                db.schema
            ))
            .await?;
        let mut peer = open_channel(hub.addr).await?;
        hello(&mut peer, "unproven").await?;
        ack(&mut peer, db.machine).await?;
        db.lock_keys().await?;
        db.wait_blocked("api_keys").await?;
        tokio::time::sleep_until(
            Instant::now() + timing.recheck_deadline + Duration::from_millis(30),
        )
        .await;
        assert!(hub.nodes.registry.is_connected(db.machine));
        db.unlock().await?;
        tokio::time::sleep_until(Instant::now() + timing.key_recheck).await;
        assert!(hub.nodes.registry.is_connected(db.machine));
        // A late proof of revocation shares the remaining tick budget with close.
        db.revoke().await?;
        db.lock_keys().await?;
        let registry = Arc::new(Registry::default());
        let (id, _close) = registry.register(db.machine);
        assert!(registry.mark_live(db.machine, id));
        let pool = db.pool()?;
        warm_channel_pool(&pool).await?;
        let tick_start = Instant::now();
        let deadline = tick_start + timing.recheck_deadline;
        let reg = registry.clone();
        let machine = db.machine;
        let key = db.key_id;
        let tick = tokio::spawn(async move {
            let mut sink = Box::pin(futures_util::sink::unfold(
                (),
                |(), _: axum::extract::ws::Message| {
                    std::future::pending::<Result<(), Infallible>>()
                },
            ));
            channel::key_tick(&mut sink, &pool, &reg, machine, id, key, deadline).await
        });
        tokio::time::sleep_until(tick_start + timing.recheck_deadline / 2).await;
        db.unlock().await?;
        timeout_at(deadline, async {
            let mut poll = tokio::time::interval(Duration::from_millis(2));
            while registry.connection_id(machine).is_some() {
                poll.tick().await;
            }
        })
        .await
        .context("late revocation proof must remove registry before shared deadline")?;
        assert!(!registry.is_connected(machine));
        assert!(tick.await?);
        assert!(tick_start.elapsed() < timing.recheck_deadline + Duration::from_millis(50));
        Ok(())
    })
    .await
}

#[tokio::test]
async fn silent_or_malformed_channel_is_closed() -> Result<()> {
    channel_database(async |db| {
        let mut timing = channel_timing();
        timing.idle_timeout = Duration::from_millis(200);
        let hub = channel_hub(db, timing).await?;
        for invalid in [
            serde_json::json!({"type":"nope"}),
            serde_json::json!({"type":"hello","node_version":"v","platform":"x".repeat(65)}),
        ] {
            let mut peer = open_channel(hub.addr).await?;
            peer.send(PeerMessage::Text(invalid.to_string().into()))
                .await?;
            close_code(&mut peer, 1008).await?;
            assert!(!hub.nodes.registry.is_connected(db.machine));
        }
        let mut peer = open_channel(hub.addr).await?;
        hello(&mut peer, "idle").await?;
        ack(&mut peer, db.machine).await?;
        close_code(&mut peer, 4408).await?;
        assert!(hub.nodes.registry.connection_id(db.machine).is_none());
        Ok(())
    })
    .await
}

#[tokio::test]
async fn node_channel_reconnects_with_backoff() -> Result<()> {
    let mut timing = channel_timing();
    timing.backoff_start = Duration::from_millis(100);
    timing.backoff_max = Duration::from_millis(400);
    timing.idle_timeout = Duration::from_millis(240);
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let origin = format!("http://{}", listener.local_addr()?);
    let home = tempfile::tempdir()?;
    let path = write_bootstrap(home.path(), &origin, None, None);
    let bootstrap = parse_hub_database_bootstrap(&std::fs::read_to_string(&path)?)?.unwrap();
    let (node, _claim) = NodeServices::prepare(&bootstrap, &path, home.path())?.unwrap();
    let (sent, mut events) = mpsc::unbounded_channel();
    let server = tokio::spawn(async move {
        let (hold, _) = tokio::sync::watch::channel(());
        for attempt in 0..8 {
            let (mut stream, _) = listener.accept().await.expect("scripted peer accept");
            let at = Instant::now();
            let (head, rest) = read_head(&mut stream).await;
            assert!(rest.is_empty());
            sent.send((attempt, at, head.clone()))
                .expect("attempt event");
            if attempt < 4 {
                stream
                    .write_all(
                        b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                    )
                    .await
                    .expect("refuse");
            } else if attempt == 5 {
                // The upgraded request was read but this peer never answers it.
                let mut release = hold.subscribe();
                tokio::spawn(async move {
                    let _ = release.changed().await;
                    drop(stream);
                });
            } else {
                let request_key = head
                    .lines()
                    .find_map(|line| {
                        line.split_once(':')
                            .filter(|(k, _)| k.eq_ignore_ascii_case("sec-websocket-key"))
                            .map(|(_, v)| v.trim())
                    })
                    .expect("WebSocket key");
                let accept = tokio_tungstenite::tungstenite::handshake::derive_accept_key(
                    request_key.as_bytes(),
                );
                stream
                    .write_all(
                        format!(
                            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\
                     Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
                        )
                        .as_bytes(),
                    )
                    .await
                    .expect("upgrade");
                let mut socket = WebSocketStream::from_raw_socket(
                    stream,
                    tokio_tungstenite::tungstenite::protocol::Role::Server,
                    None,
                )
                .await;
                let hello = socket
                    .next()
                    .await
                    .expect("hello")
                    .expect("hello transport");
                let hello: serde_json::Value =
                    serde_json::from_str(hello.to_text().expect("hello text")).expect("hello JSON");
                assert_eq!(hello["type"], "hello");
                assert_eq!(hello["node_version"], env!("CARGO_PKG_VERSION"));
                assert_eq!(
                    hello["platform"],
                    format!("{}/{}", std::env::consts::OS, std::env::consts::ARCH)
                );
                socket
                    .send(PeerMessage::Text(
                        serde_json::json!({"type":"ack","machine_id":Uuid::new_v4().to_string()})
                            .to_string()
                            .into(),
                    ))
                    .await
                    .expect("ack");
                if attempt == 4 {
                    assert!(matches!(
                        socket.next().await.unwrap().unwrap(),
                        PeerMessage::Ping(_)
                    ));
                    socket
                        .send(PeerMessage::Close(None))
                        .await
                        .expect("close after ping");
                } else {
                    let mut release = hold.subscribe();
                    tokio::spawn(async move {
                        let _ = release.changed().await;
                        drop(socket);
                    });
                }
            }
        }
    });
    let mut client = tokio::spawn(channel::run_client(node, timing));
    let mut attempts = Vec::new();
    for index in 0..8 {
        let event = tokio::select! {
            result = &mut client => { panic!("node client returned before connecting: {result:?}"); },
            event = timeout(TIMEOUT, events.recv()) => event?.expect("connection event"),
        };
        assert_eq!(event.0, index);
        if index == 4 {
            // The following attempt must reload the rotated key.
            std::fs::write(
                &path,
                std::fs::read_to_string(&path)?.replace(KEY, "gobby_rotated_key"),
            )?;
        }
        if index >= 5 {
            assert!(event.2.contains("Bearer gobby_rotated_key"));
        }
        attempts.push(event.1);
    }
    for (index, delay) in [
        timing.backoff_start,
        timing.backoff_start * 2,
        timing.backoff_max,
        timing.backoff_max,
    ]
    .iter()
    .enumerate()
    {
        let elapsed = attempts[index + 1] - attempts[index];
        // Accept-to-accept gaps include handshake and scheduler work under load.
        // The first upper bound stays below the ceiling to reject jumping to it.
        assert!(
            elapsed >= *delay && elapsed < *delay + Duration::from_millis(200),
            "backoff {index}: {elapsed:?}"
        );
    }
    assert!(
        attempts[5] - attempts[4] < timing.backoff_max,
        "ack must reset backoff"
    );
    assert!(attempts[6] - attempts[5] >= timing.attempt_timeout);
    assert!(attempts[6] - attempts[5] < timing.attempt_timeout + timing.backoff_max);
    assert!(attempts[7] - attempts[6] >= timing.idle_timeout);
    assert!(attempts[7] - attempts[6] < timing.idle_timeout + timing.backoff_max);
    // Complete the final hello/ack before stopping the client.
    server.await?;
    client.abort();
    let _ = client.await;
    Ok(())
}

#[tokio::test]
async fn channel_requires_a_key_on_a_hub() -> Result<()> {
    channel_database(async |db| {
        let timing = channel_timing();
        let hub = channel_hub(db, timing).await?;
        let mut forged = get("/api/nodes/channel");
        forged.headers_mut().remove("authorization");
        forged
            .headers_mut()
            .insert("connection", "Upgrade".parse()?);
        forged.headers_mut().insert("upgrade", "websocket".parse()?);
        forged
            .headers_mut()
            .insert("sec-websocket-version", "13".parse()?);
        let nonce = base64::Engine::encode(&base64::engine::general_purpose::STANDARD, [0_u8; 16]);
        forged
            .headers_mut()
            .insert("sec-websocket-key", nonce.parse()?);
        forged
            .headers_mut()
            .insert("x-gobby-machine-id", db.machine.to_string().parse()?);
        forged
            .headers_mut()
            .insert("x-gobby-key-id", db.key_id.to_string().parse()?);
        assert_eq!(send(hub.addr, forged).await.0, StatusCode::UNAUTHORIZED);
        // Standalone forwards the same path rather than mounting node routes.
        let backend = TcpListener::bind("127.0.0.1:0").await?;
        let target = backend.local_addr()?;
        let backend_task = tokio::spawn(async move {
            let (mut stream, _) = backend.accept().await.expect("standalone backend");
            let (head, _) = read_head(&mut stream).await;
            assert!(head.starts_with("GET /api/nodes/channel"));
            stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 14\r\n\
                  Connection: close\r\n\r\n{\"proxy\":true}",
                )
                .await
                .expect("backend response");
        });
        let auth = Arc::new(AuthState::new(
            "test-secret".into(),
            Some(Arc::new(PostgresKeyResolver::new(&db.url))),
            hub._home.path().join("bootstrap.yaml"),
        )?);
        let state = FrontDoorState::new(target, BackendState::Down, auth);
        let table = RouteTable::new(FAMILIES, &Default::default(), &state);
        let listen = TcpListener::bind("127.0.0.1:0").await?;
        let addr = listen.local_addr()?;
        let standalone = tokio::spawn(serve_doors(
            vec![(listen, FrontDoor::new(state, table))],
            None,
            std::future::pending(),
        ));
        let (status, body) = send(addr, get("/api/nodes/channel")).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["proxy"], true);
        standalone.abort();
        let _ = standalone.await;
        backend_task.await?;
        // Revoked during hello admission: no ack, no live publication.
        let mut peer = open_channel(hub.addr).await?;
        db.revoke().await?;
        hello(&mut peer, "revoked-before-hello").await?;
        close_code(&mut peer, 4401).await?;
        assert!(hub.nodes.registry.connection_id(db.machine).is_none());
        db.admin
            .batch_execute(&format!(
                "UPDATE {}.api_keys SET revoked_at=NULL",
                db.schema
            ))
            .await?;
        let mut peer = open_channel(hub.addr).await?;
        db.lock_machine().await?;
        hello(&mut peer, "revoked-during-write").await?;
        db.wait_blocked("machines").await?;
        db.revoke().await?;
        db.unlock().await?;
        close_code(&mut peer, 4401).await?;
        assert!(!hub.nodes.registry.is_connected(db.machine));
        // Reset the transport while the pre-ack check is blocked.
        db.admin
            .batch_execute(&format!(
                "UPDATE {}.api_keys SET revoked_at=NULL",
                db.schema
            ))
            .await?;
        let mut peer = open_channel(hub.addr).await?;
        db.lock_keys().await?;
        hello(&mut peer, "reset-before-ack").await?;
        db.wait_blocked("api_keys").await?;
        assert!(!hub.nodes.registry.is_connected(db.machine));
        use std::os::fd::AsRawFd;
        let linger = libc::linger {
            l_onoff: 1,
            l_linger: 0,
        };
        // SAFETY: the live socket fd and correctly sized linger value stay valid for this call.
        assert_eq!(
            unsafe {
                libc::setsockopt(
                    peer.get_mut().as_raw_fd(),
                    libc::SOL_SOCKET,
                    libc::SO_LINGER,
                    (&linger as *const libc::linger).cast(),
                    std::mem::size_of_val(&linger) as libc::socklen_t,
                )
            },
            0
        );
        drop(peer);
        db.unlock().await?;
        timeout(TIMEOUT, async {
            let mut poll = tokio::time::interval(Duration::from_millis(5));
            while hub.nodes.registry.connection_id(db.machine).is_some() {
                poll.tick().await;
            }
        })
        .await?;
        assert!(!hub.nodes.registry.is_connected(db.machine));
        // Synthetic sinks prove publication and deadline ordering independent of TCP buffers.
        let registry = Registry::default();
        let (old, mut close) = registry.register(db.machine);
        let (next, _next_close) = registry.register(db.machine);
        assert!(!registry.mark_live(db.machine, old));
        assert!(!registry.is_connected(db.machine));
        let sends = Arc::new(AtomicUsize::new(0));
        let count = sends.clone();
        let mut sink = Box::pin(futures_util::sink::unfold(
            (),
            move |(), _: axum::extract::ws::Message| {
                count.fetch_add(1, Ordering::SeqCst);
                async { Ok::<_, Infallible>(()) }
            },
        ));
        assert_eq!(
            channel::send_ack(
                &mut sink,
                &registry,
                db.machine,
                old,
                &mut close,
                Instant::now() + timing.recheck_deadline
            )
            .await,
            AckOutcome::Close(4409)
        );
        assert_eq!(sends.load(Ordering::SeqCst), 0);
        assert_eq!(registry.connection_id(db.machine), Some(next));
        assert!(!registry.is_connected(db.machine));
        let (id, mut close) = registry.register(db.machine);
        let mut pending = Box::pin(futures_util::sink::unfold(
            (),
            |(), _: axum::extract::ws::Message| std::future::pending::<Result<(), Infallible>>(),
        ));
        let deadline = Instant::now() + Duration::from_millis(60);
        assert_eq!(
            channel::send_ack(
                &mut pending,
                &registry,
                db.machine,
                id,
                &mut close,
                deadline
            )
            .await,
            AckOutcome::Drop
        );
        assert!(registry.connection_id(db.machine).is_none());
        assert!(!registry.is_connected(db.machine));
        let (id, mut close) = registry.register(db.machine);
        let mut sink = Box::pin(futures_util::sink::unfold(
            (),
            |(), message: axum::extract::ws::Message| {
                assert!(!registry.is_connected(db.machine));
                assert!(matches!(message, axum::extract::ws::Message::Text(_)));
                async { Ok::<_, Infallible>(()) }
            },
        ));
        assert_eq!(
            channel::send_ack(
                &mut sink,
                &registry,
                db.machine,
                id,
                &mut close,
                Instant::now() + timing.recheck_deadline
            )
            .await,
            AckOutcome::Live
        );
        assert!(registry.is_connected(db.machine));
        Ok(())
    })
    .await
}

#[tokio::test]
async fn replaced_channel_cannot_overwrite_successor_bookkeeping() -> Result<()> {
    channel_database(async |db| {
        let mut timing = channel_timing();
        timing.recheck_deadline = Duration::from_secs(2);
        let hub = channel_hub(db, timing).await?;
        let mut first = open_channel(hub.addr).await?;
        let mut second = open_channel(hub.addr).await?;
        db.lock_machine().await?;
        hello(&mut first, "old").await?;
        let old = wait_registered(&hub.nodes, db.machine).await?;
        db.wait_blocked("machines").await?;
        hello(&mut second, "new").await?;
        close_code(&mut first, 4409).await?;
        assert!(!hub.nodes.registry.is_connected(db.machine));
        assert!(
            timeout(Duration::from_millis(60), second.next())
                .await
                .is_err(),
            "successor ack must wait for the lifetime gate"
        );
        db.unlock().await?;
        ack(&mut second, db.machine).await?;
        assert!(hub.nodes.registry.is_connected(db.machine));
        let current = hub
            .nodes
            .registry
            .connection_id(db.machine)
            .expect("successor ID");
        assert_ne!(old, current);
        assert_eq!(db.row().await?.1.as_deref(), Some("new"));
        let pool = db.pool()?;
        let machine = db.machine.to_string();
        let outcome = hub
            .nodes
            .registry
            .persist(db.machine, old, async {
                let client = pool.get().await?;
                client
                    .execute(
                        "UPDATE machines SET node_version='stale' WHERE id=$1::text::uuid",
                        &[&machine],
                    )
                    .await?;
                Ok::<_, anyhow::Error>(())
            })
            .await;
        assert!(matches!(outcome, PersistOutcome::Skipped));
        assert_eq!(db.row().await?.1.as_deref(), Some("new"));
        // Pre-ack key lookup is cancellable when a newer hello registers.
        let mut first = open_channel(hub.addr).await?;
        let mut second = open_channel(hub.addr).await?;
        db.lock_keys().await?;
        hello(&mut first, "pre-ack-old").await?;
        db.wait_blocked("api_keys").await?;
        hello(&mut second, "pre-ack-new").await?;
        close_code(&mut first, 4409).await?;
        assert!(!hub.nodes.registry.is_connected(db.machine));
        assert!(
            timeout(Duration::from_millis(60), second.next())
                .await
                .is_err()
        );
        db.unlock().await?;
        ack(&mut second, db.machine).await?;
        assert!(hub.nodes.registry.is_connected(db.machine));
        assert_eq!(db.row().await?.1.as_deref(), Some("pre-ack-new"));
        Ok(())
    })
    .await
}

#[tokio::test]
async fn hub_connect_deadline_is_bounded() {
    let result = timeout(
        Duration::from_millis(100),
        gobby_daemon::front_door::relay::connect_with_timeout(
            Duration::from_millis(20),
            std::future::pending::<std::io::Result<TcpStream>>(),
        ),
    )
    .await;
    assert!(
        result.is_ok(),
        "hub dial must finish within its connect deadline"
    );
    assert_eq!(
        result.unwrap().unwrap_err().kind(),
        std::io::ErrorKind::TimedOut
    );
}
