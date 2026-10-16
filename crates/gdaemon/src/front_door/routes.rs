//! The routing table: which route families gdaemon serves natively, which it proxies,
//! and which it compares.
//!
//! A [`RouteFamily`] claims path prefixes and supplies an `axum::Router` over the
//! shared [`FrontDoorState`]. Families are composed statically in [`FAMILIES`]; the
//! bootstrap `front_door.routes` map picks a [`RouteBackend`] per family, read once at
//! start, and a family absent from it takes its `default_backend`. Paths no family
//! claims, requests a family's `claims` declines, and every upgrade stay on the hyper
//! proxy.

use std::collections::{BTreeMap, BTreeSet};
use std::convert::Infallible;
use std::panic::AssertUnwindSafe;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::Router;
use axum::body::Body;
use axum::extract::{Request, State};
use axum::http::header::{CACHE_CONTROL, CONTENT_TYPE};
use axum::http::request::Parts;
use axum::http::response::Parts as ResponseParts;
use axum::http::{HeaderMap, Method, Response, StatusCode};
use bytes::Bytes;
use futures_util::FutureExt;
use gobby_core::bootstrap::RouteBackend;
use gobby_core::route_family::ProxiedCheck;
use http_body_util::BodyExt;
use serde_json::Value;
use tower_service::Service;

use super::FrontDoorState;
use super::compare::{Outcome, Record};
use super::health::HealthFamily;
use super::proxy::{self, ForwardError};

pub use gobby_core::route_family::RouteFamily;

/// Every family gdaemon implements. Stage 2 leaves add their families here.
pub static FAMILIES: &[&dyn RouteFamily<FrontDoorState>] = &[&HealthFamily, &TerminalFamily];

/// The lifecycle is native; terminal upgrades retain the existing proxy transport.
struct TerminalFamily;

impl RouteFamily<FrontDoorState> for TerminalFamily {
    fn name(&self) -> &'static str {
        "terminal_ws"
    }
    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }
    fn router(&self) -> Router<FrontDoorState> {
        Router::new()
    }
}

struct Entry {
    family: &'static dyn RouteFamily<FrontDoorState>,
    backend: RouteBackend,
    router: Router,
}

/// The routing table for one listener.
pub struct RouteTable {
    entries: Vec<Entry>,
}

impl RouteTable {
    pub fn new(
        families: &[&'static dyn RouteFamily<FrontDoorState>],
        routes: &BTreeMap<String, RouteBackend>,
        state: &FrontDoorState,
    ) -> Self {
        let entries = families
            .iter()
            .map(|&family| Entry {
                family,
                backend: routes
                    .get(family.name())
                    .copied()
                    .unwrap_or_else(|| family.default_backend()),
                router: family
                    .router()
                    .fallback(proxy_fallback)
                    .with_state(state.clone()),
            })
            .collect();
        Self { entries }
    }

    /// Serve one non-upgrade request.
    pub async fn dispatch(&self, state: &FrontDoorState, request: Request) -> Response<Body> {
        let Some(entry) = self.lookup(request.uri().path()) else {
            return proxy::forward(state, request).await;
        };
        if entry.backend == RouteBackend::Proxy {
            return proxy::forward(state, request).await;
        }
        // A request the family does not claim reaches the proxy unchanged.
        let (parts, body) = request.into_parts();
        let claimed = entry.family.claims(&parts);
        let request = Request::from_parts(parts, body);
        match (entry.backend, claimed) {
            (RouteBackend::Native, true) => call(&entry.router, request).await,
            (RouteBackend::Compare, true) => compare(entry, state, request).await,
            _ => proxy::forward(state, request).await,
        }
    }

    /// The longest claimed prefix wins.
    fn lookup(&self, path: &str) -> Option<&Entry> {
        self.entries
            .iter()
            .filter_map(|entry| {
                entry
                    .family
                    .prefixes()
                    .iter()
                    .filter(|prefix| claims(prefix, path))
                    .map(|prefix| prefix.len())
                    .max()
                    .map(|len| (len, entry))
            })
            .max_by_key(|(len, _)| *len)
            .map(|(_, entry)| entry)
    }
}

/// Names in `routes` that ask for a native or compare backend gdaemon does not
/// implement; those requests are proxied. The parser accepts unknown names because
/// Stage 2 leaves introduce families.
pub fn unimplemented_families(
    families: &[&dyn RouteFamily<FrontDoorState>],
    routes: &BTreeMap<String, RouteBackend>,
) -> Vec<String> {
    routes
        .iter()
        .filter(|(name, backend)| {
            **backend != RouteBackend::Proxy && !families.iter().any(|f| f.name() == *name)
        })
        .map(|(name, _)| name.clone())
        .collect()
}

fn claims(prefix: &str, path: &str) -> bool {
    path.strip_prefix(prefix)
        .is_some_and(|rest| rest.is_empty() || rest.starts_with('/'))
}

async fn proxy_fallback(State(state): State<FrontDoorState>, request: Request) -> Response<Body> {
    proxy::forward(&state, request).await
}

async fn call(router: &Router, request: Request) -> Response<Body> {
    let result: Result<Response<Body>, Infallible> = router.clone().call(request).await;
    match result {
        Ok(response) => response,
        Err(never) => match never {},
    }
}

/// Compare mode for a claimed request; the caller always receives the proxied
/// response and every outcome is recorded. GET and HEAD run both legs and compare
/// them. Any other method proxies once and runs the family's `check_proxied`, so a
/// write never runs twice. Only this mode buffers bodies.
async fn compare(entry: &Entry, state: &FrontDoorState, request: Request) -> Response<Body> {
    let (parts, body) = request.into_parts();
    let body = match body.collect().await {
        Ok(collected) => collected.to_bytes(),
        Err(error) => {
            let mut response = Response::new(Body::from(format!("request body: {error}")));
            *response.status_mut() = StatusCode::BAD_REQUEST;
            return response;
        }
    };
    let proxied_request = copy_request(&parts, body.clone());

    if parts.method == Method::GET || parts.method == Method::HEAD {
        let native_request = copy_request(&parts, body);
        let (native, proxied) = tokio::join!(
            native_leg(&entry.router, native_request),
            proxied_leg(state, proxied_request)
        );
        let (proxied_parts, proxied_body) = match proxied {
            Ok(proxied) => proxied,
            Err(error) => return unanswered(entry, state, &parts, error),
        };
        let (outcome, native_status, detail) = match native {
            Ok((native_parts, native_body)) => {
                let detail = difference(&native_parts, &native_body, &proxied_parts, &proxied_body);
                let outcome = if detail.is_some() {
                    Outcome::Diff
                } else {
                    Outcome::Match
                };
                (outcome, Some(native_parts.status), detail)
            }
            Err(NativeFailure::Body(status)) => {
                (Outcome::NativeError, Some(status), Some("body".to_owned()))
            }
            Err(NativeFailure::Panic) => (Outcome::NativeError, None, Some("panic".to_owned())),
        };
        let proxied_status = Some(proxied_parts.status);
        record(
            entry,
            state,
            &parts,
            outcome,
            native_status,
            proxied_status,
            detail,
        );
        return Response::from_parts(proxied_parts, Body::from(proxied_body));
    }

    // Any other method may write: proxy once and let the family check Python's
    // answer against the request bytes.
    let (proxied_parts, proxied_body) = match proxied_leg(state, proxied_request).await {
        Ok(proxied) => proxied,
        Err(error) => return unanswered(entry, state, &parts, error),
    };
    let check = entry
        .family
        .check_proxied(&parts, &body, proxied_parts.status, &proxied_body)
        .await;
    let (outcome, detail) = match check {
        ProxiedCheck::Skipped => (Outcome::Skipped, None),
        ProxiedCheck::Verified => (Outcome::Verified, None),
        ProxiedCheck::Mismatch(detail) => (Outcome::Mismatch, Some(detail)),
    };
    let proxied_status = Some(proxied_parts.status);
    record(entry, state, &parts, outcome, None, proxied_status, detail);
    Response::from_parts(proxied_parts, Body::from(proxied_body))
}

/// A request with `parts`' method, URI, version and headers over `body`.
fn copy_request(parts: &Parts, body: Bytes) -> Request {
    let mut request = Request::new(Body::from(body));
    *request.method_mut() = parts.method.clone();
    *request.uri_mut() = parts.uri.clone();
    *request.version_mut() = parts.version;
    *request.headers_mut() = parts.headers.clone();
    request
}

/// Why the native leg produced no comparable response.
enum NativeFailure {
    /// The body failed to collect after the handler answered `StatusCode`.
    Body(StatusCode),
    /// The handler panicked.
    Panic,
}

/// Runs the native leg to a collected body. A panic stays inside this leg, so the
/// caller still receives the proxied response.
async fn native_leg(
    router: &Router,
    request: Request,
) -> Result<(ResponseParts, Bytes), NativeFailure> {
    let leg = async {
        let (parts, body) = call(router, request).await.into_parts();
        match body.collect().await {
            Ok(collected) => Ok((parts, collected.to_bytes())),
            Err(_) => Err(NativeFailure::Body(parts.status)),
        }
    };
    AssertUnwindSafe(leg)
        .catch_unwind()
        .await
        .unwrap_or(Err(NativeFailure::Panic))
}

/// Forwards to Python and collects its answer.
async fn proxied_leg(
    state: &FrontDoorState,
    request: Request,
) -> Result<(ResponseParts, Bytes), ForwardError> {
    let (parts, body) = proxy::try_forward(state, request).await?.into_parts();
    let body = body
        .collect()
        .await
        .map_err(|error| ForwardError::BadGateway(error.into()))?;
    Ok((parts, body.to_bytes()))
}

/// Python gave no answer to compare: record `skipped` and return the response
/// `proxy::forward` builds for the same failure.
fn unanswered(
    entry: &Entry,
    state: &FrontDoorState,
    parts: &Parts,
    error: ForwardError,
) -> Response<Body> {
    let detail = match error {
        ForwardError::Unavailable => "backend_unavailable",
        ForwardError::BadGateway(_) => "bad_gateway",
    };
    record(
        entry,
        state,
        parts,
        Outcome::Skipped,
        None,
        None,
        Some(detail.to_owned()),
    );
    error.into_response(state)
}

/// The first way the native response differs from the proxied one: `status`,
/// `header:<name>`, the first differing JSON pointer when both bodies are JSON, or
/// `bytes`. `None` when they match.
fn difference(
    native: &ResponseParts,
    native_body: &[u8],
    proxied: &ResponseParts,
    proxied_body: &[u8],
) -> Option<String> {
    if native.status != proxied.status {
        return Some("status".to_owned());
    }
    for name in [CONTENT_TYPE, CACHE_CONTROL] {
        if !native
            .headers
            .get_all(&name)
            .iter()
            .eq(proxied.headers.get_all(&name))
        {
            return Some(format!("header:{name}"));
        }
    }
    if is_json(&proxied.headers)
        && let (Ok(native), Ok(proxied)) = (
            serde_json::from_slice::<Value>(native_body),
            serde_json::from_slice::<Value>(proxied_body),
        )
    {
        return first_difference(&native, &proxied, String::new());
    }
    (native_body != proxied_body).then(|| "bytes".to_owned())
}

fn is_json(headers: &HeaderMap) -> bool {
    headers
        .get(CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(';').next())
        .is_some_and(|media| media.trim().eq_ignore_ascii_case("application/json"))
}

/// The JSON pointer of the first difference, visiting object keys in sorted order.
fn first_difference(native: &Value, proxied: &Value, pointer: String) -> Option<String> {
    match (native, proxied) {
        (Value::Object(native), Value::Object(proxied)) => {
            let keys: BTreeSet<&String> = native.keys().chain(proxied.keys()).collect();
            keys.into_iter().find_map(|key| {
                let escaped = key.replace('~', "~0").replace('/', "~1");
                let pointer = format!("{pointer}/{escaped}");
                match (native.get(key), proxied.get(key)) {
                    (Some(native), Some(proxied)) => first_difference(native, proxied, pointer),
                    _ => Some(pointer),
                }
            })
        }
        (Value::Array(native), Value::Array(proxied)) => (0..native.len().max(proxied.len()))
            .find_map(|index| {
                let pointer = format!("{pointer}/{index}");
                match (native.get(index), proxied.get(index)) {
                    (Some(native), Some(proxied)) => first_difference(native, proxied, pointer),
                    _ => Some(pointer),
                }
            }),
        _ => (native != proxied).then_some(pointer),
    }
}

/// Appends one compare record; the path drops its query and no body value is kept.
fn record(
    entry: &Entry,
    state: &FrontDoorState,
    parts: &Parts,
    outcome: Outcome,
    native_status: Option<StatusCode>,
    proxied_status: Option<StatusCode>,
    detail: Option<String>,
) {
    let Some(log) = &state.compare_log else {
        return;
    };
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| {
            i64::try_from(elapsed.as_secs()).unwrap_or(i64::MAX)
        });
    log.append(&Record {
        ts,
        family: entry.family.name().to_owned(),
        method: parts.method.to_string(),
        path: parts.uri.path().to_owned(),
        outcome,
        native_status: native_status.map(|status| status.as_u16()),
        proxied_status: proxied_status.map(|status| status.as_u16()),
        detail,
    });
}

#[cfg(test)]
mod tests {
    use std::net::SocketAddr;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use axum::http::HeaderValue;
    use hyper::server::conn::http1;
    use hyper::service::service_fn;
    use hyper_util::rt::TokioIo;
    use tokio::net::TcpListener;

    use super::*;
    use crate::front_door::health::{BackendState, SERVED_BY_HEADER};

    const PYTHON_HEALTH: &str = r#"{"status":"healthy","from":"python"}"#;

    /// A backend that answers every request with Python's health body and counts hits.
    async fn python_stub() -> (SocketAddr, Arc<AtomicUsize>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind stub");
        let addr = listener.local_addr().expect("stub addr");
        let hits = Arc::new(AtomicUsize::new(0));
        let counter = hits.clone();
        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                let counter = counter.clone();
                tokio::spawn(async move {
                    let service = service_fn(move |_request| {
                        counter.fetch_add(1, Ordering::SeqCst);
                        async { Ok::<_, Infallible>(Response::new(Body::from(PYTHON_HEALTH))) }
                    });
                    let _ = http1::Builder::new()
                        .serve_connection(TokioIo::new(stream), service)
                        .await;
                });
            }
        });
        (addr, hits)
    }

    fn table(backend: RouteBackend, state: &FrontDoorState) -> RouteTable {
        let routes = BTreeMap::from([("health".to_owned(), backend)]);
        RouteTable::new(FAMILIES, &routes, state)
    }

    async fn get(table: &RouteTable, state: &FrontDoorState, path: &str) -> Response<Body> {
        let request = Request::get(path).body(Body::empty()).expect("request");
        table.dispatch(state, request).await
    }

    async fn body_text(response: Response<Body>) -> String {
        let bytes = response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes();
        String::from_utf8(bytes.to_vec()).expect("utf8")
    }

    #[tokio::test]
    async fn health_family_honors_proxy_native_and_compare() {
        let (addr, hits) = python_stub().await;
        let state = FrontDoorState::new(
            addr,
            BackendState::Down,
            Arc::new(
                super::super::auth::AuthState::new("test-secret".into(), None, Default::default())
                    .expect("auth"),
            ),
        );

        let proxied = get(&table(RouteBackend::Proxy, &state), &state, "/api/health").await;
        assert_eq!(proxied.headers().get(SERVED_BY_HEADER), None);
        assert_eq!(body_text(proxied).await, PYTHON_HEALTH);
        assert_eq!(hits.load(Ordering::SeqCst), 1);

        let native = get(&table(RouteBackend::Native, &state), &state, "/api/health").await;
        assert_eq!(
            native.headers().get(SERVED_BY_HEADER),
            Some(&HeaderValue::from_static("gdaemon"))
        );
        assert_eq!(body_text(native).await, PYTHON_HEALTH);
        assert_eq!(hits.load(Ordering::SeqCst), 2);

        // Compare runs the native handler and the proxy, and returns the proxied leg.
        let compared = get(&table(RouteBackend::Compare, &state), &state, "/api/health").await;
        assert_eq!(compared.headers().get(SERVED_BY_HEADER), None);
        assert_eq!(body_text(compared).await, PYTHON_HEALTH);
        assert_eq!(hits.load(Ordering::SeqCst), 4);
    }

    #[tokio::test]
    async fn native_health_returns_typed_503_while_backend_refuses() {
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let refused = listener.local_addr().expect("addr");
        drop(listener);
        let state = FrontDoorState::new(
            refused,
            BackendState::Down,
            Arc::new(
                super::super::auth::AuthState::new("test-secret".into(), None, Default::default())
                    .expect("auth"),
            ),
        );

        let response = get(&table(RouteBackend::Native, &state), &state, "/api/health").await;

        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(response.headers().get("retry-after").unwrap(), "1");
        let body: serde_json::Value =
            serde_json::from_str(&body_text(response).await).expect("json body");
        assert_eq!(body["backend"]["state"], "down");
        assert_eq!(body["backend"]["target"], refused.to_string());
    }

    #[tokio::test]
    async fn unclaimed_methods_and_paths_under_a_native_family_are_proxied() {
        let (addr, hits) = python_stub().await;
        let state = FrontDoorState::new(
            addr,
            BackendState::Down,
            Arc::new(
                super::super::auth::AuthState::new("test-secret".into(), None, Default::default())
                    .expect("auth"),
            ),
        );
        let table = table(RouteBackend::Native, &state);

        let deeper = get(&table, &state, "/api/health/details").await;
        assert_eq!(deeper.headers().get(SERVED_BY_HEADER), None);
        let other = get(&table, &state, "/api/healthz").await;
        assert_eq!(other.headers().get(SERVED_BY_HEADER), None);
        assert_eq!(hits.load(Ordering::SeqCst), 2);
    }

    #[test]
    fn prefixes_match_whole_segments() {
        assert!(claims("/api/health", "/api/health"));
        assert!(claims("/api/health", "/api/health/deep"));
        assert!(!claims("/api/health", "/api/healthz"));
    }

    #[test]
    fn reports_native_routes_for_families_gdaemon_lacks() {
        let routes = BTreeMap::from([
            ("health".to_owned(), RouteBackend::Native),
            ("sessions".to_owned(), RouteBackend::Compare),
            ("terminal_ws".to_owned(), RouteBackend::Proxy),
        ]);
        assert_eq!(unimplemented_families(FAMILIES, &routes), vec!["sessions"]);
    }

    fn state_for(addr: SocketAddr) -> FrontDoorState {
        FrontDoorState::new(
            addr,
            BackendState::Down,
            Arc::new(
                super::super::auth::AuthState::new("test-secret".into(), None, Default::default())
                    .expect("auth"),
            ),
        )
    }

    /// Serves `/api/claimed` natively, but claims only requests that carry `x-claim`.
    struct HeaderClaimFamily;

    static HEADER_CLAIM_NATIVE_HITS: AtomicUsize = AtomicUsize::new(0);

    impl RouteFamily<FrontDoorState> for HeaderClaimFamily {
        fn name(&self) -> &'static str {
            "header_claim"
        }
        fn prefixes(&self) -> &'static [&'static str] {
            &["/api/claimed"]
        }
        fn router(&self) -> Router<FrontDoorState> {
            Router::new().route(
                "/api/claimed",
                axum::routing::get(|| async {
                    HEADER_CLAIM_NATIVE_HITS.fetch_add(1, Ordering::SeqCst);
                    ([(SERVED_BY_HEADER, "gdaemon")], "native")
                }),
            )
        }
        fn claims(&self, parts: &axum::http::request::Parts) -> bool {
            parts.headers.contains_key("x-claim")
        }
    }

    /// Native unless `front_door.routes` names it.
    struct NativeByDefaultFamily;

    impl RouteFamily<FrontDoorState> for NativeByDefaultFamily {
        fn name(&self) -> &'static str {
            "native_by_default"
        }
        fn prefixes(&self) -> &'static [&'static str] {
            &["/api/defaulted"]
        }
        fn router(&self) -> Router<FrontDoorState> {
            Router::new().route(
                "/api/defaulted",
                axum::routing::get(|| async { ([(SERVED_BY_HEADER, "gdaemon")], "native") }),
            )
        }
        fn default_backend(&self) -> RouteBackend {
            RouteBackend::Native
        }
    }

    #[tokio::test]
    async fn unclaimed_requests_are_proxied_under_every_backend() {
        let (addr, hits) = python_stub().await;
        let state = state_for(addr);
        let families: &[&'static dyn RouteFamily<FrontDoorState>] = &[&HeaderClaimFamily];

        for backend in [
            RouteBackend::Proxy,
            RouteBackend::Native,
            RouteBackend::Compare,
        ] {
            let routes = BTreeMap::from([("header_claim".to_owned(), backend)]);
            let table = RouteTable::new(families, &routes, &state);
            let before = hits.load(Ordering::SeqCst);

            let unclaimed = get(&table, &state, "/api/claimed").await;

            assert_eq!(
                unclaimed.headers().get(SERVED_BY_HEADER),
                None,
                "{backend:?}"
            );
            assert_eq!(body_text(unclaimed).await, PYTHON_HEALTH, "{backend:?}");
            assert_eq!(hits.load(Ordering::SeqCst), before + 1, "{backend:?}");
        }
        assert_eq!(HEADER_CLAIM_NATIVE_HITS.load(Ordering::SeqCst), 0);

        // The same family serves the requests it does claim.
        let routes = BTreeMap::from([("header_claim".to_owned(), RouteBackend::Native)]);
        let table = RouteTable::new(families, &routes, &state);
        let request = Request::get("/api/claimed")
            .header("x-claim", "1")
            .body(Body::empty())
            .expect("request");
        let claimed = table.dispatch(&state, request).await;
        assert_eq!(
            claimed.headers().get(SERVED_BY_HEADER),
            Some(&HeaderValue::from_static("gdaemon"))
        );
        assert_eq!(HEADER_CLAIM_NATIVE_HITS.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn absent_route_takes_the_family_default() {
        let (addr, hits) = python_stub().await;
        let state = state_for(addr);
        let families: &[&'static dyn RouteFamily<FrontDoorState>] = &[&NativeByDefaultFamily];

        let absent = RouteTable::new(families, &BTreeMap::new(), &state);
        let defaulted = get(&absent, &state, "/api/defaulted").await;
        assert_eq!(
            defaulted.headers().get(SERVED_BY_HEADER),
            Some(&HeaderValue::from_static("gdaemon"))
        );
        assert_eq!(hits.load(Ordering::SeqCst), 0);

        let routes = BTreeMap::from([("native_by_default".to_owned(), RouteBackend::Proxy)]);
        let overridden = RouteTable::new(families, &routes, &state);
        let proxied = get(&overridden, &state, "/api/defaulted").await;
        assert_eq!(proxied.headers().get(SERVED_BY_HEADER), None);
        assert_eq!(body_text(proxied).await, PYTHON_HEALTH);
        assert_eq!(hits.load(Ordering::SeqCst), 1);

        // Every shipped family stays proxied until bootstrap opts it in.
        for family in FAMILIES {
            assert_eq!(
                family.default_backend(),
                RouteBackend::Proxy,
                "{}",
                family.name()
            );
        }
    }
}
