//! Compare mode: the per-method policy, the compare log and `gdaemon compare-report`.

mod common;

use std::collections::BTreeMap;
use std::convert::Infallible;
use std::future::Future;
use std::net::SocketAddr;
use std::path::Path;
use std::pin::Pin;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use assert_cmd::Command;
use axum::Router;
use axum::body::Body;
use axum::http::header::{CACHE_CONTROL, CONTENT_TYPE};
use axum::http::request::Parts;
use axum::http::{Method, Request, Response, StatusCode};
use axum::routing::{get, post};
use bytes::Bytes;
use common::{TIMEOUT, refused_addr};
use gobby_core::bootstrap::RouteBackend;
use gobby_core::route_family::{ProxiedCheck, RouteFamily};
use gobby_daemon::front_door::FrontDoorState;
use gobby_daemon::front_door::auth::AuthState;
use gobby_daemon::front_door::compare::{CompareLog, Outcome, Record, log_path, report};
use gobby_daemon::front_door::health::BackendState;
use gobby_daemon::front_door::routes::RouteTable;
use http_body_util::{BodyExt, Full};
use hyper::body::Incoming;
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper_util::rt::TokioIo;
use serde_json::{Value, json};
use tokio::net::TcpListener;

const JSON: &str = "application/json";
const TEXT: &str = "text/plain; charset=utf-8";
const SAME: &str = r#"{"a":1,"b":[1,2]}"#;
const WRITE_OK: &str = r#"{"id":"ok"}"#;
const WRITE_BODY: &[u8] = br#"{"name":"original-request"}"#;

/// POSTs that reached the native router; compare must never send one there.
static NATIVE_POSTS: AtomicUsize = AtomicUsize::new(0);
/// Every `check_proxied` call: path, request bytes and proxied status.
static CHECKED: Mutex<Vec<(String, Vec<u8>, u16)>> = Mutex::new(Vec::new());

/// A family under `/api/cmp` whose native legs agree, reorder, differ or fail.
struct CompareFamily;

impl RouteFamily<FrontDoorState> for CompareFamily {
    fn name(&self) -> &'static str {
        "cmp"
    }
    fn prefixes(&self) -> &'static [&'static str] {
        &["/api/cmp"]
    }
    fn router(&self) -> Router<FrontDoorState> {
        Router::new()
            .route(
                "/api/cmp/same",
                get(|| async { reply(StatusCode::OK, JSON, SAME) }),
            )
            .route(
                "/api/cmp/reordered",
                get(|| async { reply(StatusCode::OK, JSON, r#"{"b":[1,2],"a":1}"#) }),
            )
            .route(
                "/api/cmp/differs",
                get(|| async { reply(StatusCode::OK, JSON, r#"{"a":1,"b":[1,"native-value"]}"#) }),
            )
            .route(
                "/api/cmp/status",
                get(|| async { reply(StatusCode::NOT_FOUND, JSON, SAME) }),
            )
            .route(
                "/api/cmp/cached",
                get(|| async {
                    let mut response = reply(StatusCode::OK, JSON, SAME);
                    response
                        .headers_mut()
                        .insert(CACHE_CONTROL, "no-store".parse().expect("header"));
                    response
                }),
            )
            .route(
                "/api/cmp/text",
                get(|| async { reply(StatusCode::OK, TEXT, r#"{"b":2,"a":1}"#) }),
            )
            .route("/api/cmp/broken", get(broken_body))
            .route("/api/cmp/panics", get(panics))
            .route("/api/cmp/write", post(native_post))
            .route("/api/cmp/write-bad", post(native_post))
            .route("/api/cmp/unchecked", post(native_post))
    }
    fn check_proxied<'a>(
        &'a self,
        parts: &'a Parts,
        request: &'a [u8],
        status: StatusCode,
        response: &'a [u8],
    ) -> Pin<Box<dyn Future<Output = ProxiedCheck> + Send + 'a>> {
        let path = parts.uri.path();
        CHECKED
            .lock()
            .expect("checked")
            .push((path.to_owned(), request.to_vec(), status.as_u16()));
        let check = match path {
            "/api/cmp/unchecked" => ProxiedCheck::Skipped,
            _ if response == WRITE_OK.as_bytes() => ProxiedCheck::Verified,
            _ => ProxiedCheck::Mismatch("/id".into()),
        };
        Box::pin(async move { check })
    }
}

static FAMILIES: &[&dyn RouteFamily<FrontDoorState>] = &[&CompareFamily];

fn reply(status: StatusCode, content_type: &'static str, body: &'static str) -> Response<Body> {
    let mut response = Response::new(Body::from(body));
    *response.status_mut() = status;
    response
        .headers_mut()
        .insert(CONTENT_TYPE, content_type.parse().expect("content type"));
    response
}

async fn broken_body() -> Response<Body> {
    let chunks = [Err::<Bytes, _>(std::io::Error::other("native leg failed"))];
    Response::new(Body::from_stream(futures_util::stream::iter(chunks)))
}

async fn panics() -> StatusCode {
    panic!("native leg panicked")
}

async fn native_post() -> StatusCode {
    NATIVE_POSTS.fetch_add(1, Ordering::SeqCst);
    StatusCode::CREATED
}

/// What the stub Python backend answers for each request.
fn python_answer(method: &Method, path: &str) -> (StatusCode, &'static str, &'static str) {
    match (method.as_str(), path) {
        ("GET", "/api/cmp/differs") => (StatusCode::OK, JSON, r#"{"a":1,"b":[1,"proxied-value"]}"#),
        ("GET", "/api/cmp/text") => (StatusCode::OK, TEXT, r#"{"a":1,"b":2}"#),
        ("GET", _) => (StatusCode::OK, JSON, SAME),
        ("POST", "/api/cmp/write-bad") => (StatusCode::CREATED, JSON, r#"{"id":"other"}"#),
        ("POST", _) => (StatusCode::CREATED, JSON, WRITE_OK),
        _ => (StatusCode::NOT_FOUND, TEXT, ""),
    }
}

type Seen = Arc<Mutex<Vec<(String, String, Vec<u8>)>>>;

/// A stub Python backend that records each request's method, path and body bytes.
async fn python() -> (SocketAddr, Seen) {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind stub");
    let addr = listener.local_addr().expect("stub addr");
    let seen: Seen = Arc::default();
    let log = seen.clone();
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            let log = log.clone();
            tokio::spawn(async move {
                let service = service_fn(move |request: Request<Incoming>| {
                    let log = log.clone();
                    async move {
                        let (parts, body) = request.into_parts();
                        let body = body.collect().await.expect("stub body").to_bytes();
                        log.lock().expect("seen").push((
                            parts.method.to_string(),
                            parts.uri.path().to_owned(),
                            body.to_vec(),
                        ));
                        let (status, content_type, text) =
                            python_answer(&parts.method, parts.uri.path());
                        let mut response =
                            Response::new(Full::new(Bytes::from_static(text.as_bytes())));
                        *response.status_mut() = status;
                        response
                            .headers_mut()
                            .insert(CONTENT_TYPE, content_type.parse().expect("content type"));
                        Ok::<_, Infallible>(response)
                    }
                });
                let _ = http1::Builder::new()
                    .serve_connection(TokioIo::new(stream), service)
                    .await;
            });
        }
    });
    (addr, seen)
}

/// A listener state for `target` that logs compare outcomes under `home`.
fn compare_state(target: SocketAddr, home: &Path) -> (FrontDoorState, RouteTable) {
    let auth =
        Arc::new(AuthState::new("boot-secret".into(), None, Default::default()).expect("auth"));
    let mut state = FrontDoorState::new(target, BackendState::Down, auth);
    state.compare_log = Some(Arc::new(CompareLog::open(home).expect("compare log")));
    let routes = BTreeMap::from([("cmp".to_owned(), RouteBackend::Compare)]);
    let table = RouteTable::new(FAMILIES, &routes, &state);
    (state, table)
}

async fn send(
    table: &RouteTable,
    state: &FrontDoorState,
    method: Method,
    uri: &str,
    body: &'static [u8],
) -> (StatusCode, Bytes) {
    let request = Request::builder()
        .method(method)
        .uri(uri)
        .header("host", "localhost")
        .body(Body::from(body))
        .expect("request");
    let response = tokio::time::timeout(TIMEOUT, table.dispatch(state, request))
        .await
        .expect("dispatch timed out");
    let status = response.status();
    let body = response
        .into_body()
        .collect()
        .await
        .expect("body")
        .to_bytes();
    (status, body)
}

fn records(home: &Path) -> Vec<Value> {
    std::fs::read_to_string(log_path(home))
        .unwrap_or_default()
        .lines()
        .map(|line| serde_json::from_str(line).expect("record line"))
        .collect()
}

fn now() -> i64 {
    let elapsed = SystemTime::now().duration_since(UNIX_EPOCH).expect("clock");
    i64::try_from(elapsed.as_secs()).expect("seconds")
}

fn record(ts: i64, family: &str, outcome: Outcome) -> Record {
    Record {
        ts,
        family: family.to_owned(),
        method: "GET".to_owned(),
        path: "/api/x".to_owned(),
        outcome,
        native_status: Some(200),
        proxied_status: Some(200),
        detail: None,
    }
}

#[tokio::test]
async fn get_compare_records_match_and_diff_without_values() {
    let (backend, _seen) = python().await;
    let home = tempfile::tempdir().expect("home");
    let (state, table) = compare_state(backend, home.path());
    let before = now();

    let same = send(
        &table,
        &state,
        Method::GET,
        "/api/cmp/same?token=query-value",
        b"",
    )
    .await;
    assert_eq!(same, (StatusCode::OK, Bytes::from_static(SAME.as_bytes())));
    let differs = send(&table, &state, Method::GET, "/api/cmp/differs", b"").await;
    assert_eq!(
        differs,
        (
            StatusCode::OK,
            Bytes::from_static(br#"{"a":1,"b":[1,"proxied-value"]}"#)
        ),
        "the caller receives the proxied response"
    );
    let status = send(&table, &state, Method::GET, "/api/cmp/status", b"").await;
    assert_eq!(status.0, StatusCode::OK);
    let cached = send(&table, &state, Method::GET, "/api/cmp/cached", b"").await;
    assert_eq!(cached.0, StatusCode::OK);

    let records = records(home.path());
    let summary: Vec<_> = records
        .iter()
        .map(|record| {
            json!([
                record["path"],
                record["outcome"],
                record["native_status"],
                record["detail"]
            ])
        })
        .collect();
    assert_eq!(
        summary,
        vec![
            json!(["/api/cmp/same", "match", 200, null]),
            json!(["/api/cmp/differs", "diff", 200, "/b/1"]),
            json!(["/api/cmp/status", "diff", 404, "status"]),
            json!(["/api/cmp/cached", "diff", 200, "header:cache-control"]),
        ]
    );
    for record in &records {
        assert_eq!(record["family"], "cmp");
        assert_eq!(record["method"], "GET");
        assert_eq!(record["proxied_status"], 200);
        let ts = record["ts"].as_i64().expect("ts");
        assert!(
            (before..=now()).contains(&ts),
            "ts {ts} is the request time"
        );
    }
    let log = std::fs::read_to_string(log_path(home.path())).expect("log");
    for value in ["native-value", "proxied-value", "query-value"] {
        assert!(!log.contains(value), "the log carries no values: {log}");
    }
}

#[tokio::test]
async fn json_bodies_compare_by_value() {
    let (backend, _seen) = python().await;
    let home = tempfile::tempdir().expect("home");
    let (state, table) = compare_state(backend, home.path());

    let reordered = send(&table, &state, Method::GET, "/api/cmp/reordered", b"").await;
    assert_eq!(
        reordered,
        (StatusCode::OK, Bytes::from_static(SAME.as_bytes()))
    );
    let text = send(&table, &state, Method::GET, "/api/cmp/text", b"").await;
    assert_eq!(
        text,
        (StatusCode::OK, Bytes::from_static(br#"{"a":1,"b":2}"#))
    );

    let outcomes: Vec<_> = records(home.path())
        .iter()
        .map(|record| json!([record["path"], record["outcome"], record["detail"]]))
        .collect();
    assert_eq!(
        outcomes,
        vec![
            json!(["/api/cmp/reordered", "match", null]),
            json!(["/api/cmp/text", "diff", "bytes"]),
        ],
        "JSON compares by value; other bodies compare byte for byte"
    );
}

#[tokio::test]
async fn non_get_requests_run_only_the_proxied_leg() {
    let (backend, seen) = python().await;
    let home = tempfile::tempdir().expect("home");
    let (state, table) = compare_state(backend, home.path());

    let write = send(&table, &state, Method::POST, "/api/cmp/write", WRITE_BODY).await;
    assert_eq!(
        write,
        (StatusCode::CREATED, Bytes::from_static(WRITE_OK.as_bytes()))
    );
    let bad = send(
        &table,
        &state,
        Method::POST,
        "/api/cmp/write-bad",
        WRITE_BODY,
    )
    .await;
    assert_eq!(
        bad,
        (
            StatusCode::CREATED,
            Bytes::from_static(br#"{"id":"other"}"#)
        )
    );
    let unchecked = send(
        &table,
        &state,
        Method::POST,
        "/api/cmp/unchecked",
        WRITE_BODY,
    )
    .await;
    assert_eq!(unchecked.0, StatusCode::CREATED);

    assert_eq!(
        NATIVE_POSTS.load(Ordering::SeqCst),
        0,
        "the native router is never reached"
    );
    let paths = ["/api/cmp/write", "/api/cmp/write-bad", "/api/cmp/unchecked"];
    let backend_saw: Vec<_> = seen.lock().expect("seen").clone();
    assert_eq!(
        backend_saw,
        paths
            .iter()
            .map(|path| ("POST".to_owned(), (*path).to_owned(), WRITE_BODY.to_vec()))
            .collect::<Vec<_>>(),
        "the backend receives each request once with its original bytes"
    );
    let checked: Vec<_> = CHECKED
        .lock()
        .expect("checked")
        .iter()
        .filter(|(path, _, _)| paths.contains(&path.as_str()))
        .cloned()
        .collect();
    assert_eq!(
        checked,
        paths
            .iter()
            .map(|path| ((*path).to_owned(), WRITE_BODY.to_vec(), 201))
            .collect::<Vec<_>>(),
        "check_proxied receives the original request bytes and the proxied status"
    );

    let outcomes: Vec<_> = records(home.path())
        .iter()
        .map(|record| {
            json!([
                record["method"],
                record["path"],
                record["outcome"],
                record["native_status"],
                record["proxied_status"],
                record["detail"]
            ])
        })
        .collect();
    assert_eq!(
        outcomes,
        vec![
            json!(["POST", "/api/cmp/write", "verified", null, 201, null]),
            json!(["POST", "/api/cmp/write-bad", "mismatch", null, 201, "/id"]),
            json!(["POST", "/api/cmp/unchecked", "skipped", null, 201, null]),
        ]
    );
}

#[tokio::test]
async fn native_failure_returns_the_proxied_response() {
    let (backend, _seen) = python().await;
    let home = tempfile::tempdir().expect("home");
    let (state, table) = compare_state(backend, home.path());

    for path in ["/api/cmp/broken", "/api/cmp/panics"] {
        let response = send(&table, &state, Method::GET, path, b"").await;
        assert_eq!(
            response,
            (StatusCode::OK, Bytes::from_static(SAME.as_bytes())),
            "{path} still returns the proxied response"
        );
    }

    let outcomes: Vec<_> = records(home.path())
        .iter()
        .map(|record| json!([record["path"], record["outcome"], record["proxied_status"]]))
        .collect();
    assert_eq!(
        outcomes,
        vec![
            json!(["/api/cmp/broken", "native_error", 200]),
            json!(["/api/cmp/panics", "native_error", 200]),
        ]
    );
}

#[test]
fn report_fails_on_mismatch_and_empty_window() {
    let home = tempfile::tempdir().expect("home");
    let log = CompareLog::open(home.path()).expect("compare log");
    log.append(&record(100, "health", Outcome::Mismatch));
    log.append(&record(200, "health", Outcome::Match));
    log.append(&record(250, "health", Outcome::Match));
    log.append(&record(300, "runtime", Outcome::Verified));
    log.append(&record(300, "runtime", Outcome::Skipped));
    let path = log_path(home.path());

    let window = report(&path, Some(150)).expect("report");
    assert_eq!(
        serde_json::to_value(&window).expect("report json"),
        json!({
            "since": 150,
            "first_ts": 200,
            "last_ts": 300,
            "total": 4,
            "families": {
                "health": {"match": 2},
                "runtime": {"verified": 1, "skipped": 1},
            },
        })
    );
    assert!(window.passes());
    let everything = report(&path, None).expect("report");
    assert_eq!(everything.total, 5);
    assert!(!everything.passes(), "a mismatch fails the report");
    let empty = report(&path, Some(1_000)).expect("report");
    assert_eq!(empty.total, 0);
    assert!(!empty.passes(), "an empty window proves nothing");

    let exit = |args: &[&str]| {
        let output = Command::cargo_bin("gdaemon")
            .expect("gdaemon binary")
            .env("GOBBY_HOME", home.path())
            .arg("compare-report")
            .args(args)
            .output()
            .expect("run compare-report");
        let stdout: Option<Value> = serde_json::from_slice(&output.stdout).ok();
        (output.status.code(), stdout)
    };
    let printed = serde_json::to_value(&window).expect("report json");
    assert_eq!(exit(&["--since", "150"]), (Some(0), Some(printed)));
    assert_eq!(exit(&[]).0, Some(1), "the full window holds a mismatch");
    assert_eq!(exit(&["--since", "1000"]).0, Some(1), "the window is empty");

    let malformed = home.path().join("malformed.jsonl");
    std::fs::write(&malformed, "not json\n").expect("write malformed log");
    assert!(
        report(&malformed, None).is_err(),
        "a malformed line is an error"
    );
}

#[tokio::test]
async fn unreachable_backend_records_skipped() {
    let backend = refused_addr().await;
    let home = tempfile::tempdir().expect("home");
    let (state, table) = compare_state(backend, home.path());

    for (method, path) in [
        (Method::GET, "/api/cmp/same"),
        (Method::POST, "/api/cmp/write-refused"),
    ] {
        let (status, body) = send(&table, &state, method, path, WRITE_BODY).await;
        assert_eq!(
            status,
            StatusCode::SERVICE_UNAVAILABLE,
            "{path} returns the typed 503"
        );
        let body: Value = serde_json::from_slice(&body).expect("json body");
        assert_eq!(body["status"], "unavailable");
    }

    assert_eq!(NATIVE_POSTS.load(Ordering::SeqCst), 0);
    assert!(
        CHECKED
            .lock()
            .expect("checked")
            .iter()
            .all(|(path, _, _)| path != "/api/cmp/write-refused"),
        "check_proxied is skipped when Python gave no answer"
    );
    let outcomes: Vec<_> = records(home.path())
        .iter()
        .map(|record| {
            json!([
                record["method"],
                record["path"],
                record["outcome"],
                record["proxied_status"],
                record["detail"]
            ])
        })
        .collect();
    assert_eq!(
        outcomes,
        vec![
            json!([
                "GET",
                "/api/cmp/same",
                "skipped",
                null,
                "backend_unavailable"
            ]),
            json!([
                "POST",
                "/api/cmp/write-refused",
                "skipped",
                null,
                "backend_unavailable"
            ]),
        ]
    );
}
