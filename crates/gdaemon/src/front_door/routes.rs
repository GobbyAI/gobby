//! The routing table: which route families gdaemon serves natively, which it proxies,
//! and which it compares.
//!
//! A [`RouteFamily`] claims path prefixes and supplies an `axum::Router` over the
//! shared [`FrontDoorState`]. Families are composed statically in [`FAMILIES`]; the
//! bootstrap `front_door.routes` map picks a [`RouteBackend`] per family, read once at
//! start. Paths no family claims, and every upgrade, stay on the hyper proxy.

use std::collections::BTreeMap;
use std::convert::Infallible;

use axum::Router;
use axum::body::Body;
use axum::extract::{Request, State};
use axum::http::{Response, StatusCode};
use bytes::Bytes;
use gobby_core::bootstrap::RouteBackend;
use http_body_util::BodyExt;
use tower_service::Service;

use super::FrontDoorState;
use super::health::{self, HealthFamily};
use super::proxy;

/// A group of routes that gdaemon can serve itself.
pub trait RouteFamily: Send + Sync {
    /// The family name used as the key in `front_door.routes`.
    fn name(&self) -> &'static str;
    /// Path prefixes the family owns; matched on whole path segments.
    fn prefixes(&self) -> &'static [&'static str];
    /// Native handlers. Requests under a claimed prefix that no handler matches are
    /// proxied.
    fn router(&self) -> Router<FrontDoorState>;
}

/// Every family gdaemon implements. Stage 2 leaves add their families here.
pub static FAMILIES: &[&dyn RouteFamily] = &[&HealthFamily, &TerminalFamily];

/// The lifecycle is native; terminal upgrades retain the existing proxy transport.
struct TerminalFamily;

impl RouteFamily for TerminalFamily {
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
    name: &'static str,
    prefixes: &'static [&'static str],
    backend: RouteBackend,
    router: Router,
}

/// The routing table for one listener.
pub struct RouteTable {
    entries: Vec<Entry>,
}

impl RouteTable {
    pub fn new(
        families: &[&dyn RouteFamily],
        routes: &BTreeMap<String, RouteBackend>,
        state: &FrontDoorState,
    ) -> Self {
        let entries = families
            .iter()
            .map(|family| Entry {
                name: family.name(),
                prefixes: family.prefixes(),
                backend: routes
                    .get(family.name())
                    .copied()
                    .unwrap_or(RouteBackend::Proxy),
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
        match entry.backend {
            RouteBackend::Proxy => proxy::forward(state, request).await,
            RouteBackend::Native => call(&entry.router, request).await,
            RouteBackend::Compare => compare(entry, state, request).await,
        }
    }

    /// The longest claimed prefix wins.
    fn lookup(&self, path: &str) -> Option<&Entry> {
        self.entries
            .iter()
            .filter_map(|entry| {
                entry
                    .prefixes
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
    families: &[&dyn RouteFamily],
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

/// Run the native and proxied legs on the same request, log any difference, and
/// return the proxied response. Only this mode buffers bodies.
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
    let mut native_request = Request::new(Body::from(body.clone()));
    *native_request.method_mut() = parts.method.clone();
    *native_request.uri_mut() = parts.uri.clone();
    *native_request.version_mut() = parts.version;
    *native_request.headers_mut() = parts.headers.clone();
    let path = parts.uri.path().to_owned();
    let proxied_request = Request::from_parts(parts, Body::from(body));

    let (native, proxied) = tokio::join!(
        call(&entry.router, native_request),
        proxy::forward(state, proxied_request)
    );
    let (proxied_parts, proxied_body) = proxied.into_parts();
    let proxied_body = match proxied_body.collect().await {
        Ok(collected) => collected.to_bytes(),
        Err(error) => return health::bad_gateway(state.target, error),
    };
    let (native_parts, native_body) = native.into_parts();
    let native_body: Result<Bytes, _> = native_body
        .collect()
        .await
        .map(|collected| collected.to_bytes());
    let body_equal = native_body.as_ref().is_ok_and(|body| *body == proxied_body);
    if native_parts.status != proxied_parts.status || !body_equal {
        eprintln!(
            "front_door compare diff: family={} path={path} native_status={} proxied_status={} body_equal={body_equal} native_body_error={:?}",
            entry.name,
            native_parts.status.as_u16(),
            proxied_parts.status.as_u16(),
            native_body.err().map(|error| error.to_string()),
        );
    }
    Response::from_parts(proxied_parts, Body::from(proxied_body))
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
}
