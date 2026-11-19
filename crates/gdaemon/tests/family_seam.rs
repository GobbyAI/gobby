//! Route-family seam, driven through the front door: claims, the registry and the
//! native surfaces.
//!
//! Tests named by a red expectation fail until the claim runner and the registry land.
//! The guard tests pin behaviour that must hold before and after those changes.

use super::common;

use std::collections::BTreeMap;
use std::convert::Infallible;
use std::net::{Ipv6Addr, SocketAddr};
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use axum::Router;
use axum::body::Body;
use axum::http::{Method, Request, Response, StatusCode};
use bytes::Bytes;
use gobby_core::bootstrap::RouteBackend;
use gobby_core::family::{
    ConfigEpoch, ContextParts, DegradedServices, Extensions, FamilyContext, IDENTITY_ENTRY_HEADERS,
    config_channel,
};
use gobby_core::route_family::{
    BoxFut, Credential, Gate, Kind, PathMatch, PeerRule, RouteClaim, RouteFamily,
};
use gobby_daemon::front_door::auth::{AuthState, KeyResolver};
use gobby_daemon::front_door::health::BackendState;
use gobby_daemon::front_door::registry::{FAMILIES, selectable, surfaces};
use gobby_daemon::front_door::routes::{RouteTable, unimplemented_families};
use gobby_daemon::front_door::{FrontDoor, FrontDoorState};
use gobby_daemon::websocket::broker::{BroadcastBroker, BroadcastBrokerPublisher, ConnectionId};
use gobby_daemon::websocket::subscriptions::EntryMode;
use http_body_util::{BodyExt, Full};
use hyper::body::Incoming;
use hyper::server::conn::http1 as server_http1;
use hyper::service::service_fn;
use hyper_util::rt::TokioIo;
use serde_json::json;
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::Message;

#[cfg(unix)]
use gobby_core::bootstrap::RunMode;
#[cfg(unix)]
use gobby_core::postgres_pool::{Pool, PoolSettings};
#[cfg(unix)]
use gobby_daemon::front_door::auth::{KeyIdentity, ResolveFuture};
#[cfg(unix)]
use gobby_daemon::lease::{ActiveDaemonLease, NamedLock};
#[cfg(unix)]
use gobby_daemon::lifecycle::backend::BackendStatus;
#[cfg(unix)]
use gobby_daemon::state::RuntimeState;
#[cfg(unix)]
use gobby_terminals::write_api::TokenSlot;

/// A family whose claim rows answer with a fixed marker and count their handler runs.
struct Fixture {
    claims: &'static [RouteClaim],
    status: StatusCode,
    marker: &'static str,
    backend: RouteBackend,
    runs: AtomicUsize,
}

impl Fixture {
    fn leak(
        claims: &'static [RouteClaim],
        status: StatusCode,
        marker: &'static str,
        backend: RouteBackend,
    ) -> &'static Fixture {
        Box::leak(Box::new(Fixture {
            claims,
            status,
            marker,
            backend,
            runs: AtomicUsize::new(0),
        }))
    }

    fn runs(&self) -> usize {
        self.runs.load(Ordering::SeqCst)
    }
}

impl RouteFamily<FrontDoorState> for Fixture {
    fn name(&self) -> &'static str {
        "fixture"
    }

    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }

    fn router(&self) -> Router<FrontDoorState> {
        Router::new()
    }

    fn default_backend(&self) -> RouteBackend {
        self.backend
    }

    fn route_claims(&self) -> &'static [RouteClaim] {
        self.claims
    }

    fn serve_claim<'a>(
        &'a self,
        _state: &'a FrontDoorState,
        _peer: SocketAddr,
        _request: Request<Body>,
    ) -> BoxFut<'a, Result<Request<Body>, Response<Body>>> {
        Box::pin(async move {
            self.runs.fetch_add(1, Ordering::SeqCst);
            Err(marker_response(self.status, self.marker))
        })
    }
}

fn marker_response(status: StatusCode, marker: &'static str) -> Response<Body> {
    Response::builder()
        .status(status)
        .header("x-fixture", marker)
        .body(Body::from(marker))
        .expect("fixture response")
}

static WS_CLAIM: &[RouteClaim] = &[RouteClaim {
    method: None,
    path: PathMatch::Exact("/fixture/ws"),
    gate: Gate::BeforeRefusal,
    credential: Credential::Unchecked,
    peer: PeerRule::Any,
    kind: Kind::UpgradeOnly,
}];

static PRE_AUTH_CLAIM: &[RouteClaim] = &[RouteClaim {
    method: None,
    path: PathMatch::Exact("/fixture/pre"),
    gate: Gate::BeforeRefusal,
    credential: Credential::Unchecked,
    peer: PeerRule::Any,
    kind: Kind::Any,
}];

static BYPASS_CLAIM: &[RouteClaim] = &[RouteClaim {
    method: None,
    path: PathMatch::Exact("/fixture/bypass"),
    gate: Gate::BeforeRefusal,
    credential: Credential::Unchecked,
    peer: PeerRule::Any,
    kind: Kind::Any,
}];

static AFTER_CLAIM: &[RouteClaim] = &[RouteClaim {
    method: None,
    path: PathMatch::Exact("/fixture/after"),
    gate: Gate::AfterRefusal,
    credential: Credential::AfterAuthenticate,
    peer: PeerRule::Any,
    kind: Kind::Any,
}];

static GET_CLAIM: &[RouteClaim] = &[RouteClaim {
    method: Some(Method::GET),
    path: PathMatch::Exact("/fixture/get"),
    gate: Gate::BeforeRefusal,
    credential: Credential::Unchecked,
    peer: PeerRule::Any,
    kind: Kind::Any,
}];

static LOOPBACK_CLAIM: &[RouteClaim] = &[RouteClaim {
    method: None,
    path: PathMatch::Exact("/fixture/loopback"),
    gate: Gate::BeforeRefusal,
    credential: Credential::Unchecked,
    peer: PeerRule::LoopbackOnly,
    kind: Kind::Any,
}];

/// A named family with a start rank whose claim rows answer with its name.
struct Ranked {
    name: &'static str,
    rank: u8,
    claims: &'static [RouteClaim],
    runs: AtomicUsize,
}

impl Ranked {
    fn leak(name: &'static str, rank: u8, claims: &'static [RouteClaim]) -> &'static Ranked {
        Box::leak(Box::new(Ranked {
            name,
            rank,
            claims,
            runs: AtomicUsize::new(0),
        }))
    }

    fn runs(&self) -> usize {
        self.runs.load(Ordering::SeqCst)
    }
}

impl RouteFamily<FrontDoorState> for Ranked {
    fn name(&self) -> &'static str {
        self.name
    }

    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }

    fn router(&self) -> Router<FrontDoorState> {
        Router::new()
    }

    fn default_backend(&self) -> RouteBackend {
        RouteBackend::Native
    }

    fn route_claims(&self) -> &'static [RouteClaim] {
        self.claims
    }

    fn start_rank(&self) -> u8 {
        self.rank
    }

    fn serve_claim<'a>(
        &'a self,
        _state: &'a FrontDoorState,
        _peer: SocketAddr,
        _request: Request<Body>,
    ) -> BoxFut<'a, Result<Request<Body>, Response<Body>>> {
        Box::pin(async move {
            self.runs.fetch_add(1, Ordering::SeqCst);
            Err(marker_response(StatusCode::OK, self.name))
        })
    }
}

static TIE_CLAIM: &[RouteClaim] = &[RouteClaim {
    method: None,
    path: PathMatch::Exact("/fixture/tie"),
    gate: Gate::BeforeRefusal,
    credential: Credential::Unchecked,
    peer: PeerRule::Any,
    kind: Kind::Any,
}];

static TIE_SEGMENT_CLAIM: &[RouteClaim] = &[RouteClaim {
    method: None,
    path: PathMatch::Segment("/fixture/tie"),
    gate: Gate::BeforeRefusal,
    credential: Credential::Unchecked,
    peer: PeerRule::Any,
    kind: Kind::Any,
}];

/// The same row the machines surface declares, so the two tie on specificity.
static MACHINES_TWIN_CLAIM: &[RouteClaim] = &[RouteClaim {
    method: None,
    path: PathMatch::Segment("/api/machines"),
    gate: Gate::AfterRefusal,
    credential: Credential::AfterAuthenticate,
    peer: PeerRule::Any,
    kind: Kind::Any,
}];

/// Accepts any key and names one fixed identity.
#[cfg(unix)]
struct FixedKey;

#[cfg(unix)]
impl KeyResolver for FixedKey {
    fn resolve<'a>(&'a self, _key_hash: &'a str) -> ResolveFuture<'a> {
        Box::pin(async {
            Ok(Some(KeyIdentity {
                user_id: "user-1".into(),
                machine_id: "machine-1".into(),
                key_id: "key-1".into(),
            }))
        })
    }
}

fn auth(resolver: Option<Arc<dyn KeyResolver>>) -> Arc<AuthState> {
    Arc::new(
        AuthState::new("boot-secret".into(), resolver, Default::default()).expect("auth state"),
    )
}

fn door(
    state: FrontDoorState,
    families: &[&'static dyn RouteFamily<FrontDoorState>],
    routes: &BTreeMap<String, RouteBackend>,
) -> FrontDoor {
    let table = RouteTable::new(families, routes, &state);
    FrontDoor::new(state, table)
}

/// Builds a front door with a runtime, as the native daemon does. Mirrors the
/// `write_door` helper in `tests/front_door.rs`, which is private to that module.
#[cfg(unix)]
fn write_door_harness(
    backend: SocketAddr,
    status: BackendStatus,
    families: &[&'static dyn RouteFamily<FrontDoorState>],
    resolver: Option<Arc<dyn KeyResolver>>,
) -> FrontDoor {
    let (_status, receiver) = tokio::sync::watch::channel(status);
    let mut state = FrontDoorState::new(backend, BackendState::Down, auth(resolver));
    state.runtime = Some(Arc::new(RuntimeState {
        mode: RunMode::Standalone,
        lease: None,
        lease_state: None,
        backend: receiver,
        backend_wait: tokio::sync::watch::channel(None).1,
        write_token: TokenSlot::default(),
        terminal_route: Default::default(),
    }));
    door(state, families, &BTreeMap::new())
}

fn loopback_peer() -> SocketAddr {
    "127.0.0.1:4000".parse().expect("loopback peer")
}

fn get(path: &str) -> Request<Body> {
    Request::get(path).body(Body::empty()).expect("get request")
}

fn upgrade_request(path: &str) -> Request<Body> {
    Request::get(path)
        .header("connection", "upgrade")
        .header("upgrade", "websocket")
        .body(Body::empty())
        .expect("upgrade request")
}

async fn send(door: &FrontDoor, request: Request<Body>, peer: SocketAddr) -> Response<Body> {
    door.handle(request, peer, false).await
}

async fn body_text(response: Response<Body>) -> String {
    let bytes = response
        .into_body()
        .collect()
        .await
        .expect("response body")
        .to_bytes();
    String::from_utf8_lossy(&bytes).into_owned()
}

/// A backend that answers every request with the body `backend`.
async fn stub_backend() -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("stub backend");
    let addr = listener.local_addr().expect("stub backend address");
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            tokio::spawn(async move {
                let service = service_fn(|_request: Request<Incoming>| async {
                    Ok::<_, Infallible>(Response::new(Full::new(Bytes::from_static(b"backend"))))
                });
                let _ = server_http1::Builder::new()
                    .serve_connection(TokioIo::new(stream), service)
                    .await;
            });
        }
    });
    addr
}

#[tokio::test]
async fn native_family_claims_ws_upgrade() {
    let fixture = Fixture::leak(
        WS_CLAIM,
        StatusCode::IM_A_TEAPOT,
        "ws",
        RouteBackend::Native,
    );
    let families: [&'static dyn RouteFamily<FrontDoorState>; 1] = [fixture];
    let state = FrontDoorState::new(common::refused_addr().await, BackendState::Down, auth(None));
    let door = door(state, &families, &BTreeMap::new());

    let response = send(&door, upgrade_request("/fixture/ws"), loopback_peer()).await;

    assert_eq!(response.status(), StatusCode::IM_A_TEAPOT);
    assert_eq!(
        response
            .headers()
            .get("x-fixture")
            .and_then(|value| value.to_str().ok()),
        Some("ws")
    );
    assert_eq!(fixture.runs(), 1);
}

#[tokio::test]
async fn proxy_family_ws_claim_is_not_consulted() {
    let fixture = Fixture::leak(WS_CLAIM, StatusCode::IM_A_TEAPOT, "ws", RouteBackend::Proxy);
    let families: [&'static dyn RouteFamily<FrontDoorState>; 1] = [fixture];
    let state = FrontDoorState::new(common::refused_addr().await, BackendState::Down, auth(None));
    let door = door(state, &families, &BTreeMap::new());

    let response = send(&door, upgrade_request("/fixture/ws"), loopback_peer()).await;

    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert!(response.headers().get("x-fixture").is_none());
    assert_eq!(fixture.runs(), 0);
}

#[tokio::test]
async fn pre_auth_claim_runs_before_the_credential_check() {
    let fixture = Fixture::leak(PRE_AUTH_CLAIM, StatusCode::OK, "pre", RouteBackend::Native);
    let families: [&'static dyn RouteFamily<FrontDoorState>; 1] = [fixture];
    let state = FrontDoorState::new(common::refused_addr().await, BackendState::Down, auth(None));
    let door = door(state, &families, &BTreeMap::new());
    let request = Request::get("/fixture/pre")
        .header("authorization", "Bearer not-a-key")
        .body(Body::empty())
        .expect("pre-auth request");

    let response = send(&door, request, loopback_peer()).await;

    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(body_text(response).await, "pre");
    assert_eq!(fixture.runs(), 1);
}

#[cfg(unix)]
#[tokio::test]
async fn bypass_claim_answers_while_the_gate_refuses() {
    let fixture = Fixture::leak(BYPASS_CLAIM, StatusCode::OK, "bypass", RouteBackend::Native);
    let families: [&'static dyn RouteFamily<FrontDoorState>; 1] = [fixture];
    let door = write_door_harness(
        common::refused_addr().await,
        BackendStatus::Refused {
            reason: "fixture refusal".into(),
        },
        &families,
        None,
    );

    let response = send(&door, get("/fixture/bypass"), loopback_peer()).await;

    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(body_text(response).await, "bypass");
    assert_eq!(fixture.runs(), 1);
}

#[tokio::test]
async fn after_claim_waits_for_authenticate() {
    let fixture = Fixture::leak(AFTER_CLAIM, StatusCode::OK, "after", RouteBackend::Native);
    let families: [&'static dyn RouteFamily<FrontDoorState>; 1] = [fixture];
    let state = FrontDoorState::new(common::refused_addr().await, BackendState::Down, auth(None));
    let door = door(state, &families, &BTreeMap::new());
    let request = Request::get("/fixture/after")
        .header("authorization", "Bearer not-a-key")
        .body(Body::empty())
        .expect("malformed bearer request");

    let response = send(&door, request, loopback_peer()).await;

    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    assert_eq!(fixture.runs(), 0);
}

#[tokio::test]
async fn claimed_row_leaves_other_methods_proxied() {
    let backend = stub_backend().await;
    let fixture = Fixture::leak(GET_CLAIM, StatusCode::OK, "get", RouteBackend::Native);
    let families: [&'static dyn RouteFamily<FrontDoorState>; 1] = [fixture];
    let state = FrontDoorState::new(backend, BackendState::Down, auth(None));
    let door = door(state, &families, &BTreeMap::new());

    let claimed = send(&door, get("/fixture/get"), loopback_peer()).await;
    assert_eq!(claimed.status(), StatusCode::OK);
    assert_eq!(body_text(claimed).await, "get");

    let proxied = send(
        &door,
        Request::post("/fixture/get")
            .body(Body::empty())
            .expect("post request"),
        loopback_peer(),
    )
    .await;
    assert_eq!(proxied.status(), StatusCode::OK);
    assert_eq!(body_text(proxied).await, "backend");
    assert_eq!(fixture.runs(), 1);
}

#[tokio::test]
async fn loopback_only_claim_refuses_remote_peer() {
    let fixture = Fixture::leak(
        LOOPBACK_CLAIM,
        StatusCode::OK,
        "loopback",
        RouteBackend::Native,
    );
    let families: [&'static dyn RouteFamily<FrontDoorState>; 1] = [fixture];
    let state = FrontDoorState::new(common::refused_addr().await, BackendState::Down, auth(None));
    let door = door(state, &families, &BTreeMap::new());

    let remote: SocketAddr = "192.0.2.1:4000".parse().expect("remote peer");
    let refused = send(&door, get("/fixture/loopback"), remote).await;
    assert_eq!(refused.status(), StatusCode::NOT_FOUND);
    assert_eq!(body_text(refused).await, r#"{"error":"not_found"}"#);
    assert_eq!(fixture.runs(), 0);

    let mapped = SocketAddr::new(
        Ipv6Addr::new(0, 0, 0, 0, 0, 0xffff, 0x7f00, 0x0001).into(),
        4000,
    );
    for peer in [loopback_peer(), mapped] {
        let response = send(&door, get("/fixture/loopback"), peer).await;
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(body_text(response).await, "loopback");
    }
    assert_eq!(fixture.runs(), 2);
}

#[tokio::test]
async fn equal_specificity_claims_go_to_the_lower_start_rank() {
    let state = FrontDoorState::new(common::refused_addr().await, BackendState::Down, auth(None));

    // Equal ranks keep list order.
    let first = Ranked::leak("first", 1, TIE_CLAIM);
    let second = Ranked::leak("second", 1, TIE_CLAIM);
    let families: [&'static dyn RouteFamily<FrontDoorState>; 2] = [first, second];
    let table = RouteTable::new(&families, &BTreeMap::new(), &state);
    assert_eq!(
        table.claim_owner(&Method::GET, "/fixture/tie", false),
        Some("first")
    );

    // A more specific row wins even when its family starts later.
    let segment = Ranked::leak("segment", 0, TIE_SEGMENT_CLAIM);
    let exact = Ranked::leak("exact", 3, TIE_CLAIM);
    let families: [&'static dyn RouteFamily<FrontDoorState>; 2] = [segment, exact];
    let table = RouteTable::new(&families, &BTreeMap::new(), &state);
    assert_eq!(
        table.claim_owner(&Method::GET, "/fixture/tie", false),
        Some("exact")
    );

    // At equal specificity the lower rank wins. The later-starting family is listed
    // first, so list order alone would pick it.
    let late = Ranked::leak("late", 2, TIE_CLAIM);
    let early = Ranked::leak("early", 1, TIE_CLAIM);
    let families: [&'static dyn RouteFamily<FrontDoorState>; 2] = [late, early];
    let table = RouteTable::new(&families, &BTreeMap::new(), &state);
    assert_eq!(
        table.claim_owner(&Method::GET, "/fixture/tie", false),
        Some("early")
    );

    let door = door(state, &families, &BTreeMap::new());
    let response = send(&door, get("/fixture/tie"), loopback_peer()).await;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(body_text(response).await, "early");
    assert_eq!((early.runs(), late.runs()), (1, 0));
}

#[tokio::test]
async fn a_fixed_surface_keeps_an_equal_specificity_claim_from_a_later_rank() {
    let twin = Ranked::leak("machines_twin", 1, MACHINES_TWIN_CLAIM);
    let families: [&'static dyn RouteFamily<FrontDoorState>; 1] = [twin];
    let state = FrontDoorState::new(common::refused_addr().await, BackendState::Down, auth(None));
    let table = RouteTable::new(&families, &BTreeMap::new(), &state);

    // Supplied families precede the fixed surfaces in the table, so list order alone would
    // hand the machines path to the twin. The machines surface starts at rank 0.
    assert_eq!(
        table.claim_owner(&Method::GET, "/api/machines", false),
        Some("machines")
    );
    assert_eq!(
        table.claim_owner(&Method::GET, "/api/machines/m-1", false),
        Some("machines")
    );
}

#[test]
fn registry_file_lists_one_family_per_line_alphabetically() {
    let home = Path::new("/nonexistent/gobby-home");
    let selected: Vec<&str> = selectable(None, home)
        .iter()
        .map(|family| family.name())
        .collect();
    assert_eq!(selected, ["health", "terminal_ws"]);

    let mut ranked: Vec<(u8, usize, &str)> = FAMILIES
        .iter()
        .enumerate()
        .map(|(index, family)| (family.start_rank(), index, family.name()))
        .collect();
    ranked.sort_by_key(|(rank, index, _)| (*rank, *index));
    let ranked: Vec<&str> = ranked.into_iter().map(|(_, _, name)| name).collect();
    assert_eq!(
        selected, ranked,
        "selection is start_rank, then registry order"
    );

    let surface_names: Vec<&str> = surfaces().iter().map(|family| family.name()).collect();
    let expected: Vec<&str> = if cfg!(unix) {
        vec![
            "lease_admin",
            "machines",
            "node_channel",
            "node_internal",
            "runtime_challenge",
            "terminal_write",
            "ws_bridge",
            "ws_native",
        ]
    } else {
        vec![
            "machines",
            "node_channel",
            "node_internal",
            "runtime_challenge",
            "ws_bridge",
            "ws_native",
        ]
    };
    assert_eq!(surface_names, expected);

    let source = include_str!("../src/front_door/registry.rs");
    let block = source
        .split("pub static FAMILIES")
        .nth(1)
        .and_then(|rest| rest.split("];").next())
        .unwrap_or_default();
    let entries: Vec<&str> = block
        .lines()
        .map(str::trim)
        .filter(|line| line.starts_with('&'))
        .collect();
    assert_eq!(entries.len(), 2, "one family per line in FAMILIES");
    let mut sorted = entries.clone();
    sorted.sort_unstable();
    assert_eq!(entries, sorted, "FAMILIES is alphabetical");
}

#[test]
fn families_list_holds_no_surface() {
    let names: Vec<&str> = FAMILIES.iter().map(|family| family.name()).collect();
    assert_eq!(names, ["health", "terminal_ws"]);
}

#[tokio::test]
async fn eight_surfaces_are_registered_families() {
    let state = FrontDoorState::new(common::refused_addr().await, BackendState::Down, auth(None));
    let table = RouteTable::new(FAMILIES, &BTreeMap::new(), &state);
    let mut cases: Vec<(Method, &str, bool, &str)> = vec![
        (Method::GET, "/_gobby/ws/bind", false, "ws_bridge"),
        (
            Method::POST,
            "/api/internal/nodes/x",
            false,
            "node_internal",
        ),
        (
            Method::POST,
            "/api/runtime/handshake/challenge",
            false,
            "runtime_challenge",
        ),
        (Method::GET, "/api/nodes/channel", false, "node_channel"),
        (Method::GET, "/api/machines", false, "machines"),
        (Method::GET, "/ws", true, "ws_native"),
    ];
    if cfg!(unix) {
        cases.push((
            Method::POST,
            "/api/internal/terminals/write",
            false,
            "terminal_write",
        ));
        cases.push((Method::GET, "/api/admin/lease/status", false, "lease_admin"));
    }
    for (method, path, upgrade, name) in cases {
        assert_eq!(
            table.claim_owner(&method, path, upgrade),
            Some(name),
            "{method} {path}"
        );
    }
}

#[test]
fn front_door_handle_has_no_hand_wired_surface() {
    let source = include_str!("../src/front_door/mod.rs");
    // The Node door arm keeps its own branches and ends at its relay forward. Every line
    // after that arm is the backend door, which must not wire a surface directly.
    let (_, backend_door) = source
        .split_once("node.relay.forward(request).await")
        .expect("node door arm");
    for call in [
        "publish::handle(",
        "terminal_api::handle(",
        "lease::admin_api::handle(",
        "internal_api::handle(",
        "challenge::answer(",
        "nodes::channel::handle(",
        "machines_api::handle(",
        "native_ws.accept(",
    ] {
        assert!(
            !backend_door.contains(call),
            "the backend door still calls {call} directly"
        );
    }
}

#[test]
fn identity_header_names_agree() {
    let source = include_str!("../src/front_door/auth.rs");
    let (_, list) = source
        .split_once("const IDENTITY_HEADERS: [&str; 4] = [")
        .expect("authentication names the headers it writes");
    let (list, _) = list.split_once("];").expect("the header list ends");
    let written: Vec<&str> = list
        .split(',')
        .map(|item| item.trim().trim_matches('"'))
        .filter(|item| !item.is_empty())
        .collect();

    let mut expected: Vec<&str> = IDENTITY_ENTRY_HEADERS.to_vec();
    expected.push("x-gobby-front-door");
    assert_eq!(
        written, expected,
        "authentication writes the identity headers the entry strip removes, then the proof"
    );
}

#[tokio::test]
async fn listener_door_keeps_machines_before_any_upgrade() {
    let mut state =
        FrontDoorState::new(common::refused_addr().await, BackendState::Down, auth(None));
    state.ws_listener = true;
    let door = door(state, FAMILIES, &BTreeMap::new());

    let plain = send(&door, get("/api/machines"), loopback_peer()).await;
    let plain_status = plain.status();
    let plain_body = body_text(plain).await;
    assert_ne!(plain_status, StatusCode::UPGRADE_REQUIRED);

    let upgraded = send(&door, upgrade_request("/api/machines"), loopback_peer()).await;
    assert_eq!(upgraded.status(), plain_status);
    assert_eq!(body_text(upgraded).await, plain_body);
}

#[test]
fn health_is_a_registry_family() {
    let home = Path::new("/nonexistent/gobby-home");
    let health = selectable(None, home)
        .into_iter()
        .find(|family| family.name() == "health");
    assert!(health.is_some(), "health is not a registry family");
    if let Some(health) = health {
        assert!(health.prefixes().contains(&"/api/health"));
        assert_eq!(health.default_backend(), RouteBackend::Proxy);
    }
}

#[tokio::test]
async fn surface_claim_ignores_a_proxy_route() {
    let state = FrontDoorState::new(common::refused_addr().await, BackendState::Down, auth(None));
    let routes = BTreeMap::from([("machines".to_owned(), RouteBackend::Proxy)]);
    let table = RouteTable::new(FAMILIES, &routes, &state);
    assert_eq!(
        table.claim_owner(&Method::GET, "/api/machines", false),
        Some("machines")
    );
}

#[tokio::test]
async fn fixed_surface_names_in_routes_do_not_change_answers() {
    let mut answers = Vec::new();
    for backend in [
        RouteBackend::Native,
        RouteBackend::Compare,
        RouteBackend::Proxy,
    ] {
        let routes = BTreeMap::from([
            ("machines".to_owned(), backend),
            ("ws_bridge".to_owned(), backend),
        ]);
        let state =
            FrontDoorState::new(common::refused_addr().await, BackendState::Down, auth(None));
        let door = door(state, FAMILIES, &routes);
        let response = send(&door, get("/api/machines"), loopback_peer()).await;
        let status = response.status();
        answers.push((status, body_text(response).await));

        let unimplemented = unimplemented_families(FAMILIES, &routes);
        assert_eq!(
            unimplemented.contains(&"machines".to_owned()),
            backend != RouteBackend::Proxy,
            "unimplemented names for {backend:?}"
        );
    }
    assert!(
        answers.windows(2).all(|pair| pair[0] == pair[1]),
        "the machines answer changed with the routes map: {answers:?}"
    );
}

#[tokio::test]
async fn terminal_relay_stays_splice_for_unclaimed_upgrades() {
    let refused_state =
        FrontDoorState::new(common::refused_addr().await, BackendState::Down, auth(None));
    let refused_door = door(refused_state, FAMILIES, &BTreeMap::new());
    let refused = send(
        &refused_door,
        upgrade_request("/api/terminals/ws"),
        loopback_peer(),
    )
    .await;
    assert_eq!(refused.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert!(
        body_text(refused)
            .await
            .contains(r#""status":"unavailable""#)
    );

    // A reachable backend answers the upgrade itself, which shows the request was spliced.
    let state = FrontDoorState::new(stub_backend().await, BackendState::Down, auth(None));
    let door = door(state, FAMILIES, &BTreeMap::new());
    let spliced = send(&door, upgrade_request("/api/terminals/ws"), loopback_peer()).await;
    assert_eq!(spliced.status(), StatusCode::OK);
    assert_eq!(body_text(spliced).await, "backend");
}

#[tokio::test]
async fn encoded_spelling_of_a_claimed_path_is_proxied() {
    let state = FrontDoorState::new(stub_backend().await, BackendState::Down, auth(None));
    let door = door(state, FAMILIES, &BTreeMap::new());

    let encoded = send(
        &door,
        Request::post("/api/runtime/handshake/%63hallenge")
            .body(Body::empty())
            .expect("encoded request"),
        loopback_peer(),
    )
    .await;
    assert_eq!(encoded.status(), StatusCode::OK);
    assert_eq!(body_text(encoded).await, "backend");

    let plain = send(
        &door,
        Request::post("/api/runtime/handshake/challenge")
            .body(Body::empty())
            .expect("plain request"),
        loopback_peer(),
    )
    .await;
    // The challenge handler refuses an empty body itself; the spelling is never proxied.
    assert_eq!(plain.status(), StatusCode::BAD_REQUEST);
    assert_eq!(
        body_text(plain).await,
        r#"{"detail":"invalid challenge request"}"#
    );
}

#[cfg(unix)]
#[tokio::test]
async fn lease_admin_wrong_method_keeps_the_handler_answer() {
    let door = write_door_harness(
        common::refused_addr().await,
        BackendStatus::Standby,
        FAMILIES,
        Some(Arc::new(FixedKey)),
    );
    let keyed = format!("Bearer {}", common::channel::KEY);

    let headerless = send(&door, get("/api/admin/lease/promote"), loopback_peer()).await;
    assert_eq!(headerless.status(), StatusCode::UNAUTHORIZED);
    assert!(
        body_text(headerless)
            .await
            .contains("authentication_required")
    );

    for method in [Method::POST, Method::GET] {
        let request = Request::builder()
            .method(method.clone())
            .uri("/api/admin/lease/status")
            .header("authorization", keyed.as_str())
            .body(Body::empty())
            .expect("keyed lease request");
        let response = send(&door, request, loopback_peer()).await;
        assert_ne!(
            response.status(),
            StatusCode::SERVICE_UNAVAILABLE,
            "{method} must not answer as a standby refusal"
        );
        assert_eq!(response.status(), StatusCode::CONFLICT, "{method}");
        assert!(body_text(response).await.contains("no singleton lease"));
    }

    // With a lease held, the handler's method switch answers. A POST to the status route is
    // refused before any database access, so the lease needs only a pool that is never dialed.
    let pool = Pool::build(
        "postgresql://gobby@127.0.0.1:1/unused",
        PoolSettings {
            max_size: 2,
            application_name: "gobby-gdaemon-family-seam".into(),
            acquire_timeout: std::time::Duration::from_secs(1),
        },
    )
    .expect("lazy pool");
    let lease = ActiveDaemonLease::new(
        Arc::new(pool),
        "family-seam",
        "family-seam-token",
        RunMode::Hub,
    )
    .expect("lease");
    let lease_state = Some(lease.subscribe());
    let (_status, backend) = tokio::sync::watch::channel(BackendStatus::Standby);
    let runtime = Arc::new(RuntimeState {
        mode: RunMode::Hub,
        lease: Some(Arc::new(NamedLock::new(lease))),
        lease_state,
        backend,
        backend_wait: tokio::sync::watch::channel(None).1,
        write_token: TokenSlot::default(),
        terminal_route: Default::default(),
    });
    let mut state = FrontDoorState::new(
        common::refused_addr().await,
        BackendState::Down,
        auth(Some(Arc::new(FixedKey))),
    );
    state.runtime = Some(runtime);
    let table = RouteTable::new(FAMILIES, &BTreeMap::new(), &state);
    let leased = FrontDoor::new(state, table);
    let wrong_method = Request::post("/api/admin/lease/status")
        .header("authorization", keyed.as_str())
        .body(Body::empty())
        .expect("keyed post");
    let response = send(&leased, wrong_method, loopback_peer()).await;
    assert_eq!(response.status(), StatusCode::METHOD_NOT_ALLOWED);
    assert_eq!(
        body_text(response).await,
        r#"{"detail":"method not allowed"}"#
    );
}

static CALLER_CLAIM: &[RouteClaim] = &[RouteClaim {
    method: None,
    path: PathMatch::Exact("/fixture/caller"),
    gate: Gate::AfterRefusal,
    credential: Credential::AfterAuthenticate,
    peer: PeerRule::Any,
    kind: Kind::Any,
}];

/// A Native family whose authenticated row answers `<caller>|<header>`: the user the
/// context reads from the request, or `none`, and the user-id header the handler sees.
struct CallerEcho {
    runs: AtomicUsize,
}

impl CallerEcho {
    fn leak() -> &'static CallerEcho {
        Box::leak(Box::new(CallerEcho {
            runs: AtomicUsize::new(0),
        }))
    }
}

impl RouteFamily<FrontDoorState> for CallerEcho {
    fn name(&self) -> &'static str {
        "caller_echo"
    }

    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }

    fn router(&self) -> Router<FrontDoorState> {
        Router::new()
    }

    fn default_backend(&self) -> RouteBackend {
        RouteBackend::Native
    }

    fn route_claims(&self) -> &'static [RouteClaim] {
        CALLER_CLAIM
    }

    fn serve_claim<'a>(
        &'a self,
        state: &'a FrontDoorState,
        _peer: SocketAddr,
        request: Request<Body>,
    ) -> BoxFut<'a, Result<Request<Body>, Response<Body>>> {
        self.runs.fetch_add(1, Ordering::SeqCst);
        let (parts, _body) = request.into_parts();
        let caller = state
            .context
            .caller(&parts)
            .map_or_else(|| "none".to_owned(), |caller| caller.user_id);
        let header = parts
            .headers
            .get("x-gobby-user-id")
            .and_then(|value| value.to_str().ok())
            .unwrap_or_default()
            .to_owned();
        Box::pin(async move { Err(Response::new(Body::from(format!("{caller}|{header}")))) })
    }
}

#[cfg(unix)]
#[tokio::test]
async fn caller_identity_reaches_authenticated_claims_only() {
    assert!(
        gobby_core::api_key_format::parse(common::channel::KEY).is_some(),
        "the fixture key is a checksum-valid API key"
    );
    let echo = CallerEcho::leak();
    let families: [&'static dyn RouteFamily<FrontDoorState>; 1] = [echo];
    let door = write_door_harness(
        common::refused_addr().await,
        BackendStatus::Standby,
        &families,
        Some(Arc::new(FixedKey)),
    );
    let request = Request::get("/fixture/caller")
        .header("authorization", format!("Bearer {}", common::channel::KEY))
        .body(Body::empty())
        .expect("keyed caller request");

    let response = send(&door, request, loopback_peer()).await;

    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(body_text(response).await, "user-1|user-1");
    assert_eq!(echo.runs.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn after_authenticate_handler_sees_no_caller_for_headerless() {
    let echo = CallerEcho::leak();
    let families: [&'static dyn RouteFamily<FrontDoorState>; 1] = [echo];
    let state = FrontDoorState::new(common::refused_addr().await, BackendState::Down, auth(None));
    let door = door(state, &families, &BTreeMap::new());

    let response = send(&door, get("/fixture/caller"), loopback_peer()).await;

    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(body_text(response).await, "none|");
    assert_eq!(echo.runs.load(Ordering::SeqCst), 1);
}

#[derive(Debug)]
struct SubscriberGone;

#[tokio::test]
async fn broker_publisher_delivers_to_a_subscriber() {
    let broker = BroadcastBroker::default();
    let (tx, mut received) = tokio::sync::mpsc::unbounded_channel::<Message>();
    let sink = futures_util::sink::unfold(tx, |tx, message: Message| async move {
        tx.send(message).map_err(|_| SubscriberGone)?;
        Ok::<_, SubscriberGone>(tx)
    });
    let _registration = broker
        .register(
            ConnectionId::new(1),
            EntryMode::Browser,
            false,
            Box::pin(sink),
            || {},
        )
        .expect("a fresh connection registers");
    let (config_publisher, config) = config_channel(ConfigEpoch::unpublished());
    let ctx = FamilyContext::new(ContextParts {
        publisher: Arc::new(BroadcastBrokerPublisher::new(broker.clone())),
        backend: None,
        lease: None,
        budget: None,
        serve_pool: None,
        config,
        home: std::path::PathBuf::new(),
        degraded: DegradedServices::default(),
        config_publisher,
        startup: None,
        extensions: Extensions::new(),
    });

    let published = ctx
        .publisher()
        .publish_frames(&json!({"type": "fixture_event"}), vec!["frame-1".into()]);

    assert_eq!(published, Ok(()));
    let frame = tokio::time::timeout(std::time::Duration::from_secs(1), received.recv())
        .await
        .ok()
        .flatten();
    assert_eq!(frame, Some(Message::text("frame-1")));
}
