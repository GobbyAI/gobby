//! Public WebSocket connections relayed over private legs to the Python bridge.
//!
//! A public upgrade is admitted only when the bridge's first private frame is
//! `bridge_ready` for the current generation. An admitted connection registers
//! with the broker under the binding lock, and its pump relays frames both ways
//! until either side ends.

use std::collections::HashMap;
use std::net::{IpAddr, SocketAddr};
use std::num::{NonZeroU64, NonZeroUsize};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use axum::body::Body;
use axum::http::header::{CONNECTION, SEC_WEBSOCKET_KEY, SEC_WEBSOCKET_VERSION, UPGRADE};
use axum::http::{HeaderMap, HeaderValue, Request, Response, StatusCode};
use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use futures_util::stream::{SplitSink, SplitStream};
use futures_util::{SinkExt, StreamExt};
use gobby_terminals::write_api::TokenSlot;
use hyper::upgrade::{OnUpgrade, Upgraded};
use hyper_util::rt::TokioIo;
use serde::Deserialize;
use serde_json::Value;
use tokio::net::TcpStream;
use tokio::sync::{oneshot, watch};
use tokio::time::{Instant, interval_at, sleep_until, timeout};
use tokio_tungstenite::WebSocketStream;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::handshake::server::create_response_with_body;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::{CloseFrame, Role, WebSocketConfig};
use tokio_tungstenite::tungstenite::{Error as WsError, Message};

use super::broker::{Batch, BroadcastBroker, ConnectionId, MAX_QUEUED_BYTES, Registration};
use super::subscriptions::EntryMode;
use crate::front_door::health::{BackendState, unavailable};
use crate::lifecycle::backend::BackendStatus;

/// Connect, handshake and the first private frame share this budget.
const READY_TIMEOUT: Duration = Duration::from_secs(2);
const PRIVATE_SEND_TIMEOUT: Duration = Duration::from_secs(2);
const CLOSE_TIMEOUT: Duration = Duration::from_secs(1);
const BRIDGE_PATH: &str = "/_gobby/ws/bridge";

const RESTART: &str = "Service restart";
const SHUTDOWN: &str = "Server shutting down";
const UNAVAILABLE: &str = "WebSocket server unavailable";
const BRIDGE_GONE: &str = "Bridge unavailable";
const TOO_BIG: &str = "Message too big";
const KEEPALIVE: &str = "keepalive ping timeout";

/// Public request headers copied to the bridge, first value only.
const FORWARDED: [&str; 8] = [
    "authorization",
    "cookie",
    "x-gobby-break-glass",
    "origin",
    "user-agent",
    "x-gobby-user-id",
    "x-gobby-machine-id",
    "x-gobby-key-id",
];

type Public = WebSocketStream<TokioIo<Upgraded>>;
type Bridge = WebSocketStream<TcpStream>;

/// Where a test can park one request before it commits.
#[doc(hidden)]
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Site {
    /// After a public upgrade is ready, before registration takes the lock.
    Register,
    /// After a bind is proven and validated, before it takes the lock.
    Bind,
    /// After a publish is proven and validated, before it takes the lock.
    Publish,
    /// After the backend watcher applied one status change.
    BackendChecked,
}

/// A parked request: `arrived` resolves once it reaches its site, and
/// `release` lets it continue.
#[doc(hidden)]
pub struct Held {
    arrived: oneshot::Receiver<()>,
    release: oneshot::Sender<()>,
}

impl Held {
    pub async fn arrived(&mut self) {
        let _ = (&mut self.arrived).await;
    }

    pub fn release(self) {
        let _ = self.release.send(());
    }
}

struct Gate {
    arrived: oneshot::Sender<()>,
    release: oneshot::Receiver<()>,
}

/// The binding's bounds, as Python's bind request sends them.
#[derive(Debug, Clone, Copy, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Settings {
    max_message_size: NonZeroUsize,
    ping_interval: NonZeroU64,
    ping_timeout: NonZeroU64,
}

/// The bridge's first private frame. Only `bridge_ready` at version 1 is read.
#[derive(Deserialize)]
struct Ready {
    #[serde(rename = "type")]
    kind: String,
    version: u64,
    generation: String,
    admitted: bool,
    status: Option<u16>,
    body: Option<String>,
    close_code: Option<u16>,
    reason: Option<String>,
}

/// One admitted public connection, held by its binding until it ends.
struct Leg {
    registration: Registration,
    stop: oneshot::Sender<CloseFrame>,
}

/// The supervisor token that proved a bind, with its generation and bounds.
struct Binding {
    generation: String,
    settings: Settings,
    token: String,
    legs: HashMap<ConnectionId, Leg>,
}

#[derive(Default)]
struct State {
    binding: Option<Binding>,
    shutting_down: bool,
}

/// The binding a public upgrade was admitted under.
#[derive(Clone)]
struct Captured {
    generation: String,
    settings: Settings,
}

/// Why a private bind or publish was refused. Nothing was applied.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Refusal {
    Proof,
    Stale,
    Unavailable,
}

impl Refusal {
    pub(super) fn status(self) -> StatusCode {
        match self {
            Self::Proof => StatusCode::FORBIDDEN,
            Self::Stale => StatusCode::CONFLICT,
            Self::Unavailable => StatusCode::SERVICE_UNAVAILABLE,
        }
    }
}

/// One validated publish or subscribe operation, ready to apply.
pub(super) enum Op {
    Publish {
        filter: Value,
        batch: Batch,
    },
    Subscribe {
        leg: ConnectionId,
        events: Vec<String>,
    },
}

struct Inner {
    broker: BroadcastBroker,
    secret: String,
    token: Option<TokenSlot>,
    backend: SocketAddr,
    gates: Mutex<HashMap<Site, Gate>>,
    state: Mutex<State>,
    next_leg: AtomicU64,
}

/// The native transport shared by both public entries and the private
/// endpoints.
#[derive(Clone)]
pub struct NativeWs {
    inner: Arc<Inner>,
}

impl NativeWs {
    pub fn new(
        broker: BroadcastBroker,
        secret: String,
        token: Option<TokenSlot>,
        backend: SocketAddr,
    ) -> Self {
        Self {
            inner: Arc::new(Inner {
                broker,
                secret,
                token,
                backend,
                gates: Mutex::default(),
                state: Mutex::default(),
                next_leg: AtomicU64::new(1),
            }),
        }
    }

    /// Admits one public upgrade on `entry`.
    pub async fn accept(
        &self,
        request: Request<Body>,
        entry: EntryMode,
        peer: SocketAddr,
    ) -> Response<Body> {
        let mut request = request;
        let response = match handshake(&request) {
            Ok(response) => response,
            Err(refusal) => return *refusal,
        };
        let upgrade = hyper::upgrade::on(&mut request);
        let Some(captured) = self.captured() else {
            return self.unavailable_for(entry, response, upgrade);
        };
        let id = ConnectionId::new(self.inner.next_leg.fetch_add(1, Ordering::Relaxed));
        // Built before the first await: the public request body is not Sync, so
        // no borrow of it may live across a suspension point.
        let Some(leg_request) = self.leg_request(&request, entry, peer, &captured, id) else {
            return self.unavailable_for(entry, response, upgrade);
        };
        let opened = timeout(READY_TIMEOUT, self.open_leg(leg_request, &captured)).await;
        let Ok(Some((leg, ready))) = opened else {
            return self.unavailable_for(entry, response, upgrade);
        };
        if ready.admitted {
            tokio::spawn(self.clone().attach(upgrade, leg, entry, id, captured));
            return response;
        }
        match entry {
            EntryMode::Direct => {
                match ready
                    .status
                    .and_then(|status| StatusCode::from_u16(status).ok())
                {
                    Some(status) if status.is_client_error() || status.is_server_error() => {
                        let mut refusal = Response::new(Body::from(ready.body.unwrap_or_default()));
                        *refusal.status_mut() = status;
                        refusal
                    }
                    _ => self.unavailable_for(entry, response, upgrade),
                }
            }
            EntryMode::Browser => match (ready.close_code, ready.reason) {
                (Some(code), Some(reason)) => {
                    close_after_upgrade(
                        upgrade,
                        CloseFrame {
                            code: CloseCode::from(code),
                            reason: reason.into(),
                        },
                    );
                    response
                }
                _ => self.unavailable_for(entry, response, upgrade),
            },
        }
    }

    /// Retires the binding whose proving token stops authorizing. Each status
    /// change is re-evaluated under the binding lock, so a delayed change never
    /// retires a replacement that proved its own token.
    pub async fn retire_on_backend_change(&self, mut status: watch::Receiver<BackendStatus>) {
        while status.changed().await.is_ok() {
            {
                let mut state = self.lock_state();
                if let Some(binding) = state
                    .binding
                    .take_if(|binding| !self.authorizes(&binding.token))
                {
                    self.retire(binding.legs, CloseCode::Restart, RESTART);
                }
            }
            self.pass(Site::BackendChecked).await;
        }
    }

    /// Stops admission and closes every public connection with 1001.
    pub async fn shutdown(&self) {
        let mut state = self.lock_state();
        state.shutting_down = true;
        if let Some(binding) = state.binding.take() {
            self.retire(binding.legs, CloseCode::Away, SHUTDOWN);
        }
    }

    /// Parks the next request that reaches `site` until the returned hold is
    /// released.
    #[doc(hidden)]
    pub fn hold_next(&self, site: Site) -> Held {
        let (arrived_tx, arrived) = oneshot::channel();
        let (release, release_rx) = oneshot::channel();
        self.inner
            .gates
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .insert(
                site,
                Gate {
                    arrived: arrived_tx,
                    release: release_rx,
                },
            );
        Held { arrived, release }
    }

    /// Proof for a private request: a loopback peer, the pair secret, and a
    /// token that passes now. Returns the proven token.
    pub(super) fn prove(&self, headers: &HeaderMap, peer: SocketAddr) -> Option<String> {
        if !is_loopback(peer.ip()) {
            return None;
        }
        let secret = headers.get("x-gobby-front-door")?.as_bytes();
        if !same_bytes(secret, self.inner.secret.as_bytes()) {
            return None;
        }
        let token = headers.get("x-gobby-backend-token")?.to_str().ok()?;
        self.authorizes(token).then(|| token.to_owned())
    }

    /// Installs a binding for `token`, retiring the previous generation's
    /// connections with 1012. Nothing changes unless the token still passes.
    pub(super) fn commit_bind(
        &self,
        token: &str,
        settings: Settings,
        generation: String,
    ) -> Result<(), Refusal> {
        let mut state = self.lock_state();
        if state.shutting_down {
            return Err(Refusal::Unavailable);
        }
        if !self.authorizes(token) {
            return Err(Refusal::Proof);
        }
        let previous = state.binding.replace(Binding {
            generation,
            settings,
            token: token.to_owned(),
            legs: HashMap::new(),
        });
        if let Some(previous) = previous {
            self.retire(previous.legs, CloseCode::Restart, RESTART);
        }
        Ok(())
    }

    /// Applies one publish request's operations in order. Nothing applies
    /// unless the token passes and the generation is current.
    pub(super) fn commit_publish(
        &self,
        token: &str,
        generation: &str,
        ops: Vec<Op>,
    ) -> Result<(), Refusal> {
        let state = self.lock_state();
        if !self.authorizes(token) {
            return Err(Refusal::Proof);
        }
        if state
            .binding
            .as_ref()
            .map(|binding| binding.generation.as_str())
            != Some(generation)
        {
            return Err(Refusal::Stale);
        }
        for op in ops {
            match op {
                Op::Publish { filter, batch } => self.inner.broker.publish(&filter, &batch),
                Op::Subscribe { leg, events } => self.inner.broker.add_subscriptions(leg, events),
            }
        }
        Ok(())
    }

    /// The public frame bound of `generation`, when it is still current.
    pub(super) fn frame_limit(&self, generation: &str) -> Option<usize> {
        let state = self.lock_state();
        state
            .binding
            .as_ref()
            .filter(|binding| binding.generation == generation)
            .map(|binding| binding.settings.max_message_size.get())
    }

    /// Parks this request at `site` when a test holds it, then lets it
    /// continue. Never called under the binding lock.
    pub(super) async fn pass(&self, site: Site) {
        let gate = self
            .inner
            .gates
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .remove(&site);
        if let Some(gate) = gate {
            let _ = gate.arrived.send(());
            let _ = gate.release.await;
        }
    }

    fn lock_state(&self) -> MutexGuard<'_, State> {
        self.inner
            .state
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
    }

    /// Whether `token` passes the supervisor's current-incarnation check.
    fn authorizes(&self, token: &str) -> bool {
        self.inner
            .token
            .as_ref()
            .is_some_and(|slot| slot.authorize(Some(token)).is_ok())
    }

    fn captured(&self) -> Option<Captured> {
        let state = self.lock_state();
        state.binding.as_ref().map(|binding| Captured {
            generation: binding.generation.clone(),
            settings: binding.settings,
        })
    }

    fn unavailable_for(
        &self,
        entry: EntryMode,
        response: Response<Body>,
        upgrade: OnUpgrade,
    ) -> Response<Body> {
        match entry {
            EntryMode::Direct => unavailable(self.inner.backend, BackendState::Down),
            EntryMode::Browser => {
                close_after_upgrade(upgrade, close_frame(CloseCode::Again, UNAVAILABLE));
                response
            }
        }
    }

    /// Closes each connection in `legs` with `code`: the public side retires
    /// at once, and the private side is told to close.
    fn retire(&self, legs: HashMap<ConnectionId, Leg>, code: CloseCode, reason: &'static str) {
        for leg in legs.into_values() {
            self.inner
                .broker
                .retire_with(&leg.registration, code, reason);
            let _ = leg.stop.send(close_frame(code, reason));
        }
    }

    /// The private request for one leg: the bridge path, the synthesized
    /// headers, and the allowlisted public headers. Synchronous, so no borrow
    /// of the public request lives across an await.
    fn leg_request(
        &self,
        request: &Request<Body>,
        entry: EntryMode,
        peer: SocketAddr,
        captured: &Captured,
        id: ConnectionId,
    ) -> Option<Request<()>> {
        let mut leg_request = format!("ws://{}{}", self.inner.backend, BRIDGE_PATH)
            .into_client_request()
            .ok()?;
        let leg_headers = leg_request.headers_mut();
        leg_headers.insert(
            "x-gobby-front-door",
            HeaderValue::from_str(&self.inner.secret).ok()?,
        );
        leg_headers.insert(
            "x-gobby-ws-generation",
            HeaderValue::from_str(&captured.generation).ok()?,
        );
        leg_headers.insert(
            "x-gobby-ws-entry",
            HeaderValue::from_static(match entry {
                EntryMode::Direct => "direct",
                EntryMode::Browser => "browser",
            }),
        );
        let path = request
            .uri()
            .path_and_query()
            .map_or("/", |path| path.as_str());
        leg_headers.insert("x-gobby-ws-path", HeaderValue::from_str(path).ok()?);
        leg_headers.insert(
            "x-gobby-ws-leg",
            HeaderValue::from_str(&id.get().to_string()).ok()?,
        );
        leg_headers.insert(
            "x-forwarded-for",
            HeaderValue::from_str(&canonical(peer.ip()).to_string()).ok()?,
        );
        let proto = if request
            .headers()
            .get("x-forwarded-proto")
            .is_some_and(|value| value.as_bytes() == b"https")
        {
            "https"
        } else {
            "http"
        };
        leg_headers.insert("x-forwarded-proto", HeaderValue::from_static(proto));
        for name in FORWARDED {
            if let Some(value) = request.headers().get(name) {
                leg_headers.insert(name, value.clone());
            }
        }

        Some(leg_request)
    }

    /// Opens the private leg and reads its first frame. `None` fails closed:
    /// a missing or late first frame, or one that is not `bridge_ready` for
    /// version 1 and the captured generation.
    async fn open_leg(
        &self,
        leg_request: Request<()>,
        captured: &Captured,
    ) -> Option<(Bridge, Ready)> {
        let stream = TcpStream::connect(self.inner.backend).await.ok()?;
        let (mut leg, _) =
            tokio_tungstenite::client_async_with_config(leg_request, stream, Some(leg_config()))
                .await
                .ok()?;
        let Some(Ok(Message::Text(text))) = leg.next().await else {
            return None;
        };
        let ready: Ready = serde_json::from_str(text.as_str()).ok()?;
        let valid = ready.kind == "bridge_ready"
            && ready.version == 1
            && ready.generation == captured.generation;
        valid.then_some((leg, ready))
    }

    /// Completes the public upgrade, registers the connection if its generation
    /// is still current, and relays frames until one side ends.
    async fn attach(
        self,
        upgrade: OnUpgrade,
        leg: Bridge,
        entry: EntryMode,
        id: ConnectionId,
        captured: Captured,
    ) {
        let Ok(upgraded) = upgrade.await else {
            return;
        };
        let public = WebSocketStream::from_raw_socket(
            TokioIo::new(upgraded),
            Role::Server,
            Some(public_config(captured.settings)),
        )
        .await;
        self.pass(Site::Register).await;
        let (sink, stream): (SplitSink<Public, Message>, SplitStream<Public>) = public.split();
        let (done_tx, done) = oneshot::channel();
        let (stop_tx, stop) = oneshot::channel();
        let registration = {
            let mut state = self.lock_state();
            match state
                .binding
                .as_mut()
                .filter(|binding| binding.generation == captured.generation)
            {
                Some(binding) => {
                    let Ok(registration) = self.inner.broker.register(id, entry, sink, move || {
                        let _ = done_tx.send(());
                    }) else {
                        return;
                    };
                    binding.legs.insert(
                        id,
                        Leg {
                            registration: registration.clone(),
                            stop: stop_tx,
                        },
                    );
                    registration
                }
                None => {
                    tokio::spawn(close_unregistered(sink, leg));
                    return;
                }
            }
        };
        self.pump(registration, stream, leg, captured.settings, stop, done)
            .await;
        self.release(&captured.generation, id);
    }

    /// Relays frames between the public and private sides. Every exit path
    /// either closes or retires the registration itself. None of them calls
    /// `disconnect` after an ordered close, which would drop the queued close
    /// frame ahead of the broker's retirement.
    async fn pump(
        &self,
        registration: Registration,
        mut public: SplitStream<Public>,
        leg: Bridge,
        settings: Settings,
        mut stop: oneshot::Receiver<CloseFrame>,
        mut done: oneshot::Receiver<()>,
    ) {
        let broker = &self.inner.broker;
        let (mut leg_tx, mut leg_rx): (SplitSink<Bridge, Message>, SplitStream<Bridge>) =
            leg.split();
        let period = Duration::from_secs(settings.ping_interval.get());
        let pong_wait = Duration::from_secs(settings.ping_timeout.get());
        let mut keepalive = interval_at(Instant::now() + period, period);
        let mut deadline: Option<Instant> = None;
        loop {
            // A copy of the deadline: the future never borrows the loop's state.
            let due = deadline;
            let pong_due = async move {
                match due {
                    Some(at) => sleep_until(at).await,
                    None => std::future::pending::<()>().await,
                }
            };
            tokio::select! {
                message = public.next() => match message {
                    Some(Ok(Message::Text(text))) => {
                        if let Some(app) = broker.receive(&registration, text.as_str())
                            && !send_leg(&mut leg_tx, Message::text(app.to_owned())).await
                        {
                            let frame = close_frame(CloseCode::Error, BRIDGE_GONE);
                            broker.close(&registration, frame);
                            break;
                        }
                    }
                    Some(Ok(Message::Binary(bytes))) => {
                        if !send_leg(&mut leg_tx, Message::Binary(bytes)).await {
                            broker.close(&registration, close_frame(CloseCode::Error, BRIDGE_GONE));
                            break;
                        }
                    }
                    Some(Ok(Message::Pong(_))) => deadline = None,
                    Some(Ok(Message::Close(frame))) => {
                        broker.disconnect(&registration);
                        let frame = frame.unwrap_or_else(|| close_frame(CloseCode::Error, ""));
                        close_leg(&mut leg_tx, frame).await;
                        break;
                    }
                    Some(Err(WsError::Capacity(_))) => {
                        broker.retire_with(&registration, CloseCode::Size, TOO_BIG);
                        close_leg(&mut leg_tx, close_frame(CloseCode::Size, TOO_BIG)).await;
                        break;
                    }
                    Some(Ok(_)) => {}
                    Some(Err(_)) | None => {
                        broker.disconnect(&registration);
                        close_leg(&mut leg_tx, close_frame(CloseCode::Error, "")).await;
                        break;
                    }
                },
                message = leg_rx.next() => match message {
                    Some(Ok(Message::Text(text))) => match Batch::text(text) {
                        Ok(batch) => broker.reply(&registration, batch),
                        Err(_) => {
                            broker.retire_with(&registration, CloseCode::Size, TOO_BIG);
                            close_leg(&mut leg_tx, close_frame(CloseCode::Size, TOO_BIG)).await;
                            break;
                        }
                    },
                    Some(Ok(Message::Binary(bytes))) => match Batch::binary(bytes) {
                        Ok(batch) => broker.reply(&registration, batch),
                        Err(_) => {
                            broker.retire_with(&registration, CloseCode::Size, TOO_BIG);
                            close_leg(&mut leg_tx, close_frame(CloseCode::Size, TOO_BIG)).await;
                            break;
                        }
                    },
                    Some(Ok(Message::Close(frame))) => {
                        let frame = frame.unwrap_or_else(|| close_frame(CloseCode::Error, ""));
                        broker.close(&registration, frame);
                        break;
                    }
                    Some(Ok(_)) => {}
                    Some(Err(_)) | None => {
                        broker.close(&registration, close_frame(CloseCode::Error, BRIDGE_GONE));
                        break;
                    }
                },
                frame = &mut stop => {
                    let frame = frame.unwrap_or_else(|_| close_frame(CloseCode::Error, ""));
                    close_leg(&mut leg_tx, frame).await;
                    break;
                }
                _ = &mut done => {
                    close_leg(&mut leg_tx, close_frame(CloseCode::Error, "")).await;
                    break;
                }
                _ = keepalive.tick() => {
                    if deadline.is_none() {
                        broker.ping(&registration);
                        deadline = Some(Instant::now() + pong_wait);
                    }
                }
                _ = pong_due, if deadline.is_some() => {
                    broker.retire_with(&registration, CloseCode::Error, KEEPALIVE);
                    close_leg(&mut leg_tx, close_frame(CloseCode::Error, KEEPALIVE)).await;
                    break;
                }
            }
        }
    }

    /// Drops the leg from the binding, unless the binding was replaced first.
    fn release(&self, generation: &str, id: ConnectionId) {
        let mut state = self.lock_state();
        if let Some(binding) = state
            .binding
            .as_mut()
            .filter(|binding| binding.generation == generation)
        {
            binding.legs.remove(&id);
        }
    }
}

/// Validates the upgrade request as tungstenite does, plus the checks it
/// leaves out: no duplicated handshake header, version 13 (426 otherwise), and
/// a key that decodes to 16 bytes. Every other failure is 400.
fn handshake(request: &Request<Body>) -> Result<Response<Body>, Box<Response<Body>>> {
    let headers = request.headers();
    let duplicated = [
        UPGRADE,
        CONNECTION,
        SEC_WEBSOCKET_KEY,
        SEC_WEBSOCKET_VERSION,
    ]
    .iter()
    .any(|name| headers.get_all(name).iter().count() > 1);
    if duplicated {
        return Err(Box::new(status_only(StatusCode::BAD_REQUEST)));
    }
    let version = headers
        .get(SEC_WEBSOCKET_VERSION)
        .and_then(|value| value.to_str().ok());
    if version != Some("13") {
        let mut response = status_only(StatusCode::UPGRADE_REQUIRED);
        response
            .headers_mut()
            .insert(SEC_WEBSOCKET_VERSION, HeaderValue::from_static("13"));
        return Err(Box::new(response));
    }
    let key = headers
        .get(SEC_WEBSOCKET_KEY)
        .and_then(|value| STANDARD.decode(value.as_bytes()).ok());
    if key.is_none_or(|key| key.len() != 16) {
        return Err(Box::new(status_only(StatusCode::BAD_REQUEST)));
    }
    create_response_with_body(request, Body::empty)
        .map_err(|_| Box::new(status_only(StatusCode::BAD_REQUEST)))
}

/// Answers a refused browser upgrade: the 101 is already on its way, so the
/// client gets the close frame right after it.
fn close_after_upgrade(upgrade: OnUpgrade, frame: CloseFrame) {
    tokio::spawn(async move {
        let Ok(upgraded) = upgrade.await else {
            return;
        };
        let mut socket =
            WebSocketStream::from_raw_socket(TokioIo::new(upgraded), Role::Server, None).await;
        let _ = timeout(CLOSE_TIMEOUT, socket.close(Some(frame))).await;
    });
}

/// Closes a connection that lost its generation before it registered. Both
/// sides close 1012, as if the connection had been retired.
async fn close_unregistered(mut sink: SplitSink<Public, Message>, mut leg: Bridge) {
    let frame = close_frame(CloseCode::Restart, RESTART);
    let _ = sink.send(Message::Close(Some(frame.clone()))).await;
    let _ = sink.close().await;
    let _ = timeout(CLOSE_TIMEOUT, leg.close(Some(frame))).await;
}

async fn send_leg(leg: &mut SplitSink<Bridge, Message>, message: Message) -> bool {
    matches!(
        timeout(PRIVATE_SEND_TIMEOUT, leg.send(message)).await,
        Ok(Ok(()))
    )
}

async fn close_leg(leg: &mut SplitSink<Bridge, Message>, frame: CloseFrame) {
    let _ = timeout(CLOSE_TIMEOUT, leg.send(Message::Close(Some(frame)))).await;
}

fn close_frame(code: CloseCode, reason: &'static str) -> CloseFrame {
    CloseFrame {
        code,
        reason: reason.into(),
    }
}

/// A response with no body, for refusals that carry only a status.
pub(super) fn status_only(status: StatusCode) -> Response<Body> {
    let mut response = Response::new(Body::empty());
    *response.status_mut() = status;
    response
}

fn public_config(settings: Settings) -> WebSocketConfig {
    let bound = settings.max_message_size.get();
    WebSocketConfig::default()
        .max_message_size(Some(bound))
        .max_frame_size(Some(bound))
}

fn leg_config() -> WebSocketConfig {
    WebSocketConfig::default()
        .max_message_size(Some(MAX_QUEUED_BYTES))
        .max_frame_size(Some(MAX_QUEUED_BYTES))
}

fn is_loopback(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => ip.is_loopback(),
        IpAddr::V6(ip) => {
            ip.is_loopback()
                || ip
                    .to_ipv4_mapped()
                    .is_some_and(|mapped| mapped.is_loopback())
        }
    }
}

/// The IPv4 form of an IPv4-mapped address; other addresses pass unchanged.
fn canonical(ip: IpAddr) -> IpAddr {
    match ip {
        IpAddr::V6(ip) => ip.to_ipv4_mapped().map_or(IpAddr::V6(ip), IpAddr::V4),
        IpAddr::V4(ip) => IpAddr::V4(ip),
    }
}

/// Equality that inspects every byte of equal-length inputs.
fn same_bytes(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}
