//! The native public WebSocket transport and its private bind and publish
//! endpoints, driven end to end. An in-process public listener serves
//! `NativeWs::accept` (`/ws` as the browser entry, every other path as the
//! direct entry), the private endpoints are called through `publish::handle`,
//! and a scripted stub bridge stands in for Python's `/_gobby/ws/bridge`. The
//! pair secret and the supervisor tokens are synthetic; no daemon, database or
//! credential is involved.

mod common;

use std::collections::{BTreeMap, BTreeSet};
use std::future::Future;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::body::Body;
use axum::http::header::{CONNECTION, UPGRADE};
use axum::http::{Request, Response, StatusCode};
use futures_util::{SinkExt, StreamExt};
use gobby_daemon::front_door::auth::AuthState;
use gobby_daemon::front_door::health::BackendState;
use gobby_daemon::front_door::routes::{FAMILIES, RouteTable};
use gobby_daemon::front_door::{FrontDoor, FrontDoorState};
use gobby_daemon::lifecycle::backend::BackendStatus;
use gobby_daemon::websocket::broker::BroadcastBroker;
use gobby_daemon::websocket::publish;
use gobby_daemon::websocket::subscriptions::EntryMode;
use gobby_daemon::websocket::transport::{NativeWs, Site};
use gobby_terminals::write_api::{TokenLease, TokenSlot};
use http_body_util::BodyExt;
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper_util::rt::TokioIo;
use serde_json::{Value, json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, watch};
use tokio::time::{sleep, timeout};
use tokio_tungstenite::WebSocketStream;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::handshake::derive_accept_key;
use tokio_tungstenite::tungstenite::http::HeaderMap;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::{CloseFrame, Role};

const SECRET: &str = "pair-secret";
const TOKEN_A: &str = "token-a";
const TOKEN_B: &str = "token-b";
const TOKEN_C: &str = "token-c";
const TOKEN_D: &str = "token-d";
/// Bound on every awaited step, so a stall fails inside the test body.
const STEP: Duration = Duration::from_secs(5);
const LOOPBACK: &str = "127.0.0.1:40000";
const DIRECT: &str = "/direct";
const BROWSER: &str = "/ws";
const BIND: &str = "/_gobby/ws/bind";
const PUBLISH: &str = "/_gobby/ws/publish";
/// RFC 6455 section 1.3's sample key and its accept value.
const KEY: &str = "dGhlIHNhbXBsZSBub25jZQ==";
const ACCEPT: &str = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=";
const MAX_MESSAGE: u64 = 64 * 1024;
const PUBLISH_BOUND: u64 = 128 * 1024 * 1024;
/// A leg ID gdaemon never assigns in these tests.
const UNREGISTERED_LEG: u64 = 4_000_000_000;
const SUBSCRIBE_SESSION: &str = r#"{"type": "subscribe", "events": ["session_event"]}"#;
const UNSUBSCRIBE_SESSION: &str = r#"{"type": "unsubscribe", "events": ["session_event"]}"#;

async fn within<T>(context: &str, future: impl Future<Output = T>) -> T {
    match timeout(STEP, future).await {
        Ok(value) => value,
        Err(_) => panic!("{context}: no progress within {STEP:?}"),
    }
}

/// One private leg as the stub bridge accepted it.
struct Leg {
    path: String,
    headers: HeaderMap,
    ws: WebSocketStream<TokioIo<hyper::upgrade::Upgraded>>,
}

impl Leg {
    fn header(&self, name: &str) -> Vec<String> {
        self.headers
            .get_all(name)
            .iter()
            .map(|value| value.to_str().expect("ASCII header").to_owned())
            .collect()
    }

    fn single(&self, name: &str) -> String {
        let values = self.header(name);
        assert_eq!(
            values.len(),
            1,
            "{name} must appear exactly once: {values:?}"
        );
        values[0].clone()
    }

    fn id(&self) -> u64 {
        self.single("x-gobby-ws-leg")
            .parse()
            .expect("decimal leg ID")
    }

    async fn send(&mut self, message: Message) {
        within("bridge send", self.ws.send(message))
            .await
            .expect("bridge send");
    }

    async fn next(&mut self, context: &str) -> Message {
        loop {
            match within(context, self.ws.next()).await {
                Some(Ok(Message::Ping(_) | Message::Pong(_))) => {}
                Some(Ok(message)) => return message,
                other => panic!("{context}: private leg ended: {other:?}"),
            }
        }
    }
}

/// The public client after a 101.
struct Public {
    ws: WebSocketStream<TcpStream>,
    head: String,
}

impl Public {
    async fn upgraded(stream: TcpStream, head: String, rest: Vec<u8>) -> Self {
        assert_eq!(status(&head), 101, "expected an upgrade: {head}");
        let ws = WebSocketStream::from_partially_read(stream, rest, Role::Client, None).await;
        Self { ws, head }
    }

    async fn send(&mut self, message: Message) {
        within("public send", self.ws.send(message))
            .await
            .expect("public send");
    }

    async fn next(&mut self, context: &str) -> Message {
        loop {
            match within(context, self.ws.next()).await {
                Some(Ok(Message::Ping(_) | Message::Pong(_))) => {}
                Some(Ok(message)) => return message,
                other => panic!("{context}: public stream ended: {other:?}"),
            }
        }
    }

    async fn text(&mut self, context: &str) -> String {
        match self.next(context).await {
            Message::Text(text) => text.as_str().to_owned(),
            other => panic!("{context}: expected a text frame, got {other:?}"),
        }
    }

    async fn close(&mut self, context: &str) -> (u16, String) {
        match self.next(context).await {
            Message::Close(Some(frame)) => {
                (u16::from(frame.code), frame.reason.as_str().to_owned())
            }
            other => panic!("{context}: expected a close frame, got {other:?}"),
        }
    }

    async fn close_code(&mut self, context: &str) -> u16 {
        self.close(context).await.0
    }
}

fn close_code_of(message: &Message, context: &str) -> u16 {
    match message {
        Message::Close(Some(frame)) => u16::from(frame.code),
        other => panic!("{context}: expected a close frame, got {other:?}"),
    }
}

/// Every frame `public` receives before the bridge's `marker` reply. The
/// marker shares the connection FIFO, so nothing queued earlier is missed.
async fn frames_before_marker(public: &mut Public, leg: &mut Leg, marker: &str) -> Vec<Message> {
    leg.send(Message::text(marker)).await;
    let mut frames = Vec::new();
    loop {
        match public.next(marker).await {
            Message::Text(text) if text.as_str() == marker => return frames,
            other => frames.push(other),
        }
    }
}

fn texts(values: &[&Value]) -> Vec<Message> {
    values
        .iter()
        .map(|value| Message::text(value.to_string()))
        .collect()
}

/// Accepts private legs and hands each to the test with its request.
async fn stub_bridge() -> (SocketAddr, mpsc::UnboundedReceiver<Leg>) {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind stub bridge");
    let addr = listener.local_addr().expect("stub bridge addr");
    let (tx, rx) = mpsc::unbounded_channel();
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            let tx = tx.clone();
            tokio::spawn(async move {
                let service = service_fn(move |request: Request<hyper::body::Incoming>| {
                    let tx = tx.clone();
                    async move { Ok::<_, std::convert::Infallible>(accept_leg(request, tx)) }
                });
                let _ = http1::Builder::new()
                    .serve_connection(TokioIo::new(stream), service)
                    .with_upgrades()
                    .await;
            });
        }
    });
    (addr, rx)
}

/// Answers an upgrade request with 101 and sends its leg once the connection upgrades.
fn accept_leg(
    mut request: Request<hyper::body::Incoming>,
    tx: mpsc::UnboundedSender<Leg>,
) -> Response<Body> {
    let Some(key) = request.headers().get("sec-websocket-key") else {
        return Response::builder()
            .status(StatusCode::BAD_REQUEST)
            .body(Body::empty())
            .expect("static response");
    };
    let accept = derive_accept_key(key.as_bytes());
    let path = request.uri().to_string();
    let headers = request.headers().clone();
    let upgrade = hyper::upgrade::on(&mut request);
    tokio::spawn(async move {
        let Ok(upgraded) = upgrade.await else {
            return;
        };
        let ws =
            WebSocketStream::from_raw_socket(TokioIo::new(upgraded), Role::Server, None).await;
        let _ = tx.send(Leg { path, headers, ws });
    });
    Response::builder()
        .status(StatusCode::SWITCHING_PROTOCOLS)
        .header(UPGRADE, "websocket")
        .header(CONNECTION, "upgrade")
        .header("sec-websocket-accept", accept)
        .body(Body::empty())
        .expect("static response")
}

/// A backend that accepts connections and never answers.
async fn silent_backend() -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind silent backend");
    let addr = listener.local_addr().expect("silent backend addr");
    tokio::spawn(async move {
        let mut held = Vec::new();
        while let Ok((stream, _)) = listener.accept().await {
            held.push(stream);
        }
    });
    addr
}

async fn serve_public(native: NativeWs) -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind public listener");
    let addr = listener.local_addr().expect("public addr");
    tokio::spawn(async move {
        while let Ok((stream, peer)) = listener.accept().await {
            let native = native.clone();
            tokio::spawn(async move {
                let service = service_fn(move |request: Request<hyper::body::Incoming>| {
                    let native = native.clone();
                    async move {
                        let request = request.map(Body::new);
                        let entry = if request.uri().path() == BROWSER {
                            EntryMode::Browser
                        } else {
                            EntryMode::Direct
                        };
                        Ok::<_, std::convert::Infallible>(native.accept(request, entry, peer).await)
                    }
                });
                let _ = http1::Builder::new()
                    .serve_connection(TokioIo::new(stream), service)
                    .with_upgrades()
                    .await;
            });
        }
    });
    addr
}

fn upgrade_request(path: &str, extra: &[(&str, &str)]) -> String {
    let mut request = format!(
        "GET {path} HTTP/1.1\r\nhost: gobby.test\r\nupgrade: websocket\r\nconnection: Upgrade\r\n\
         sec-websocket-key: {KEY}\r\nsec-websocket-version: 13\r\n"
    );
    for (name, value) in extra {
        request.push_str(&format!("{name}: {value}\r\n"));
    }
    request.push_str("\r\n");
    request
}

async fn handshake(addr: SocketAddr, request: String) -> (TcpStream, String, Vec<u8>) {
    let mut stream = TcpStream::connect(addr).await.expect("connect public");
    stream
        .write_all(request.as_bytes())
        .await
        .expect("write handshake");
    let (head, rest) = common::read_head(&mut stream).await;
    (stream, head, rest)
}

fn status(head: &str) -> u16 {
    head.split_whitespace()
        .nth(1)
        .and_then(|code| code.parse().ok())
        .unwrap_or_else(|| panic!("no status in {head}"))
}

fn header_value(head: &str, name: &str) -> Option<String> {
    head.lines().find_map(|line| {
        let (key, value) = line.split_once(':')?;
        key.trim()
            .eq_ignore_ascii_case(name)
            .then(|| value.trim().to_owned())
    })
}

async fn body_after(stream: &mut TcpStream, head: &str, mut rest: Vec<u8>) -> Vec<u8> {
    let length: usize = header_value(head, "content-length")
        .expect("refusal carries a content-length")
        .parse()
        .expect("numeric content-length");
    within(
        "refusal body",
        common::read_exact_into(stream, &mut rest, length),
    )
    .await;
    rest.truncate(length);
    rest
}

fn ready_frame(generation: &str) -> String {
    json!({"type": "bridge_ready", "version": 1, "generation": generation, "admitted": true})
        .to_string()
}

fn settings(max_message_size: u64, ping_interval: u64, ping_timeout: u64) -> Value {
    json!({
        "max_message_size": max_message_size,
        "ping_interval": ping_interval,
        "ping_timeout": ping_timeout,
    })
}

fn default_settings() -> Value {
    settings(MAX_MESSAGE, 30, 10)
}

fn publish_op(message: &Value) -> Value {
    json!({"op": "publish", "filter": message, "frames": [message.to_string()]})
}

/// The subscribe operation's leg encoding, kept in this one place.
fn leg_json(leg: u64) -> Value {
    json!(leg)
}

fn subscribe_op(leg: u64, events: &[&str]) -> Value {
    json!({"op": "subscribe", "leg": leg_json(leg), "events": events})
}

fn private_request(
    path: &str,
    secret: Option<&str>,
    token: Option<&str>,
    body: impl Into<Body>,
) -> Request<Body> {
    let mut builder = Request::builder()
        .method("POST")
        .uri(path)
        .header("content-type", "application/json");
    if let Some(secret) = secret {
        builder = builder.header("x-gobby-front-door", secret);
    }
    if let Some(token) = token {
        builder = builder.header("x-gobby-backend-token", token);
    }
    builder.body(body.into()).expect("private request")
}

async fn call(native: &NativeWs, request: Request<Body>, peer: &str) -> (StatusCode, Vec<u8>) {
    let peer: SocketAddr = peer.parse().expect("peer address");
    let response = within("private endpoint", publish::handle(native, request, peer)).await;
    let status = response.status();
    let body = within("private body", response.into_body().collect())
        .await
        .expect("private body")
        .to_bytes()
        .to_vec();
    (status, body)
}

async fn bind_as(native: NativeWs, token: String, settings: Value) -> (StatusCode, Vec<u8>) {
    let request = private_request(BIND, Some(SECRET), Some(&token), settings.to_string());
    call(&native, request, LOOPBACK).await
}

async fn publish_as(native: NativeWs, token: String, generation: String, ops: Value) -> StatusCode {
    let body = json!({"generation": generation, "ops": ops}).to_string();
    let request = private_request(PUBLISH, Some(SECRET), Some(&token), body);
    call(&native, request, LOOPBACK).await.0
}

async fn wait_connections(broker: &BroadcastBroker, expected: usize) {
    let context = format!("{expected} registered connections");
    within(&context, async {
        while broker.connection_count() != expected {
            sleep(Duration::from_millis(5)).await;
        }
    })
    .await;
}

struct Harness {
    native: NativeWs,
    broker: BroadcastBroker,
    slot: TokenSlot,
    token: String,
    leases: Vec<TokenLease>,
    public: SocketAddr,
    legs: mpsc::UnboundedReceiver<Leg>,
    generation: String,
}

impl Harness {
    async fn start() -> Self {
        let (bridge, legs) = stub_bridge().await;
        Self::serve(bridge, legs).await
    }

    async fn with_backend(backend: SocketAddr) -> Self {
        let (_, legs) = mpsc::unbounded_channel();
        Self::serve(backend, legs).await
    }

    async fn serve(backend: SocketAddr, legs: mpsc::UnboundedReceiver<Leg>) -> Self {
        let broker = BroadcastBroker::default();
        let slot = TokenSlot::default();
        let lease = slot.install(TOKEN_A.to_owned());
        let native = NativeWs::new(
            broker.clone(),
            SECRET.to_owned(),
            Some(slot.clone()),
            backend,
        );
        let public = serve_public(native.clone()).await;
        Self {
            native,
            broker,
            slot,
            token: TOKEN_A.to_owned(),
            leases: vec![lease],
            public,
            legs,
            generation: String::new(),
        }
    }

    async fn bind_settings(&mut self, settings: Value) -> String {
        let (status, body) = bind_as(self.native.clone(), self.token.clone(), settings).await;
        assert_eq!(
            status,
            StatusCode::OK,
            "bind: {}",
            String::from_utf8_lossy(&body)
        );
        let reply: Value = serde_json::from_slice(&body).expect("bind reply is JSON");
        let generation = reply["generation"]
            .as_str()
            .expect("generation string")
            .to_owned();
        assert!(
            generation.len() == 32 && generation.bytes().all(|byte| byte.is_ascii_hexdigit()),
            "a 128-bit hex generation: {generation}"
        );
        self.generation = generation.clone();
        generation
    }

    async fn bind(&mut self) -> String {
        self.bind_settings(default_settings()).await
    }

    async fn publish(&self, ops: Value) {
        let context = ops.to_string();
        let status = publish_as(
            self.native.clone(),
            self.token.clone(),
            self.generation.clone(),
            ops,
        )
        .await;
        assert_eq!(status, StatusCode::NO_CONTENT, "publish {context}");
    }

    /// Rotates the supervisor token; earlier leases stay alive, so only the
    /// newest token authorizes.
    fn rotate(&mut self, token: &str) {
        self.leases.push(self.slot.install(token.to_owned()));
        self.token = token.to_owned();
    }

    async fn next_leg(&mut self) -> Leg {
        within("private leg opens", self.legs.recv())
            .await
            .expect("stub bridge is listening")
    }

    fn assert_no_leg(&mut self, context: &str) {
        assert!(
            self.legs.try_recv().is_err(),
            "{context}: a private leg opened"
        );
    }

    /// Starts a public handshake, answers its private leg's first frame with
    /// `first` (or nothing), and returns the public response with the leg.
    async fn open(
        &mut self,
        path: &str,
        extra: &[(&str, &str)],
        first: Option<Message>,
    ) -> (TcpStream, String, Vec<u8>, Leg) {
        let connecting = tokio::spawn(handshake(self.public, upgrade_request(path, extra)));
        let mut leg = self.next_leg().await;
        if let Some(first) = first {
            leg.send(first).await;
        }
        let (stream, head, rest) = within("public response", connecting)
            .await
            .expect("handshake task");
        (stream, head, rest, leg)
    }

    /// A public connection the stub bridge admits under the current generation.
    async fn admit(&mut self, path: &str, extra: &[(&str, &str)]) -> (Public, Leg) {
        let first = Message::text(ready_frame(&self.generation));
        let (stream, head, rest, leg) = self.open(path, extra, Some(first)).await;
        (Public::upgraded(stream, head, rest).await, leg)
    }

    /// The public response when no private leg is involved.
    async fn refused(&self, request: String) -> (TcpStream, String, Vec<u8>) {
        within("refused handshake", handshake(self.public, request)).await
    }
}

fn assert_reply(actual: &str, expected: &Value, context: &str) {
    match expected {
        Value::String(bytes) => assert_eq!(actual, bytes, "{context}: reply bytes"),
        Value::Object(reply) => {
            let ack = &reply["ack"];
            let actual: Value = serde_json::from_str(actual).expect("reply is JSON");
            assert_eq!(actual["type"], ack["type"], "{context}: ack type");
            let sorted = |value: &Value| -> Vec<String> {
                let mut events: Vec<String> = value
                    .as_array()
                    .expect("ack events")
                    .iter()
                    .map(|event| event.as_str().expect("ack event").to_owned())
                    .collect();
                events.sort();
                events
            };
            assert_eq!(
                sorted(&actual["events"]),
                sorted(&ack["events"]),
                "{context}: ack events"
            );
        }
        other => panic!("{context}: unsupported reply {other}"),
    }
}

fn fixtures_dir(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures")
        .join(name)
}

fn terminal_corpus() -> Vec<(String, String)> {
    let dir = fixtures_dir("terminal_ws_golden");
    let manifest: Value = serde_json::from_slice(
        &std::fs::read(dir.join("manifest.json")).expect("read terminal manifest"),
    )
    .expect("parse terminal manifest");
    manifest["fixtures"]
        .as_array()
        .expect("fixtures array")
        .iter()
        .map(|name| {
            let name = name.as_str().expect("fixture name").to_owned();
            let bytes = std::fs::read(dir.join(&name)).expect("read terminal fixture");
            let text = String::from_utf8(bytes).expect("terminal fixtures are text frames");
            (name, text)
        })
        .collect()
}

fn envelope_cases() -> Vec<Value> {
    let path = fixtures_dir("ws_envelope_golden").join("cases.json");
    let corpus: Value = serde_json::from_slice(&std::fs::read(path).expect("read envelope corpus"))
        .expect("parse envelope corpus");
    corpus["cases"].as_array().expect("envelope cases").clone()
}

#[tokio::test]
async fn native_leg_relays_frames_and_consumes_controls() {
    let mut h = Harness::start().await;
    h.bind().await;
    let extra = [
        ("authorization", "Bearer key-1"),
        ("cookie", "session=first"),
        ("cookie", "session=second"),
        ("x-gobby-break-glass", "glass"),
        ("origin", "http://gobby.test"),
        ("user-agent", "native-test"),
        ("x-gobby-user-id", "user-1"),
        ("x-gobby-machine-id", "machine-1"),
        ("x-gobby-key-id", "key-1"),
        ("X-Forwarded-Proto", "https"),
        ("x-forwarded-proto", "http"),
        ("X-Forwarded-For", "203.0.113.9"),
        ("X-GOBBY-WS-LEG", "999"),
        ("x-gobby-ws-generation", "forged"),
        ("X-Gobby-Ws-Entry", "browser"),
        ("x-gobby-ws-path", "/forged"),
        ("x-gobby-front-door", "forged"),
        ("X-Gobby-Front-Door", "forged-again"),
        ("sec-websocket-protocol", "chat"),
        ("sec-websocket-extensions", "permessage-deflate"),
        ("x-unlisted", "dropped"),
    ];
    let (mut public, mut leg) = h.admit("/direct?probe=1", &extra).await;

    // The public 101: the RFC accept key, no extension and no subprotocol.
    assert_eq!(
        header_value(&public.head, "sec-websocket-accept").as_deref(),
        Some(ACCEPT),
        "accept key: {}",
        public.head
    );
    let head = public.head.to_ascii_lowercase();
    assert!(
        !head.contains("sec-websocket-protocol") && !head.contains("sec-websocket-extensions"),
        "the 101 negotiates nothing: {}",
        public.head
    );

    // The private upgrade carries only allowlisted headers. Synthesized ones
    // appear exactly once whatever the public request repeated; allowlisted
    // public ones keep their first value.
    assert_eq!(leg.path, "/_gobby/ws/bridge");
    let expected = [
        ("x-gobby-front-door", SECRET.to_owned()),
        ("x-gobby-ws-generation", h.generation.clone()),
        ("x-gobby-ws-entry", "direct".to_owned()),
        ("x-gobby-ws-path", "/direct?probe=1".to_owned()),
        ("x-forwarded-for", "127.0.0.1".to_owned()),
        ("x-forwarded-proto", "https".to_owned()),
        ("x-gobby-user-id", "user-1".to_owned()),
        ("x-gobby-machine-id", "machine-1".to_owned()),
        ("x-gobby-key-id", "key-1".to_owned()),
        ("authorization", "Bearer key-1".to_owned()),
        ("cookie", "session=first".to_owned()),
        ("x-gobby-break-glass", "glass".to_owned()),
        ("origin", "http://gobby.test".to_owned()),
        ("user-agent", "native-test".to_owned()),
    ];
    for (name, value) in &expected {
        assert_eq!(leg.single(name), *value, "private {name}");
    }
    assert_ne!(leg.id(), 999, "the public leg header is never forwarded");
    let handshake_headers = [
        "host",
        "connection",
        "upgrade",
        "sec-websocket-key",
        "sec-websocket-version",
    ];
    let allowed: BTreeSet<&str> = expected
        .iter()
        .map(|(name, _)| *name)
        .chain(handshake_headers)
        .chain(["x-gobby-ws-leg"])
        .collect();
    let sent: BTreeSet<&str> = leg.headers.keys().map(|name| name.as_str()).collect();
    let unlisted: Vec<&&str> = sent.difference(&allowed).collect();
    assert!(
        unlisted.is_empty(),
        "unlisted private headers: {unlisted:?}"
    );
    wait_connections(&h.broker, 1).await;

    // Text and binary relay exactly in both directions. Client text must be a
    // JSON object: any other text is answered locally and never forwarded.
    let client_event = r#"{"type": "client_event"}"#;
    public.send(Message::text(client_event)).await;
    assert_eq!(leg.next("client event").await, Message::text(client_event));
    let client_bytes = vec![0xff, 0x00, 0xfe, 0x80];
    public.send(Message::binary(client_bytes.clone())).await;
    assert_eq!(
        leg.next("client binary").await,
        Message::binary(client_bytes)
    );
    leg.send(Message::text("bridge text")).await;
    assert_eq!(
        public.next("bridge text").await,
        Message::text("bridge text")
    );
    let reply_bytes = vec![0xc3, 0x28, 0x00, 0xff];
    leg.send(Message::binary(reply_bytes.clone())).await;
    assert_eq!(
        public.next("bridge binary").await,
        Message::binary(reply_bytes)
    );

    // Controls are consumed by the broker: the acknowledgement precedes the
    // newly selected event, and the bridge never sees either control.
    public.send(Message::text(SUBSCRIBE_SESSION)).await;
    assert_eq!(
        public.text("subscribe ack").await,
        r#"{"type": "subscribe_success", "events": ["session_event"]}"#
    );
    let event = json!({"type": "session_event", "event": "updated", "session_id": "s-1"});
    h.publish(json!([publish_op(&event)])).await;
    assert_eq!(public.text("selected event").await, event.to_string());
    public.send(Message::text(UNSUBSCRIBE_SESSION)).await;
    assert_eq!(
        public.text("unsubscribe ack").await,
        r#"{"type": "unsubscribe_success", "events": []}"#
    );
    h.publish(json!([publish_op(&event)])).await;
    // A direct reply of the same gated type is unfiltered, and it is the next
    // frame, so the filtered broadcast never arrived.
    let direct = json!({"type": "session_event", "event": "direct", "session_id": "s-1"});
    leg.send(Message::text(direct.to_string())).await;
    assert_eq!(public.text("direct reply").await, direct.to_string());
    let after = r#"{"type": "after_controls"}"#;
    public.send(Message::text(after)).await;
    assert_eq!(
        leg.next("after controls").await,
        Message::text(after),
        "no control reached the bridge"
    );
}

#[tokio::test]
async fn bridge_readiness_is_one_time_and_entry_specific() {
    let mut h = Harness::start().await;
    let generation = h.bind().await;

    // Direct refusal: the bridge's status and body, without 101.
    let body = "Unauthorized: Missing Authorization header\n";
    let refusal = json!({
        "type": "bridge_ready", "version": 1, "generation": generation,
        "admitted": false, "status": 401, "body": body,
    });
    let (mut stream, head, rest, _leg) = h
        .open(DIRECT, &[], Some(Message::text(refusal.to_string())))
        .await;
    assert_eq!(status(&head), 401, "direct refusal: {head}");
    assert_eq!(body_after(&mut stream, &head, rest).await, body.as_bytes());

    // Browser refusal: 101, then 4401.
    let refusal = json!({
        "type": "bridge_ready", "version": 1, "generation": generation,
        "admitted": false, "close_code": 4401, "reason": "Authentication required",
    });
    let (stream, head, rest, _leg) = h
        .open(BROWSER, &[], Some(Message::text(refusal.to_string())))
        .await;
    let mut public = Public::upgraded(stream, head, rest).await;
    assert_eq!(
        public.close("browser refusal").await,
        (4401, "Authentication required".to_owned())
    );

    // Anything but a current bridge_ready text frame fails closed, as unbound.
    let failures = [
        ("not JSON", Message::text("bridge_ready")),
        ("binary", Message::binary(ready_frame(&generation).into_bytes())),
        (
            "version 2",
            Message::text(
                json!({"type": "bridge_ready", "version": 2, "generation": generation, "admitted": true})
                    .to_string(),
            ),
        ),
        ("other generation", Message::text(ready_frame(&"0".repeat(32)))),
        (
            "other type",
            Message::text(json!({"type": "connection_established"}).to_string()),
        ),
        // Python's pre-admission refusals close 1008 without bridge_ready.
        (
            "bridge close",
            Message::Close(Some(CloseFrame {
                code: CloseCode::from(1008),
                reason: "Policy violation".into(),
            })),
        ),
    ];
    for (label, first) in failures {
        let (_stream, head, _rest, _leg) = h.open(DIRECT, &[], Some(first.clone())).await;
        assert_eq!(status(&head), 503, "direct {label}: {head}");
        let (stream, head, rest, _leg) = h.open(BROWSER, &[], Some(first)).await;
        let mut public = Public::upgraded(stream, head, rest).await;
        assert_eq!(public.close_code(label).await, 1013, "browser {label}");
    }
    let started = Instant::now();
    let (_stream, head, _rest, _leg) = h.open(DIRECT, &[], None).await;
    assert_eq!(status(&head), 503, "no first frame: {head}");
    assert!(
        started.elapsed() < Duration::from_millis(2500),
        "readiness waits at most two seconds: {:?}",
        started.elapsed()
    );
    assert_eq!(h.broker.connection_count(), 0, "no refusal registered");

    // After readiness every private frame is application data, including
    // ones shaped like bridge_ready, and none changes admission or generation.
    let (mut public, mut leg) = h.admit(DIRECT, &[]).await;
    wait_connections(&h.broker, 1).await;
    let lookalikes = [
        ready_frame(&generation),
        json!({
            "type": "bridge_ready", "version": 1, "generation": "f".repeat(32),
            "admitted": false, "status": 403, "body": "Forbidden",
        })
        .to_string(),
    ];
    for frame in lookalikes {
        leg.send(Message::text(frame.clone())).await;
        assert_eq!(public.text("later bridge_ready").await, frame);
    }
    let still_open = r#"{"type": "still_open"}"#;
    public.send(Message::text(still_open)).await;
    assert_eq!(leg.next("still open").await, Message::text(still_open));
    let (_second, second_leg) = h.admit(DIRECT, &[]).await;
    assert_eq!(second_leg.single("x-gobby-ws-generation"), generation);
    wait_connections(&h.broker, 2).await;
}

#[tokio::test]
async fn bind_and_publish_admit_only_the_current_pair() {
    let mut h = Harness::start().await;

    // Proof is checked before the body: a malformed body with bad proof is 403.
    let proofs = [
        (
            "non-loopback peer",
            "192.0.2.1:1",
            Some(SECRET),
            Some(TOKEN_A),
        ),
        ("missing secret", LOOPBACK, None, Some(TOKEN_A)),
        ("wrong secret", LOOPBACK, Some("pair-secreT"), Some(TOKEN_A)),
        ("missing token", LOOPBACK, Some(SECRET), None),
        ("wrong token", LOOPBACK, Some(SECRET), Some(TOKEN_B)),
    ];
    for (label, peer, secret, token) in proofs {
        for path in [BIND, PUBLISH] {
            let request = private_request(path, secret, token, "{not json");
            let (status, _) = call(&h.native, request, peer).await;
            assert_eq!(status, StatusCode::FORBIDDEN, "{label} on {path}");
        }
    }
    let request = private_request("/_gobby/ws/other", Some(SECRET), Some(TOKEN_A), "{}");
    assert_eq!(
        call(&h.native, request, LOOPBACK).await.0,
        StatusCode::NOT_FOUND
    );

    // Bind bodies are bounded and validated.
    let padded = json!({"max_message_size": MAX_MESSAGE, "ping_interval": 30, "ping_timeout": 10, "pad": "x".repeat(4096)});
    let (status, _) = bind_as(h.native.clone(), TOKEN_A.to_owned(), padded).await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE, "bind over 4 KiB");
    let malformed_binds = [
        json!({"max_message_size": "65536", "ping_interval": 30, "ping_timeout": 10}),
        json!({"max_message_size": MAX_MESSAGE, "ping_interval": 30}),
        json!({"max_message_size": MAX_MESSAGE, "ping_interval": 30, "ping_timeout": 10, "extra": 1}),
        json!({"max_message_size": -1, "ping_interval": 30, "ping_timeout": 10}),
        json!([MAX_MESSAGE, 30, 10]),
    ];
    for body in malformed_binds {
        let context = body.to_string();
        let (status, _) = bind_as(h.native.clone(), TOKEN_A.to_owned(), body).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "bind {context}");
    }

    // Each bind mints a fresh generation and retires the previous one's
    // connections with 1012.
    let first = h.bind().await;
    let (mut retired, _retired_leg) = h.admit(BROWSER, &[]).await;
    wait_connections(&h.broker, 1).await;
    let second = h.bind().await;
    assert_ne!(first, second, "a fresh generation");
    assert_eq!(retired.close_code("previous generation").await, 1012);
    wait_connections(&h.broker, 0).await;

    let (mut public, mut leg) = h.admit(BROWSER, &[]).await;
    wait_connections(&h.broker, 1).await;
    let task = json!({"type": "task_event", "event": "created", "task_id": "t-1"});

    // A stale generation is 409 and applies nothing.
    let status = publish_as(
        h.native.clone(),
        TOKEN_A.to_owned(),
        first,
        json!([publish_op(&task)]),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT, "stale generation");

    // A malformed body is 400 and applies nothing, even its valid operations.
    let malformed_ops = [
        json!([publish_op(&task), {"op": "unknown"}]),
        json!([publish_op(&task), {"op": "publish", "filter": task, "frames": [1]}]),
        json!([publish_op(&task), {"op": "publish", "filter": "task_event", "frames": ["{}"]}]),
        json!([publish_op(&task), {"op": "publish", "filter": task}]),
        json!([publish_op(&task), {"op": "subscribe", "leg": true, "events": ["session_event"]}]),
        json!([publish_op(&task), {"op": "subscribe", "leg": leg_json(leg.id()), "events": "session_event"}]),
        json!({"op": "publish"}),
    ];
    for ops in malformed_ops {
        let context = ops.to_string();
        let status = publish_as(h.native.clone(), TOKEN_A.to_owned(), second.clone(), ops).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "publish {context}");
    }
    for body in ["{not json", r#"{"ops": []}"#] {
        let request = private_request(PUBLISH, Some(SECRET), Some(TOKEN_A), body);
        assert_eq!(
            call(&h.native, request, LOOPBACK).await.0,
            StatusCode::BAD_REQUEST,
            "{body}"
        );
    }

    // Oversize input is 413: a frame over max_message_size, or a declared
    // body over the publish bound.
    let oversize = "x".repeat(MAX_MESSAGE as usize + 1);
    let ops = json!([publish_op(&task), {"op": "publish", "filter": task, "frames": [oversize]}]);
    let status = publish_as(h.native.clone(), TOKEN_A.to_owned(), second.clone(), ops).await;
    assert_eq!(
        status,
        StatusCode::PAYLOAD_TOO_LARGE,
        "frame over max_message_size"
    );
    let mut request = private_request(PUBLISH, Some(SECRET), Some(TOKEN_A), "{}");
    request.headers_mut().insert(
        "content-length",
        (PUBLISH_BOUND + 1)
            .to_string()
            .parse()
            .expect("header value"),
    );
    assert_eq!(
        call(&h.native, request, LOOPBACK).await.0,
        StatusCode::PAYLOAD_TOO_LARGE,
        "declared body over 128 MiB"
    );

    // Operations apply in order, each publish operation once.
    let a = json!({"type": "task_event", "event": "a"});
    let b = json!({"type": "task_event", "event": "b"});
    h.publish(json!([publish_op(&a), publish_op(&b)])).await;
    assert_eq!(
        frames_before_marker(&mut public, &mut leg, "after ordered publish").await,
        texts(&[&a, &b]),
        "nothing from the refused requests, then a and b once each"
    );

    // A subscribe operation changes only its named leg.
    let (mut other, mut other_leg) = h.admit(BROWSER, &[]).await;
    wait_connections(&h.broker, 2).await;
    let gated = json!({"type": "session_event", "event": "updated", "session_id": "s-1"});
    h.publish(json!([
        subscribe_op(leg.id(), &["session_event"]),
        publish_op(&gated)
    ]))
    .await;
    assert_eq!(
        frames_before_marker(&mut public, &mut leg, "subscribed leg").await,
        texts(&[&gated])
    );
    assert_eq!(
        frames_before_marker(&mut other, &mut other_leg, "other leg").await,
        Vec::<Message>::new()
    );

    // A token that rotated after the request was first proven is 403.
    let mut held = h.native.hold_next(Site::Publish);
    let pending = tokio::spawn(publish_as(
        h.native.clone(),
        TOKEN_A.to_owned(),
        second.clone(),
        json!([publish_op(&task)]),
    ));
    within("publish reaches its barrier", held.arrived()).await;
    h.rotate(TOKEN_B);
    held.release();
    let status = within("held publish", pending).await.expect("publish task");
    assert_eq!(status, StatusCode::FORBIDDEN, "token rotated after proof");
    assert_eq!(
        frames_before_marker(&mut public, &mut leg, "after rotated publish").await,
        Vec::<Message>::new()
    );
}

#[tokio::test]
async fn terminal_and_envelope_corpora_replay_native() {
    let mut h = Harness::start().await;
    h.bind().await;

    let corpus = terminal_corpus();
    assert!(!corpus.is_empty(), "terminal manifest lists no fixtures");
    let (mut public, mut leg) = h.admit(DIRECT, &[]).await;
    wait_connections(&h.broker, 1).await;
    for (name, text) in &corpus {
        public.send(Message::text(text.clone())).await;
        assert_eq!(
            leg.next(name).await,
            Message::text(text.clone()),
            "{name} to bridge"
        );
        leg.send(Message::text(text.clone())).await;
        assert_eq!(
            public.next(name).await,
            Message::text(text.clone()),
            "{name} to client"
        );
    }
    drop((public, leg));
    wait_connections(&h.broker, 0).await;

    let cases = envelope_cases();
    assert!(!cases.is_empty(), "envelope corpus lists no cases");
    for case in &cases {
        let name = case["name"].as_str().expect("case name");
        let mut clients: BTreeMap<String, (Public, Leg)> = BTreeMap::new();
        for (client, spec) in case["clients"].as_object().expect("clients") {
            // No native entry produces the explicit-null state.
            if spec["explicit_null"].as_bool() == Some(true) {
                continue;
            }
            let path = match spec["entry"].as_str() {
                Some("direct") => DIRECT,
                Some("browser") => BROWSER,
                other => panic!("{name}: unknown entry {other:?}"),
            };
            clients.insert(client.clone(), h.admit(path, &[]).await);
        }
        wait_connections(&h.broker, clients.len()).await;
        let mut pending: BTreeMap<String, Vec<String>> = BTreeMap::new();
        for (index, step) in case["steps"].as_array().expect("steps").iter().enumerate() {
            let context = format!("{name} step {index}");
            match step["op"].as_str().expect("step op") {
                "send" => {
                    let client = step["client"].as_str().expect("send client");
                    let Some((public, _)) = clients.get_mut(client) else {
                        continue;
                    };
                    public
                        .send(Message::text(step["text"].as_str().expect("send text")))
                        .await;
                    for earlier in pending.remove(client).unwrap_or_default() {
                        assert_eq!(
                            public.text(&context).await,
                            earlier,
                            "{context}: queued frame"
                        );
                    }
                    for reply in step["replies"].as_array().expect("replies") {
                        let actual = public.text(&context).await;
                        assert_reply(&actual, reply, &context);
                    }
                }
                "broadcast" => {
                    let message = &step["message"];
                    let deliveries = step["deliveries"].as_object().expect("deliveries");
                    let delivered = deliveries
                        .values()
                        .filter_map(|frames| frames.as_array())
                        .find(|frames| !frames.is_empty());
                    let wire: Vec<String> = match (delivered, step["frames"].as_array()) {
                        (Some(frames), _) => frames
                            .iter()
                            .map(|frame| frame.as_str().expect("wire frame").to_owned())
                            .collect(),
                        (None, Some(frames)) if !frames.is_empty() => {
                            frames.iter().map(Value::to_string).collect()
                        }
                        (None, _) => vec![message.to_string()],
                    };
                    h.publish(json!([{"op": "publish", "filter": message, "frames": wire}]))
                        .await;
                    for (client, frames) in deliveries {
                        if clients.contains_key(client) {
                            pending.entry(client.clone()).or_default().extend(
                                frames
                                    .as_array()
                                    .expect("delivered frames")
                                    .iter()
                                    .map(|frame| frame.as_str().expect("frame").to_owned()),
                            );
                        }
                    }
                }
                "direct" => {
                    let client = step["client"].as_str().expect("direct client");
                    let Some((_, leg)) = clients.get_mut(client) else {
                        continue;
                    };
                    let frames: Vec<String> = step["deliveries"][client]
                        .as_array()
                        .expect("direct deliveries")
                        .iter()
                        .map(|frame| frame.as_str().expect("frame").to_owned())
                        .collect();
                    for frame in &frames {
                        leg.send(Message::text(frame.clone())).await;
                    }
                    pending.entry(client.to_owned()).or_default().extend(frames);
                }
                other => panic!("{context}: unknown step {other}"),
            }
        }
        for (client, (public, leg)) in clients.iter_mut() {
            let context = format!("{name} end of case for {client}");
            let expected: Vec<Message> = pending
                .remove(client)
                .unwrap_or_default()
                .into_iter()
                .map(Message::text)
                .collect();
            assert_eq!(
                frames_before_marker(public, leg, &context).await,
                expected,
                "{context}: exactly the corpus deliveries"
            );
            let client_marker = r#"{"type": "client_marker"}"#;
            public.send(Message::text(client_marker)).await;
            assert_eq!(
                leg.next(&context).await,
                Message::text(client_marker),
                "{context}: no control reached the bridge"
            );
        }
        drop(clients);
        wait_connections(&h.broker, 0).await;
    }
}

#[tokio::test]
async fn failures_are_bounded_and_leave_no_residue() {
    // Unbound: direct 503 and browser 101 then 1013, with no private leg.
    let mut h = Harness::start().await;
    let started = Instant::now();
    let (_stream, head, _rest) = h.refused(upgrade_request(DIRECT, &[])).await;
    assert_eq!(status(&head), 503, "unbound direct: {head}");
    let (stream, head, rest) = h.refused(upgrade_request(BROWSER, &[])).await;
    let mut public = Public::upgraded(stream, head, rest).await;
    assert_eq!(
        public.close("unbound browser").await,
        (1013, "WebSocket server unavailable".to_owned())
    );
    assert!(
        started.elapsed() < Duration::from_secs(2),
        "unbound is prompt"
    );
    h.assert_no_leg("unbound");

    // Backend connect failure and backend timeout each end within two seconds.
    for (label, backend) in [
        ("connect refused", common::refused_addr().await),
        ("connect timeout", silent_backend().await),
    ] {
        let mut dead = Harness::with_backend(backend).await;
        dead.bind().await;
        let started = Instant::now();
        let (_stream, head, _rest) = dead.refused(upgrade_request(DIRECT, &[])).await;
        assert_eq!(status(&head), 503, "{label}: {head}");
        assert!(
            started.elapsed() < Duration::from_millis(2500),
            "{label} took {:?}",
            started.elapsed()
        );
        assert_eq!(dead.broker.connection_count(), 0, "{label}");
    }

    // An oversize public message closes 1009.
    h.bind_settings(settings(1024, 30, 10)).await;
    let (mut public, _leg) = h.admit(DIRECT, &[]).await;
    wait_connections(&h.broker, 1).await;
    public.send(Message::text("x".repeat(2048))).await;
    assert_eq!(public.close_code("oversize message").await, 1009);
    wait_connections(&h.broker, 0).await;

    // No pong within ping_timeout closes 1011. The client does not read, so
    // it never answers the ping.
    h.bind_settings(settings(MAX_MESSAGE, 1, 1)).await;
    let (mut public, _leg) = h.admit(DIRECT, &[]).await;
    wait_connections(&h.broker, 1).await;
    // Reading would answer the ping, so wait for the server to close on its own.
    wait_connections(&h.broker, 0).await;
    assert_eq!(public.close_code("ping timeout").await, 1011);

    // Private EOF closes the public side within one second, 1011 without a
    // close code; a bridge close code passes through.
    h.bind().await;
    let (mut public, leg) = h.admit(DIRECT, &[]).await;
    wait_connections(&h.broker, 1).await;
    drop(leg);
    let started = Instant::now();
    assert_eq!(public.close_code("private EOF").await, 1011);
    assert!(started.elapsed() < Duration::from_secs(1), "private EOF");
    wait_connections(&h.broker, 0).await;
    let (mut public, mut leg) = h.admit(DIRECT, &[]).await;
    wait_connections(&h.broker, 1).await;
    leg.send(Message::Close(Some(CloseFrame {
        code: CloseCode::from(4000),
        reason: "bridge done".into(),
    })))
    .await;
    assert_eq!(public.close_code("private close").await, 4000);
    wait_connections(&h.broker, 0).await;

    // Public close closes the private side with its code.
    let (mut public, mut leg) = h.admit(DIRECT, &[]).await;
    wait_connections(&h.broker, 1).await;
    public
        .send(Message::Close(Some(CloseFrame {
            code: CloseCode::Normal,
            reason: "bye".into(),
        })))
        .await;
    let started = Instant::now();
    let closed = leg.next("public close").await;
    assert_eq!(close_code_of(&closed, "public close"), 1000);
    assert!(started.elapsed() < Duration::from_secs(1), "public close");
    wait_connections(&h.broker, 0).await;

    // Shutdown closes every public connection 1001 and stops admission.
    let (mut direct, _direct_leg) = h.admit(DIRECT, &[]).await;
    let (mut browser, _browser_leg) = h.admit(BROWSER, &[]).await;
    wait_connections(&h.broker, 2).await;
    let started = Instant::now();
    within("shutdown", h.native.shutdown()).await;
    assert_eq!(direct.close_code("shutdown direct").await, 1001);
    assert_eq!(browser.close_code("shutdown browser").await, 1001);
    assert!(started.elapsed() < Duration::from_secs(1), "shutdown");
    wait_connections(&h.broker, 0).await;
    let (_stream, head, _rest) = h.refused(upgrade_request(DIRECT, &[])).await;
    assert_eq!(status(&head), 503, "after shutdown: {head}");
}

#[tokio::test]
async fn server_subscriptions_reach_only_the_named_leg() {
    let mut h = Harness::start().await;
    h.bind().await;
    let (mut direct, mut direct_leg) = h.admit(DIRECT, &[]).await;
    let (mut browser, mut browser_leg) = h.admit(BROWSER, &[]).await;
    let (mut direct_other, mut direct_other_leg) = h.admit(DIRECT, &[]).await;
    let (mut browser_other, mut browser_other_leg) = h.admit(BROWSER, &[]).await;
    wait_connections(&h.broker, 4).await;

    let selector = "workspace_event:workspace_id=w-1";
    let early = json!({"type": "workspace_event", "workspace_id": "w-1", "event": "early"});
    let event = json!({"type": "workspace_event", "workspace_id": "w-1", "event": "changed"});

    // Published before the subscribe: never delivered.
    h.publish(json!([publish_op(&early)])).await;
    // The subscribe itself sends nothing; an unregistered leg is a no-op.
    h.publish(json!([
        subscribe_op(direct_leg.id(), &[selector]),
        subscribe_op(browser_leg.id(), &[selector]),
        subscribe_op(UNREGISTERED_LEG, &[selector]),
    ]))
    .await;
    assert_eq!(
        frames_before_marker(&mut direct, &mut direct_leg, "direct after subscribe").await,
        Vec::<Message>::new()
    );
    assert_eq!(
        frames_before_marker(&mut browser, &mut browser_leg, "browser after subscribe").await,
        Vec::<Message>::new()
    );

    h.publish(json!([publish_op(&event)])).await;
    assert_eq!(
        frames_before_marker(&mut direct, &mut direct_leg, "direct from absent").await,
        texts(&[&event])
    );
    assert_eq!(
        frames_before_marker(&mut browser, &mut browser_leg, "browser from empty").await,
        texts(&[&event])
    );
    assert_eq!(
        frames_before_marker(&mut direct_other, &mut direct_other_leg, "other direct").await,
        Vec::<Message>::new()
    );
    assert_eq!(
        frames_before_marker(&mut browser_other, &mut browser_other_leg, "other browser").await,
        Vec::<Message>::new()
    );
    h.publish(json!([subscribe_op(UNREGISTERED_LEG, &["*"])]))
        .await;
    assert_eq!(h.broker.connection_count(), 4);
}

#[tokio::test]
async fn invalid_handshakes_are_refused_before_the_bridge() {
    let mut h = Harness::start().await;
    h.bind().await;
    let valid = upgrade_request(DIRECT, &[]);
    let key_line = format!("sec-websocket-key: {KEY}\r\n");
    let with = |line: &str| valid.replacen("\r\n\r\n", &format!("\r\n{line}\r\n\r\n"), 1);
    let cases = [
        ("POST", valid.replacen("GET ", "POST ", 1), 400),
        (
            "HTTP/1.0",
            valid.replacen(" HTTP/1.1\r\n", " HTTP/1.0\r\n", 1),
            400,
        ),
        (
            "missing Upgrade",
            valid.replacen("upgrade: websocket\r\n", "", 1),
            400,
        ),
        (
            "missing Connection upgrade",
            valid.replacen("connection: Upgrade", "connection: keep-alive", 1),
            400,
        ),
        (
            "missing version",
            valid.replacen("sec-websocket-version: 13\r\n", "", 1),
            426,
        ),
        (
            "wrong version",
            valid.replacen("sec-websocket-version: 13", "sec-websocket-version: 8", 1),
            426,
        ),
        ("missing key", valid.replacen(&key_line, "", 1), 400),
        ("short key", valid.replacen(KEY, "c2hvcnQ=", 1), 400),
        (
            "non-base64 key",
            valid.replacen(KEY, "!!!!!!!!!!!!!!!!!!!!!!==", 1),
            400,
        ),
        (
            "duplicate key",
            with(&format!("sec-websocket-key: {KEY}")),
            400,
        ),
        ("duplicate Upgrade", with("upgrade: websocket"), 400),
        ("duplicate Connection", with("connection: Upgrade"), 400),
        ("duplicate version", with("sec-websocket-version: 13"), 400),
    ];
    for (label, request, expected) in cases {
        let (_stream, head, _rest) = h.refused(request).await;
        assert_eq!(status(&head), expected, "{label}: {head}");
        if expected == 426 {
            assert!(
                common::head_has(&head, "sec-websocket-version: 13"),
                "{label} names the supported version: {head}"
            );
        }
    }
    h.assert_no_leg("refused handshakes");
    assert_eq!(h.broker.connection_count(), 0, "nothing registered");

    // The unmodified request opens a leg and upgrades.
    let (public, _leg) = h.admit(DIRECT, &[]).await;
    assert_eq!(
        header_value(&public.head, "sec-websocket-accept").as_deref(),
        Some(ACCEPT)
    );
    wait_connections(&h.broker, 1).await;
}

#[tokio::test]
async fn generation_changes_commit_under_the_binding_lock() {
    let mut h = Harness::start().await;
    let (status_tx, status_rx) = watch::channel(BackendStatus::Starting { pid: 1 });
    let watcher = h.native.clone();
    tokio::spawn(async move { watcher.retire_on_backend_change(status_rx).await });
    let task = json!({"type": "task_event", "event": "created", "task_id": "t-1"});
    let first = h.bind().await;

    // A rebind between readiness and registration closes that connection
    // 1012 on both sides and leaves it unregistered.
    let mut held = h.native.hold_next(Site::Register);
    let connecting = tokio::spawn(handshake(h.public, upgrade_request(DIRECT, &[])));
    let mut leg = h.next_leg().await;
    leg.send(Message::text(ready_frame(&first))).await;
    within("registration reaches its barrier", held.arrived()).await;
    let second = h.bind().await;
    held.release();
    let (stream, head, rest) = within("raced upgrade", connecting)
        .await
        .expect("handshake task");
    let mut public = Public::upgraded(stream, head, rest).await;
    assert_eq!(public.close_code("rebind before registration").await, 1012);
    let closed = leg.next("private side of the raced leg").await;
    assert_eq!(close_code_of(&closed, "private side"), 1012);
    assert_eq!(h.broker.connection_count(), 0, "raced leg unregistered");

    // An old-generation publish racing a rebind never reaches a
    // new-generation connection.
    let mut held = h.native.hold_next(Site::Publish);
    let stale = tokio::spawn(publish_as(
        h.native.clone(),
        h.token.clone(),
        second,
        json!([publish_op(&task)]),
    ));
    within("publish reaches its barrier", held.arrived()).await;
    h.bind().await;
    let (mut public, mut leg) = h.admit(BROWSER, &[]).await;
    wait_connections(&h.broker, 1).await;
    held.release();
    let publish_status = within("stale publish", stale).await.expect("publish task");
    assert_eq!(publish_status, StatusCode::CONFLICT, "old generation");
    assert_eq!(
        frames_before_marker(&mut public, &mut leg, "after the raced publish").await,
        Vec::<Message>::new()
    );

    // A status change whose proving token is no longer current retires that
    // generation.
    h.rotate(TOKEN_B);
    let mut checked = h.native.hold_next(Site::BackendChecked);
    status_tx
        .send(BackendStatus::Starting { pid: 2 })
        .expect("watcher listens");
    within("status change applied", checked.arrived()).await;
    checked.release();
    assert_eq!(public.close_code("rotated token").await, 1012);
    wait_connections(&h.broker, 0).await;
    let (_stream, head, _rest) = h.refused(upgrade_request(DIRECT, &[])).await;
    assert_eq!(status(&head), 503, "retired binding is unbound: {head}");

    // A status change delivered after a replacement bind leaves the
    // replacement bound, and it keeps admitting with no publication or bind.
    let replacement = h.bind().await;
    let mut checked = h.native.hold_next(Site::BackendChecked);
    status_tx
        .send(BackendStatus::Starting { pid: 3 })
        .expect("watcher listens");
    within("delayed status change applied", checked.arrived()).await;
    checked.release();
    let (mut public, _leg) = h.admit(BROWSER, &[]).await;
    wait_connections(&h.broker, 1).await;

    // Stale bind after replacement: a bind proven with B is held after proof;
    // the token rotates to C, the status change retires B's generation, and a
    // bind proven with C commits. The held bind is then refused.
    let mut held = h.native.hold_next(Site::Bind);
    let stale_bind = tokio::spawn(bind_as(
        h.native.clone(),
        TOKEN_B.to_owned(),
        default_settings(),
    ));
    within("bind reaches its barrier", held.arrived()).await;
    h.rotate(TOKEN_C);
    let mut checked = h.native.hold_next(Site::BackendChecked);
    status_tx
        .send(BackendStatus::Starting { pid: 4 })
        .expect("watcher listens");
    within("rotation applied", checked.arrived()).await;
    checked.release();
    assert_eq!(public.close_code("replacement retired").await, 1012);
    wait_connections(&h.broker, 0).await;
    let current = h.bind().await;
    assert_ne!(current, replacement);
    let (mut public, mut leg) = h.admit(BROWSER, &[]).await;
    wait_connections(&h.broker, 1).await;
    held.release();
    let (status, _) = within("stale bind", stale_bind).await.expect("bind task");
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "bind proven with a retired token"
    );
    let (_second_public, second_leg) = h.admit(BROWSER, &[]).await;
    assert_eq!(second_leg.single("x-gobby-ws-generation"), current);
    wait_connections(&h.broker, 2).await;
    h.publish(json!([publish_op(&task)])).await;
    assert_eq!(
        frames_before_marker(&mut public, &mut leg, "current binding intact").await,
        texts(&[&task])
    );

    // A publish held the same way is refused and enqueues nothing.
    let mut held = h.native.hold_next(Site::Publish);
    let stale_publish = tokio::spawn(publish_as(
        h.native.clone(),
        TOKEN_C.to_owned(),
        current,
        json!([publish_op(&task)]),
    ));
    within("publish reaches its barrier", held.arrived()).await;
    h.rotate(TOKEN_D);
    held.release();
    let status = within("held publish", stale_publish)
        .await
        .expect("publish task");
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "publish proven with a retired token"
    );
    assert_eq!(
        frames_before_marker(&mut public, &mut leg, "after the held publish").await,
        Vec::<Message>::new()
    );
}

/// Serves `FrontDoor::handle` on two in-process listeners that share one
/// `NativeWs`: the daemon-port browser listener and the WebSocket-port direct
/// listener. Both target the same backend HTTP address.
async fn serve_front_doors(native: NativeWs, backend: SocketAddr) -> [SocketAddr; 2] {
    let mut addresses = [SocketAddr::from(([127, 0, 0, 1], 0)); 2];
    for (address, ws_listener) in addresses.iter_mut().zip([false, true]) {
        let auth = Arc::new(
            AuthState::new("boot-secret".into(), None, Default::default()).expect("auth state"),
        );
        let mut state = FrontDoorState::new(backend, BackendState::Down, auth);
        state.native_ws = native.clone();
        state.ws_listener = ws_listener;
        let table = RouteTable::new(FAMILIES, &BTreeMap::new(), &state);
        let door = Arc::new(FrontDoor::new(state, table));
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("front door listener");
        *address = listener.local_addr().expect("front door address");
        tokio::spawn(async move {
            while let Ok((stream, peer)) = listener.accept().await {
                let door = door.clone();
                tokio::spawn(async move {
                    let service = service_fn(move |request: Request<hyper::body::Incoming>| {
                        let door = door.clone();
                        async move {
                            let request = request.map(Body::new);
                            Ok::<_, std::convert::Infallible>(
                                door.handle(request, peer, false).await,
                            )
                        }
                    });
                    let _ = http1::Builder::new()
                        .serve_connection(TokioIo::new(stream), service)
                        .with_upgrades()
                        .await;
                });
            }
        });
    }
    addresses
}

/// Admits one upgrade on a front-door listener and returns its public socket
/// and the private leg the stub bridge received for it.
async fn admit_at(h: &mut Harness, address: SocketAddr, path: &str) -> (Public, Leg) {
    let connecting = tokio::spawn(handshake(address, upgrade_request(path, &[])));
    let mut leg = h.next_leg().await;
    leg.send(Message::text(ready_frame(&h.generation))).await;
    let (stream, head, rest) = within("public response", connecting)
        .await
        .expect("handshake task");
    assert_eq!(status(&head), 101, "{head}");
    (Public::upgraded(stream, head, rest).await, leg)
}

/// A plain (non-upgrade) request on a front-door listener, answered in full.
async fn plain_request(address: SocketAddr, path: &str) -> String {
    let mut stream = TcpStream::connect(address).await.expect("connect");
    let request = format!("GET {path} HTTP/1.1\r\nHost: gobby.test\r\nConnection: close\r\n\r\n");
    stream
        .write_all(request.as_bytes())
        .await
        .expect("write request");
    let mut response = Vec::new();
    within(
        "plain response",
        AsyncReadExt::read_to_end(&mut stream, &mut response),
    )
    .await
    .expect("read response");
    String::from_utf8_lossy(&response).into_owned()
}

#[tokio::test]
async fn application_paths_are_native_and_hmr_is_proxy() {
    let (hmr_backend, hmr) = common::ws_backend(0, Vec::new()).await;
    let mut h = Harness::start().await;
    h.bind().await;
    let [browser, direct] = serve_front_doors(h.native.clone(), hmr_backend).await;

    // The browser entry: /ws and /ws/x are native, each leg marked browser.
    for path in ["/ws", "/ws/x"] {
        let (mut public, mut leg) = admit_at(&mut h, browser, path).await;
        assert_eq!(leg.single("x-gobby-ws-entry"), "browser", "{path}");
        leg.send(Message::text("browser-probe")).await;
        assert_eq!(public.text("browser probe").await, "browser-probe", "{path}");
    }

    // The direct entry: every path on the WebSocket listener is native.
    for path in ["/direct", "/ws/x"] {
        let (mut public, mut leg) = admit_at(&mut h, direct, path).await;
        assert_eq!(leg.single("x-gobby-ws-entry"), "direct", "{path}");
        leg.send(Message::text("direct-probe")).await;
        assert_eq!(public.text("direct probe").await, "direct-probe", "{path}");
    }

    // Vite HMR keeps the splice to the backend HTTP target and never reaches
    // the native bridge.
    let (_, head, _) = within(
        "hmr handshake",
        handshake(browser, upgrade_request("/__vite_hmr", &[])),
    )
    .await;
    assert_eq!(status(&head), 101, "{head}");
    let capture = within("hmr backend capture", hmr)
        .await
        .expect("hmr backend capture");
    assert!(
        capture.request_head.starts_with("GET /__vite_hmr "),
        "{}",
        capture.request_head
    );
    assert!(
        h.legs.try_recv().is_err(),
        "the HMR upgrade reached the native bridge"
    );

    // Other /_gobby/ paths answer 404 on both listeners, and a plain request
    // on the direct listener answers 426 with the upgrade it requires.
    for address in [browser, direct] {
        let response = plain_request(address, "/_gobby/other").await;
        assert!(response.starts_with("HTTP/1.1 404"), "{response}");
    }
    let response = plain_request(direct, "/direct").await;
    assert!(response.starts_with("HTTP/1.1 426"), "{response}");
    assert!(
        response
            .to_ascii_lowercase()
            .contains("upgrade: websocket"),
        "{response}"
    );
}

/// A browser client that attaches to one session receives that session's
/// `session_message` and `hook_event` frames and nothing from another session.
/// The attach reaches the private leg, and the application handler answers it
/// by subscribing the leg, which the publish route carries.
#[tokio::test]
async fn browser_attach_receives_only_its_session_frames() {
    let mut h = Harness::start().await;
    h.bind().await;
    let (mut attached, mut attached_leg) = h.admit(BROWSER, &[]).await;
    let (mut other, mut other_leg) = h.admit(BROWSER, &[]).await;
    wait_connections(&h.broker, 2).await;

    let attach = r#"{"type": "attach_to_session", "session_id": "s-1"}"#;
    attached.send(Message::text(attach)).await;
    assert_eq!(
        attached_leg.next("attach relayed to the leg").await,
        Message::text(attach)
    );
    h.publish(json!([subscribe_op(
        attached_leg.id(),
        &["session_message:session_id=s-1", "hook_event:session_id=s-1"],
    )]))
    .await;

    let own_message = json!({"type": "session_message", "session_id": "s-1", "content": "mine"});
    let own_hook = json!({"type": "hook_event", "session_id": "s-1", "event_type": "PreToolUse"});
    let other_message =
        json!({"type": "session_message", "session_id": "s-2", "content": "theirs"});
    let other_hook = json!({"type": "hook_event", "session_id": "s-2", "event_type": "PreToolUse"});
    h.publish(json!([
        publish_op(&other_message),
        publish_op(&own_message),
        publish_op(&other_hook),
        publish_op(&own_hook),
    ]))
    .await;

    assert_eq!(
        frames_before_marker(&mut attached, &mut attached_leg, "attached after publish").await,
        texts(&[&own_message, &own_hook])
    );
    assert_eq!(
        frames_before_marker(&mut other, &mut other_leg, "unattached after publish").await,
        Vec::<Message>::new()
    );
}
