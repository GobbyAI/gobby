//! The `runtime` route family: `/api/runtime/*` and `/api/config/*`, served by
//! gdaemon from Python's published state, with Python as the proxy target.

pub mod config;

use std::path::PathBuf;
use std::sync::Arc;

use axum::Router;
use axum::http::Method;
use axum::http::request::Parts;
use axum::routing::{MethodFilter, MethodRouter, on};
use gobby_core::postgres_pool::Pool;
use gobby_core::route_family::RouteFamily;

/// Who sent a request, as the front door's authentication left it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Caller {
    /// An API key the front door resolved: the request carries `x-gobby-key-id`.
    Operator,
}

impl Caller {
    pub const ALL: &[Caller] = &[Caller::Operator];

    fn of(parts: &Parts) -> Option<Self> {
        parts
            .headers
            .contains_key("x-gobby-key-id")
            .then_some(Caller::Operator)
    }
}

/// One natively served route. `path` uses axum 0.8 route syntax.
pub struct NativeRoute {
    pub method: Method,
    pub path: &'static str,
    pub callers: &'static [Caller],
    pub gate: Option<fn(&Parts) -> bool>,
}

/// Every route the family serves; requests matching no row reach Python.
pub const NATIVE_ROUTES: &[NativeRoute] = &[NativeRoute {
    method: Method::GET,
    path: "/api/config/effective",
    callers: &[Caller::Operator],
    gate: None,
}];

/// State shared by every native handler of the family.
pub(crate) struct RuntimeState {
    pool: Arc<Pool>,
    home: PathBuf,
}

/// The `runtime` family over the shared gdaemon pool and the Gobby home.
pub struct RuntimeFamily {
    state: Arc<RuntimeState>,
}

impl RuntimeFamily {
    pub fn new(pool: Arc<Pool>, home: PathBuf) -> Self {
        Self {
            state: Arc::new(RuntimeState { pool, home }),
        }
    }
}

fn handler(row: &NativeRoute) -> MethodRouter<Arc<RuntimeState>> {
    let filter = MethodFilter::try_from(row.method.clone())
        .expect("native routes use standard HTTP methods");
    match row.path {
        "/api/config/effective" => on(filter, config::effective),
        other => panic!("native route {other} has no handler"),
    }
}

/// Whether `path` matches an axum 0.8 route `pattern`: `{name}` matches one
/// non-empty segment and `{*rest}` a non-empty remainder.
fn path_matches(pattern: &str, path: &str) -> bool {
    let mut wanted = pattern.split('/');
    let mut actual = path.split('/');
    loop {
        match (wanted.next(), actual.next()) {
            (None, None) => return true,
            (Some(segment), _) if segment.starts_with("{*") => {
                return actual.any(|rest| !rest.is_empty());
            }
            (Some(segment), Some(part)) if segment.starts_with('{') => {
                if part.is_empty() {
                    return false;
                }
            }
            (Some(segment), Some(part)) if segment == part => {}
            _ => return false,
        }
    }
}

impl<S: Clone + Send + Sync + 'static> RouteFamily<S> for RuntimeFamily {
    fn name(&self) -> &'static str {
        "runtime"
    }
    fn prefixes(&self) -> &'static [&'static str] {
        &["/api/runtime", "/api/config"]
    }
    fn router(&self) -> Router<S> {
        let routes = NATIVE_ROUTES.iter().fold(Router::new(), |routes, row| {
            routes.route(row.path, handler(row))
        });
        routes.with_state(self.state.clone())
    }
    fn claims(&self, parts: &Parts) -> bool {
        let Some(caller) = Caller::of(parts) else {
            return false;
        };
        NATIVE_ROUTES.iter().any(|row| {
            row.method == parts.method
                && path_matches(row.path, parts.uri.path())
                && row.callers.contains(&caller)
                && row.gate.is_none_or(|gate| gate(parts))
        })
    }
}

#[cfg(test)]
mod tests {
    use super::path_matches;

    #[test]
    fn placeholders_match_one_segment_or_a_remainder() {
        assert!(path_matches("/api/x/{id}", "/api/x/abc"));
        assert!(!path_matches("/api/x/{id}", "/api/x/"));
        assert!(!path_matches("/api/x/{id}", "/api/x/abc/def"));
        assert!(path_matches("/api/x/{*rest}", "/api/x/a/b"));
        assert!(!path_matches("/api/x/{*rest}", "/api/x/"));
        assert!(!path_matches("/api/x/effective", "/api/x/other"));
    }
}
