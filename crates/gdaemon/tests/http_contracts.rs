//! Replays the HTTP contract corpus (`tests/contracts/http/`) through `gdaemon serve`.
//!
//! The loader, secret redaction, masks, and response normalization mirror
//! `tests/contracts/http_corpus.py`, so both harnesses compare like with like.
//! `origin: python` cases run against a case-driven stub backend; `origin: gdaemon`
//! cases run against a backend address that refuses connections.

mod common;

use std::collections::{BTreeMap, BTreeSet};
use std::convert::Infallible;
use std::net::SocketAddr;
use std::path::Path;
use std::sync::{Arc, Mutex};

use bytes::Bytes;
use common::TIMEOUT;
use gobby_core::bootstrap::RouteBackend;
use gobby_daemon::front_door::health::SERVED_BY_HEADER;
use gobby_daemon::front_door::routes::{FAMILIES, RouteFamily, unimplemented_families};
use gobby_daemon::serve::{PublicListener, serve};
use http_body_util::{BodyExt, Full};
use hyper::body::Incoming;
use hyper::header::HeaderMap;
use hyper::server::conn::http1 as server_http1;
use hyper::service::service_fn;
use hyper::{Request, Response};
use hyper_util::rt::TokioIo;
use serde_json::{Map, Value, json};
use tokio::net::{TcpListener, TcpSocket, TcpStream};

const CORPUS_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../tests/contracts/http");
const MASK: &str = "@mask@";
const SECRET: &str = "@secret@";
const RESPONSE_HEADER_ALLOWLIST: &[&str] = &[
    "cache-control",
    "content-type",
    "retry-after",
    "x-gobby-user-id",
    "x-gobby-machine-id",
    "x-gobby-key-id",
];
const SECRET_KEYS: &[&str] = &[
    "dsn",
    "password",
    "api_key",
    "token",
    "deployment_token",
    "payload_checksum",
    "signature",
    "proof",
];
/// The gdaemon-authored family: its typed 503 covers every proxied path, so it is not
/// a `RouteFamily` and the parity precheck exempts it.
const SYNTHETIC_FAMILY: &str = "front_door";

fn read_json(path: &Path) -> Value {
    let text = std::fs::read_to_string(path)
        .unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
    serde_json::from_str(&text).unwrap_or_else(|error| panic!("parse {}: {error}", path.display()))
}

fn load_manifest() -> Value {
    read_json(&Path::new(CORPUS_DIR).join("manifest.json"))
}

/// Every manifest case in order, rejecting any whose `schema_version` differs.
fn load_all_cases(manifest: &Value) -> Vec<Value> {
    let version = &manifest["schema_version"];
    let families = manifest["families"].as_object().expect("manifest families");
    manifest["cases"]
        .as_array()
        .expect("manifest cases")
        .iter()
        .map(|file| {
            let file = file.as_str().expect("case file name");
            let case = read_json(&Path::new(CORPUS_DIR).join(file));
            assert_eq!(
                &case["schema_version"], version,
                "{file}: schema_version differs from the manifest"
            );
            let family = case["family"].as_str().expect("case family");
            assert!(
                families.contains_key(family),
                "{file}: family {family:?} is not in the manifest"
            );
            case
        })
        .collect()
}

fn family_field<'a>(manifest: &'a Value, family: &str, field: &str) -> &'a str {
    manifest["families"][family][field]
        .as_str()
        .unwrap_or_else(|| panic!("family {family:?} has no {field}"))
}

/// The manifest cases whose family matches `select(parity, origin)`.
fn cases_where(select: impl Fn(&str, &str) -> bool) -> Vec<Value> {
    let manifest = load_manifest();
    let cases: Vec<Value> = load_all_cases(&manifest)
        .into_iter()
        .filter(|case| {
            let family = case["family"].as_str().expect("case family");
            select(
                family_field(&manifest, family, "parity"),
                family_field(&manifest, family, "origin"),
            )
        })
        .collect();
    assert!(!cases.is_empty(), "no manifest case matches the selection");
    cases
}

fn parity(value: &str) -> RouteBackend {
    match value {
        "proxy" => RouteBackend::Proxy,
        "native" => RouteBackend::Native,
        other => panic!("unknown parity {other:?}"),
    }
}

fn redact_secrets(value: &mut Value) {
    match value {
        Value::Object(object) => {
            for (key, item) in object.iter_mut() {
                if SECRET_KEYS.contains(&key.as_str()) {
                    *item = Value::String(SECRET.to_owned());
                } else {
                    redact_secrets(item);
                }
            }
        }
        Value::Array(items) => items.iter_mut().for_each(redact_secrets),
        _ => {}
    }
}

/// Replace each pointed-at value with `MASK`; a pointer to an absent field is a no-op.
fn apply_masks(document: &mut Value, pointers: &Value) {
    for pointer in pointers.as_array().expect("mask list") {
        let pointer = pointer.as_str().expect("mask pointer");
        assert!(
            pointer.starts_with('/'),
            "mask pointer {pointer:?} must start with '/'"
        );
        if let Some(value) = document.pointer_mut(pointer) {
            *value = Value::String(MASK.to_owned());
        }
    }
}

/// Redact then mask a `{"response": ...}` document and return its response.
fn normalized_response(mut document: Value, mask: &Value) -> Value {
    redact_secrets(&mut document["response"]["body"]);
    apply_masks(&mut document, mask);
    document["response"].take()
}

fn is_json(content_type: Option<&str>) -> bool {
    content_type.is_some_and(|value| value.starts_with("application/json"))
}

fn percent_encode(text: &str) -> String {
    text.bytes()
        .map(|byte| {
            if byte.is_ascii_alphanumeric() || b"-._~".contains(&byte) {
                char::from(byte).to_string()
            } else {
                format!("%{byte:02X}")
            }
        })
        .collect()
}

fn request_target(request: &Value) -> String {
    let path = request["path"].as_str().expect("request path");
    let query = request["query"].as_object().expect("request query");
    if query.is_empty() {
        return path.to_owned();
    }
    let pairs: Vec<String> = query
        .iter()
        .map(|(key, value)| {
            let value = value.as_str().expect("query values are strings");
            format!("{}={}", percent_encode(key), percent_encode(value))
        })
        .collect();
    format!("{path}?{}", pairs.join("&"))
}

/// What the stub backend received.
struct Seen {
    method: String,
    target: String,
    headers: HeaderMap,
    body: Bytes,
}

type SeenLog = Arc<Mutex<Vec<Seen>>>;

fn recorded_response(recorded: &Value) -> Response<Full<Bytes>> {
    let status = u16::try_from(recorded["status"].as_u64().expect("recorded status"))
        .expect("status fits u16");
    let headers = recorded["headers"].as_object().expect("recorded headers");
    let mut builder = Response::builder().status(status);
    for (name, value) in headers {
        builder = builder.header(name, value.as_str().expect("header value"));
    }
    let content_type = headers.get("content-type").and_then(Value::as_str);
    let body = match &recorded["body"] {
        Value::Null => Bytes::new(),
        Value::String(text) if !is_json(content_type) => Bytes::from(text.clone()),
        body => Bytes::from(serde_json::to_vec(body).expect("serialize body")),
    };
    builder.body(Full::new(body)).expect("recorded response")
}

/// A loopback backend that logs every request and answers with the case's recording.
async fn stub_backend(case: &Value) -> (SocketAddr, SeenLog) {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind stub");
    let addr = listener.local_addr().expect("stub addr");
    let seen: SeenLog = Arc::default();
    let log = Arc::clone(&seen);
    let recorded = case["response"].clone();
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            let log = Arc::clone(&log);
            let recorded = recorded.clone();
            tokio::spawn(async move {
                let service = service_fn(move |request: Request<Incoming>| {
                    let log = Arc::clone(&log);
                    let response = recorded_response(&recorded);
                    async move {
                        let (parts, body) = request.into_parts();
                        let body = body.collect().await.expect("request body").to_bytes();
                        log.lock().expect("seen log").push(Seen {
                            method: parts.method.to_string(),
                            target: parts.uri.to_string(),
                            headers: parts.headers,
                            body,
                        });
                        Ok::<_, Infallible>(response)
                    }
                });
                let _ = server_http1::Builder::new()
                    .serve_connection(TokioIo::new(stream), service)
                    .await;
            });
        }
    });
    (addr, seen)
}

/// A loopback address bound without listening, so connections to it are refused for as
/// long as the returned socket is held.
fn refusing_backend() -> (TcpSocket, SocketAddr) {
    let socket = TcpSocket::new_v4().expect("socket");
    socket
        .bind("127.0.0.1:0".parse().expect("loopback"))
        .expect("bind refusing backend");
    let addr = socket.local_addr().expect("refusing addr");
    (socket, addr)
}

async fn start_front_door(
    backend: SocketAddr,
    routes: BTreeMap<String, RouteBackend>,
) -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind front door");
    let addr = listener.local_addr().expect("front door addr");
    tokio::spawn(async move {
        serve(
            vec![PublicListener { listener, backend }],
            &routes,
            None,
            std::future::pending(),
        )
        .await
        .expect("front door serve");
    });
    addr
}

/// Send the case's request through the front door; return its normalized response and
/// the raw response headers.
async fn replay(front_door: SocketAddr, case: &Value) -> (Value, HeaderMap) {
    let request = &case["request"];
    let mut builder = Request::builder()
        .method(request["method"].as_str().expect("request method"))
        .uri(request_target(request))
        .header("host", "localhost");
    for (name, value) in request["headers"].as_object().expect("request headers") {
        builder = builder.header(name, value.as_str().expect("header value"));
    }
    let body = match &request["body"] {
        Value::Null => Bytes::new(),
        body => {
            builder = builder.header("content-type", "application/json");
            Bytes::from(serde_json::to_vec(body).expect("serialize request body"))
        }
    };
    let request = builder.body(Full::new(body)).expect("request");

    let stream = TcpStream::connect(front_door)
        .await
        .expect("connect front door");
    let (mut sender, connection) = hyper::client::conn::http1::handshake(TokioIo::new(stream))
        .await
        .expect("client handshake");
    tokio::spawn(connection);
    let response = tokio::time::timeout(TIMEOUT, sender.send_request(request))
        .await
        .expect("response timed out")
        .expect("send request");

    let (parts, body) = response.into_parts();
    let body = body.collect().await.expect("response body").to_bytes();
    let content_type = parts
        .headers
        .get("content-type")
        .map(|value| value.to_str().expect("content-type is text"));
    let body = if body.is_empty() {
        Value::Null
    } else if is_json(content_type) {
        serde_json::from_slice(&body).expect("json response body")
    } else {
        Value::String(String::from_utf8(body.to_vec()).expect("utf-8 response body"))
    };
    let headers: Map<String, Value> = RESPONSE_HEADER_ALLOWLIST
        .iter()
        .filter_map(|name| {
            let value = parts.headers.get(*name)?;
            let value = value.to_str().expect("allowlisted header is text");
            Some(((*name).to_owned(), Value::String(value.to_owned())))
        })
        .collect();
    let captured = json!({
        "response": {"status": parts.status.as_u16(), "headers": headers, "body": body},
    });
    (normalized_response(captured, &case["mask"]), parts.headers)
}

fn expected_response(case: &Value) -> Value {
    normalized_response(case.clone(), &case["mask"])
}

/// Assert the stub received exactly the case's request.
fn assert_request_unchanged(case: &Value, seen: &SeenLog) {
    let name = case["name"].as_str().expect("case name");
    let request = &case["request"];
    let seen = seen.lock().expect("seen log");
    assert_eq!(seen.len(), 1, "{name}: stub request count");
    let seen = &seen[0];
    assert_eq!(
        seen.method,
        request["method"].as_str().expect("method"),
        "{name}"
    );
    assert_eq!(seen.target, request_target(request), "{name}");
    for (header, value) in request["headers"].as_object().expect("request headers") {
        let received = seen
            .headers
            .get(header.to_ascii_lowercase())
            .map(|value| value.to_str().expect("header is text"));
        assert_eq!(received, value.as_str(), "{name}: request header {header}");
    }
    match &request["body"] {
        Value::Null => assert!(seen.body.is_empty(), "{name}: request body"),
        body => {
            let received: Value = serde_json::from_slice(&seen.body).expect("json request body");
            assert_eq!(&received, body, "{name}: request body");
        }
    }
}

/// Every parity violation between the manifest and the families gdaemon registers.
fn parity_violations(
    manifest_families: &Map<String, Value>,
    case_families: &BTreeSet<String>,
    families: &[&dyn RouteFamily],
) -> Vec<String> {
    let mut violations = Vec::new();
    for family in families {
        let name = family.name();
        if !case_families.contains(name) {
            violations.push(format!("{name}: registered family has no corpus case"));
        }
        let declared = manifest_families
            .get(name)
            .and_then(|entry| entry["parity"].as_str());
        if declared != Some("native") {
            violations.push(format!(
                "{name}: registered family has parity {declared:?}, expected \"native\""
            ));
        }
    }
    let routes: BTreeMap<String, RouteBackend> = manifest_families
        .iter()
        .filter(|(name, _)| name.as_str() != SYNTHETIC_FAMILY)
        .map(|(name, entry)| {
            let declared = entry["parity"].as_str().expect("family parity");
            (name.clone(), parity(declared))
        })
        .collect();
    for name in unimplemented_families(families, &routes) {
        violations.push(format!(
            "{name}: native parity but gdaemon registers no such family"
        ));
    }
    violations
}

#[tokio::test]
async fn proxy_families_replay_equal() {
    for case in cases_where(|parity, origin| parity == "proxy" && origin == "python") {
        let family = case["family"].as_str().expect("case family");
        let (backend, seen) = stub_backend(&case).await;
        let routes = BTreeMap::from([(family.to_owned(), RouteBackend::Proxy)]);
        let front_door = start_front_door(backend, routes).await;

        let (actual, _) = replay(front_door, &case).await;

        assert_eq!(actual, expected_response(&case), "{}", case["name"]);
        assert_request_unchanged(&case, &seen);
    }
}

#[tokio::test]
async fn native_health_replays_equal() {
    for case in cases_where(|_, origin| origin == "python") {
        if case["family"] != "health" {
            continue;
        }
        assert_eq!(family_field(&load_manifest(), "health", "parity"), "native");
        let (backend, seen) = stub_backend(&case).await;
        let routes = BTreeMap::from([("health".to_owned(), RouteBackend::Native)]);
        let front_door = start_front_door(backend, routes).await;

        let (actual, headers) = replay(front_door, &case).await;

        assert_eq!(actual, expected_response(&case), "{}", case["name"]);
        assert_eq!(headers[SERVED_BY_HEADER], "gdaemon", "{}", case["name"]);
        assert_request_unchanged(&case, &seen);
    }
}

#[tokio::test]
async fn front_door_backend_down_replays_equal() {
    let cases = cases_where(|_, origin| origin == "gdaemon");
    assert!(
        cases.iter().any(|case| case["family"] == SYNTHETIC_FAMILY),
        "the corpus has no front_door case"
    );
    for case in cases {
        let (held, backend) = refusing_backend();
        let front_door = start_front_door(backend, BTreeMap::new()).await;

        let (actual, _) = replay(front_door, &case).await;

        assert_eq!(actual, expected_response(&case), "{}", case["name"]);
        drop(held);
    }
}

#[test]
fn mask_matches_python_vector() {
    let vector = read_json(&Path::new(CORPUS_DIR).join("mask_vector.json"));
    let entries = vector.as_array().expect("mask vector entries");
    assert!(!entries.is_empty(), "mask vector has no entries");
    for entry in entries {
        let mut document = entry["input"].clone();
        redact_secrets(&mut document);
        apply_masks(&mut document, &entry["mask"]);
        assert_eq!(document, entry["expected"], "{}", entry["name"]);
    }
}

#[test]
fn every_native_family_has_corpus_cases() {
    let manifest = load_manifest();
    let case_families: BTreeSet<String> = load_all_cases(&manifest)
        .iter()
        .map(|case| case["family"].as_str().expect("case family").to_owned())
        .collect();
    let families = manifest["families"].as_object().expect("manifest families");

    let violations = parity_violations(families, &case_families, FAMILIES);

    assert!(violations.is_empty(), "parity violations: {violations:#?}");
}

#[test]
fn precheck_rejects_missing_or_mismatched_parity() {
    let families = |value: Value| value.as_object().expect("families").clone();
    let cases = |names: &[&str]| -> BTreeSet<String> {
        names.iter().map(|name| (*name).to_owned()).collect()
    };

    let missing_case = parity_violations(
        &families(json!({"health": {"parity": "native", "origin": "python"}})),
        &cases(&[]),
        FAMILIES,
    );
    assert_eq!(
        missing_case,
        ["health: registered family has no corpus case"]
    );

    let proxied = parity_violations(
        &families(json!({"health": {"parity": "proxy", "origin": "python"}})),
        &cases(&["health"]),
        FAMILIES,
    );
    assert_eq!(
        proxied,
        ["health: registered family has parity Some(\"proxy\"), expected \"native\""]
    );

    let unregistered = parity_violations(
        &families(json!({
            "health": {"parity": "native", "origin": "python"},
            "tasks": {"parity": "native", "origin": "python"},
            "front_door": {"parity": "native", "origin": "gdaemon"},
        })),
        &cases(&["health", "tasks", "front_door"]),
        FAMILIES,
    );
    assert_eq!(
        unregistered,
        ["tasks: native parity but gdaemon registers no such family"]
    );
}
