//! Driver backend selection and the compare listener (design-v2 sections 7.9 and 7.10).
//!
//! The driver passes each family's backend to the listener as a constructor argument, in the
//! `front_door.routes` map that `RouteTable::new` reads. The compare listener is built from the
//! crate's public pieces (`FrontDoorState`, `CompareLog`, `RouteTable`, `FrontDoor`,
//! `serve_doors`), not from `serve.rs`, so it dispatches the way a daemon does.

use std::collections::BTreeMap;
use std::convert::Infallible;
use std::net::SocketAddr;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::Router;
use axum::http::{Request, Response};
use axum::routing::get;
use bytes::Bytes;
use gobby_core::bootstrap::RouteBackend;
use gobby_core::route_family::RouteFamily;
use gobby_daemon::front_door::auth::AuthState;
use gobby_daemon::front_door::compare::{CompareLog, Outcome, Record, log_path};
use gobby_daemon::front_door::health::BackendState;
use gobby_daemon::front_door::routes::{FAMILIES, RouteTable};
use gobby_daemon::front_door::{FrontDoor, FrontDoorState};
use gobby_daemon::heartbeat::HeartbeatHost;
use gobby_daemon::serve::serve_doors;
use http_body_util::{BodyExt, Full};
use hyper::body::Incoming;
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper_util::rt::TokioIo;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::task::JoinHandle;

const TIMEOUT: Duration = Duration::from_secs(10);
/// The answer of the test-only family's one native route.
const NATIVE_ANSWER: &str = "native-answer";
/// The answer of the stub backend that every proxied request reaches.
const STUB_ANSWER: &str = "stub-answer";
/// The outcome name a differential run writes when the family has no native backend.
pub const NO_NATIVE_BACKEND: &str = "no-native-backend";

/// Method and path of each request a stub backend received, in arrival order.
pub type Seen = Arc<Mutex<Vec<(String, String)>>>;

/// A test-only family under `/api/probe`. Its one native route answers `native-answer`; every
/// other probe path has no native handler, so it falls through to the proxy.
pub struct Probe;

impl RouteFamily<FrontDoorState> for Probe {
    fn name(&self) -> &'static str {
        "probe"
    }
    fn prefixes(&self) -> &'static [&'static str] {
        &["/api/probe"]
    }
    fn router(&self) -> Router<FrontDoorState> {
        Router::new().route("/api/probe/native", get(|| async { NATIVE_ANSWER }))
    }
}

/// The probe family as a registry entry, for `start_listener` and `native_family`.
pub static PROBE: Probe = Probe;

/// The `front_door.routes` map a listener reads: each named family's backend. A family the map
/// does not name takes its `default_backend`, which is `Proxy`.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Backends(BTreeMap<String, RouteBackend>);

impl Backends {
    pub fn new<'a>(entries: impl IntoIterator<Item = (&'a str, RouteBackend)>) -> Self {
        Self(
            entries
                .into_iter()
                .map(|(name, backend)| (name.to_owned(), backend))
                .collect(),
        )
    }

    pub fn routes(&self) -> &BTreeMap<String, RouteBackend> {
        &self.0
    }
}

/// A differential run has no native backend to compare with: no registry family has the name.
#[derive(Debug, PartialEq, Eq)]
pub struct NoNativeBackend {
    pub family: String,
}

impl NoNativeBackend {
    /// The explicit outcome name the differential run records (never `skipped`, never `match`).
    pub fn outcome(&self) -> &'static str {
        NO_NATIVE_BACKEND
    }
}

/// The native family a differential run compares `family` against: the entry named `family`.
pub fn native_family<'a>(
    families: &[&'a dyn RouteFamily<FrontDoorState>],
    family: &str,
) -> Result<&'a dyn RouteFamily<FrontDoorState>, NoNativeBackend> {
    families
        .iter()
        .copied()
        .find(|entry| entry.name() == family)
        .ok_or_else(|| NoNativeBackend {
            family: family.to_owned(),
        })
}

/// Serves `families` with `backends` on a fresh loopback port. Proxied legs go to `target`,
/// and compare outcomes are logged under `home`. Returns the listener's address.
pub async fn start_listener(
    families: &[&'static dyn RouteFamily<FrontDoorState>],
    backends: &Backends,
    target: SocketAddr,
    home: &Path,
) -> SocketAddr {
    start_listener_task(families, backends, target, home)
        .await
        .0
}

/// As [`start_listener`], and also returns the task that serves the listener, so a caller
/// that starts many listeners can stop each one.
pub async fn start_listener_task(
    families: &[&'static dyn RouteFamily<FrontDoorState>],
    backends: &Backends,
    target: SocketAddr,
    home: &Path,
) -> (SocketAddr, JoinHandle<()>) {
    let auth =
        Arc::new(AuthState::new("driver-secret".into(), None, Default::default()).expect("auth"));
    let mut state = FrontDoorState::new(target, BackendState::Down, auth);
    state.compare_log = Some(Arc::new(CompareLog::open(home).expect("compare log")));
    let table = RouteTable::new(families, backends.routes(), &state);
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind listener");
    let addr = listener.local_addr().expect("listener addr");
    let door = FrontDoor::new(state, table);
    let task = tokio::spawn(async move {
        let doors = vec![(listener, door)];
        let _ = serve_doors(doors, None, HeartbeatHost::new(), std::future::pending()).await;
    });
    (addr, task)
}

/// A stub Python backend: answers every request with `stub-answer` and records its method and
/// path before answering.
pub async fn stub_target() -> (SocketAddr, Seen) {
    let (addr, seen, _task) = stub_target_task().await;
    (addr, seen)
}

/// As [`stub_target`], and also returns the task that accepts connections, so a caller that starts
/// many stubs can stop each one.
pub async fn stub_target_task() -> (SocketAddr, Seen, JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind stub");
    let addr = listener.local_addr().expect("stub addr");
    let seen: Seen = Arc::default();
    let log = seen.clone();
    let task = tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            let log = log.clone();
            tokio::spawn(async move {
                let service = service_fn(move |request: Request<Incoming>| {
                    let log = log.clone();
                    async move {
                        let (parts, body) = request.into_parts();
                        let _ = body.collect().await;
                        log.lock()
                            .expect("seen")
                            .push((parts.method.to_string(), parts.uri.path().to_owned()));
                        let answer = Bytes::from_static(STUB_ANSWER.as_bytes());
                        // The native probe answers as text, so the stub carries the same type.
                        let response = Response::builder()
                            .header("content-type", "text/plain; charset=utf-8")
                            .body(Full::new(answer))
                            .expect("stub response");
                        Ok::<_, Infallible>(response)
                    }
                });
                let _ = http1::Builder::new()
                    .serve_connection(TokioIo::new(stream), service)
                    .await;
            });
        }
    });
    (addr, seen, task)
}

/// A relay between the front door and a WS stub. It forwards the upgrade head unchanged, passes
/// each later client chunk through `rewrite_client`, and after the stub's end of stream waits
/// briefly and writes `late_reply` to the client (design-v2 section 7.11.1; fix3 V22 and V25).
#[derive(Debug, Clone, Default)]
pub struct WsHop {
    /// Rewrites each client chunk after the upgrade head. `None` forwards the client bytes as sent.
    pub rewrite_client: Option<fn(&mut [u8])>,
    /// Bytes written to the client after the stub's reply and its end of stream. Empty writes
    /// nothing.
    pub late_reply: Vec<u8>,
}

/// The delay between the stub's end of stream and the hop's late write.
const LATE_DELAY: Duration = Duration::from_millis(50);

/// Starts a hop to `backend` for one connection and returns the address the front door should
/// dial instead. The upgrade head crosses unchanged; the client bytes after it pass through
/// `rewrite_client` chunk by chunk; the backend's bytes cross unchanged, and `late_reply` follows
/// them after the backend's end of stream and [`LATE_DELAY`], before the client leg closes.
pub async fn start_ws_hop(backend: SocketAddr, hop: &WsHop) -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind hop");
    let addr = listener.local_addr().expect("hop addr");
    let hop = hop.clone();
    tokio::spawn(async move {
        let Ok((inbound, _)) = listener.accept().await else {
            return;
        };
        let Ok(outbound) = TcpStream::connect(backend).await else {
            return;
        };
        let (mut front_read, mut front_write) = inbound.into_split();
        let (mut back_read, mut back_write) = outbound.into_split();
        let rewrite = hop.rewrite_client;
        tokio::spawn(async move {
            let (head, rest) = crate::common::read_head(&mut front_read).await;
            if back_write.write_all(head.as_bytes()).await.is_err() {
                return;
            }
            let mut pending = rest;
            loop {
                if !pending.is_empty() {
                    if let Some(rewrite) = rewrite {
                        rewrite(&mut pending);
                    }
                    if back_write.write_all(&pending).await.is_err() {
                        return;
                    }
                }
                let mut chunk = [0_u8; 4096];
                match front_read.read(&mut chunk).await {
                    Ok(0) | Err(_) => break,
                    Ok(read) => pending = chunk[..read].to_vec(),
                }
            }
            let _ = back_write.shutdown().await;
        });
        let late = hop.late_reply;
        tokio::spawn(async move {
            let mut chunk = [0_u8; 4096];
            loop {
                match back_read.read(&mut chunk).await {
                    Ok(0) | Err(_) => break,
                    Ok(read) => {
                        if front_write.write_all(&chunk[..read]).await.is_err() {
                            return;
                        }
                    }
                }
            }
            // The stub's reply has crossed; the late bytes follow it, before the client leg closes.
            if !late.is_empty() {
                tokio::time::sleep(LATE_DELAY).await;
                if front_write.write_all(&late).await.is_err() {
                    return;
                }
            }
            let _ = front_write.shutdown().await;
        });
    });
    addr
}

/// One `GET` over a fresh connection; returns the status code and the body text.
pub async fn get_text(addr: SocketAddr, path: &str) -> (u16, String) {
    let exchange = async {
        let mut stream = TcpStream::connect(addr).await.expect("connect listener");
        let request =
            format!("GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n");
        stream
            .write_all(request.as_bytes())
            .await
            .expect("write request");
        let mut raw = Vec::new();
        stream.read_to_end(&mut raw).await.expect("read response");
        String::from_utf8(raw).expect("utf-8 response")
    };
    let text = tokio::time::timeout(TIMEOUT, exchange)
        .await
        .expect("GET timed out");
    let (head, body) = text.split_once("\r\n\r\n").expect("header end");
    let status = head
        .split_whitespace()
        .nth(1)
        .and_then(|code| code.parse().ok())
        .expect("status code");
    (status, body.to_owned())
}

/// The compare records the listener wrote under `home`.
pub fn read_records(home: &Path) -> Vec<Record> {
    std::fs::read_to_string(log_path(home))
        .unwrap_or_default()
        .lines()
        .map(|line| serde_json::from_str(line).expect("record line"))
        .collect()
}

#[tokio::test]
async fn backend_per_family_comes_from_the_constructor() {
    let (target, seen) = stub_target().await;
    let cases = [
        (
            Backends::new([("probe", RouteBackend::Native)]),
            NATIVE_ANSWER,
            0,
        ),
        (
            Backends::new([("probe", RouteBackend::Proxy)]),
            STUB_ANSWER,
            1,
        ),
        // A family the map does not name takes its default backend, which is Proxy.
        (Backends::default(), STUB_ANSWER, 1),
    ];
    for (backends, body, stub_requests) in cases {
        let home = tempfile::tempdir().expect("home");
        let addr = start_listener(&[&PROBE], &backends, target, home.path()).await;
        seen.lock().expect("seen").clear();

        let (status, text) = get_text(addr, "/api/probe/native").await;
        assert_eq!((status, text.as_str()), (200, body), "{backends:?}");
        assert_eq!(
            seen.lock().expect("seen").len(),
            stub_requests,
            "{backends:?}"
        );
    }
}

#[tokio::test]
async fn get_makes_two_stub_requests_and_records_match() {
    let (target, seen) = stub_target().await;
    let home = tempfile::tempdir().expect("home");
    let backends = Backends::new([("probe", RouteBackend::Compare)]);
    let addr = start_listener(&[&PROBE], &backends, target, home.path()).await;

    // The probe has no handler for this path, so the native leg falls through to the same stub.
    let (status, text) = get_text(addr, "/api/probe/shared").await;
    assert_eq!((status, text.as_str()), (200, STUB_ANSWER));
    let requests = seen.lock().expect("seen").clone();
    let get = ("GET".to_owned(), "/api/probe/shared".to_owned());
    assert_eq!(requests, vec![get.clone(), get], "both legs reach the stub");

    let records = read_records(home.path());
    assert_eq!(records.len(), 1, "one compare record per GET");
    assert_eq!(records[0].family, "probe");
    assert_eq!(records[0].outcome, Outcome::Match);
}

#[test]
fn differential_without_a_native_family_is_no_native_backend() {
    assert!(
        native_family(FAMILIES, "health").is_ok(),
        "health has a native family"
    );
    assert!(
        native_family(&[&PROBE], "probe").is_ok(),
        "the probe is passed by constructor"
    );
    let Err(missing) = native_family(FAMILIES, "missing-family") else {
        panic!("no registry family is named missing-family");
    };
    assert_eq!(missing.outcome(), "no-native-backend");
}
