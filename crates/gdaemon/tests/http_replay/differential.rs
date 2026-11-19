//! Differential mode (design-v2 section 7.10; H12).
//!
//! One case runs twice: once through the front door with the proxy backend for its family, and
//! once with the native backend of that family. Both legs are normalized into comparison
//! documents (`compare.rs`). Equal documents give `match`; otherwise the verdict is `diff` with the
//! first differing pointer. A family without a native backend gives `no-native-backend` and runs
//! no leg. Every verdict is appended to `differential.jsonl` under the home.
//!
//! Scope of a leg: the response is observed over HTTP. The `db_delta` and side-effect values of
//! each leg come from the caller (`Extras`), because their observers need a database or a
//! listener that only the full-daemon leg has. At landing no native family writes (FW-7).

use std::convert::Infallible;
use std::error::Error;
use std::fmt;
use std::net::SocketAddr;
use std::path::Path;

use bytes::Bytes;
use gobby_core::bootstrap::RouteBackend;
use gobby_core::route_family::RouteFamily;
use gobby_daemon::front_door::FrontDoorState;
use gobby_test_support::corpus_v2::is_json;
use gobby_test_support::listeners::{bind_loopback, local_addr};
use http_body_util::{BodyExt, Full};
use hyper::body::Incoming;
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Request, Response};
use hyper_util::rt::TokioIo;
use serde_json::Value;

use super::corpus::{Case, Kind};
use super::normalize::{NormalizeError, Window};

/// The file every verdict line is appended to, under the home.
pub const DIFFERENTIAL_FILE: &str = "differential.jsonl";

/// The outcome of one differential run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Verdict {
    /// The normalized legs are equal.
    Match,
    /// The legs differ first at this RFC 6901 pointer of the comparison document.
    Diff { pointer: String },
    /// The family has no native backend, so no leg ran.
    NoNativeBackend,
}

impl Verdict {
    /// The outcome name written to `differential.jsonl`.
    pub fn outcome(&self) -> &'static str {
        unimplemented!("not wired: differential::Verdict::outcome")
    }
}

/// What one leg produced: the observed response and the observed `db_delta` and side-effect
/// values, in the recorded shape.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Observation {
    pub response: Value,
    pub db_delta: Vec<Value>,
    pub side_effects: Vec<Value>,
}

/// The `db_delta` and side-effect values a leg's observer supplies.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Extras {
    pub db_delta: Vec<Value>,
    pub side_effects: Vec<Value>,
}

/// A differential run that cannot give a verdict.
#[derive(Debug)]
pub enum DifferentialError {
    /// Only HTTP cases have a driver request in this package.
    Unsupported { case: String, kind: Kind },
    /// A normalizer fails the case.
    Normalize(NormalizeError),
    /// The verdict file or a leg's socket failed.
    Io(std::io::Error),
    /// The case or a leg lacks what the run needs.
    Shape(String),
}

impl fmt::Display for DifferentialError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DifferentialError::Unsupported { case, kind } => {
                write!(f, "{case}: differential mode runs HTTP cases, not {kind:?}")
            }
            DifferentialError::Normalize(error) => write!(f, "{error}"),
            DifferentialError::Io(error) => write!(f, "{error}"),
            DifferentialError::Shape(detail) => write!(f, "{detail}"),
        }
    }
}

impl Error for DifferentialError {}

/// Compare the two legs of one case. The proxy leg is the expected side; the native leg is the
/// actual side. Both are normalized with the case's masks and normalizers first.
pub fn compare_legs(
    case: &Case,
    window: &Window,
    proxy: &Observation,
    native: &Observation,
) -> Result<Verdict, DifferentialError> {
    unimplemented!("not wired: differential::compare_legs")
}

/// Send the case's request to one front door whose family uses `backend`, and observe the leg.
pub async fn run_leg(
    case: &Case,
    families: &[&'static dyn RouteFamily<FrontDoorState>],
    backend: RouteBackend,
    target: SocketAddr,
    home: &Path,
    extras: Extras,
) -> Result<Observation, DifferentialError> {
    unimplemented!("not wired: differential::run_leg")
}

/// Run one case through the proxy and native legs of its family, compare, and append the verdict
/// to `home/differential.jsonl`.
pub async fn run_differential(
    case: &Case,
    families: &[&'static dyn RouteFamily<FrontDoorState>],
    target: SocketAddr,
    home: &Path,
    window: &Window,
    proxy: Extras,
    native: Extras,
) -> Result<Verdict, DifferentialError> {
    unimplemented!("not wired: differential::run_differential")
}

/// The line one verdict appends: `{"case", "family", "outcome", "pointer"}`, where `pointer` is
/// the differing pointer for `diff` and `null` otherwise.
pub fn record_line(case: &Case, verdict: &Verdict) -> Value {
    unimplemented!("not wired: differential::record_line")
}

/// A stub backend that answers every request with the status, content type and body of
/// `recorded`, a v2 or v3 `response` object. This is test scaffolding, not the behaviour under
/// test, so it is real.
pub async fn recorded_target(recorded: &Value) -> SocketAddr {
    let status = u16::try_from(recorded["status"].as_u64().expect("recorded status"))
        .expect("status fits u16");
    let content_type = recorded["headers"]["content-type"]
        .as_str()
        .map(str::to_owned);
    let body: Vec<u8> = match &recorded["body"] {
        Value::Null => Vec::new(),
        Value::String(text) if !is_json(content_type.as_deref()) => text.clone().into_bytes(),
        body => serde_json::to_vec(body).expect("serialize recorded body"),
    };
    let listener = bind_loopback(0).await.expect("bind recorded target");
    let addr = local_addr(&listener).expect("recorded target addr");
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            let content_type = content_type.clone();
            let body = body.clone();
            tokio::spawn(async move {
                let service = service_fn(move |request: Request<Incoming>| {
                    let content_type = content_type.clone();
                    let body = body.clone();
                    async move {
                        let _ = request.into_body().collect().await;
                        let mut response = Response::builder().status(status);
                        if let Some(value) = &content_type {
                            response = response.header("content-type", value);
                        }
                        Ok::<_, Infallible>(
                            response
                                .body(Full::new(Bytes::from(body)))
                                .expect("recorded response"),
                        )
                    }
                });
                let _ = http1::Builder::new()
                    .serve_connection(TokioIo::new(stream), service)
                    .await;
            });
        }
    });
    addr
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::super::backend::{PROBE, stub_target};
    use crate::common::refused_addr;
    use gobby_daemon::front_door::routes::FAMILIES;
    use gobby_test_support::corpus_v2::{CORPUS_DIR, read_json};
    use serde_json::{Map, json};
    use time::{Duration, OffsetDateTime};
    use time::format_description::well_known::Rfc3339;

    fn at(text: &str) -> OffsetDateTime {
        OffsetDateTime::parse(text, &Rfc3339).expect("rfc3339 test timestamp")
    }

    /// A window that covers every timestamp these tests use.
    fn wide_window() -> Window {
        Window {
            start: OffsetDateTime::UNIX_EPOCH,
            end: OffsetDateTime::UNIX_EPOCH + Duration::days(100_000),
        }
    }

    /// A one-hour window that excludes 2023-11-15.
    fn narrow_window() -> Window {
        Window {
            start: at("2023-11-14T22:00:00Z"),
            end: at("2023-11-14T23:00:00Z"),
        }
    }

    fn http_case(name: &str, family: &str, backend: &str, path: &str) -> Case {
        Case {
            name: name.to_owned(),
            schema_version: 3,
            kind: Kind::Http,
            family: family.to_owned(),
            backend: backend.to_owned(),
            ledger_key: Some(format!("HTTP GET {path}")),
            request: Some(json!({"method": "GET", "path": path, "query": {}, "headers": {}, "body": null})),
            invocation: None,
            response: None,
            db_delta: Vec::new(),
            side_effects: Vec::new(),
            normalizers: Map::new(),
            mask: json!([]),
            waiver: None,
            native_response: None,
        }
    }

    /// A schema 2 file of the v2 corpus, read as it is.
    fn v2_case(file: &str) -> Case {
        let document = read_json(&Path::new(CORPUS_DIR).join(file));
        Case {
            name: document["name"].as_str().expect("name").to_owned(),
            schema_version: 2,
            kind: Kind::Http,
            family: document["family"].as_str().expect("family").to_owned(),
            backend: document["backend"].as_str().expect("backend").to_owned(),
            ledger_key: None,
            request: Some(document["request"].clone()),
            invocation: None,
            response: Some(document["response"].clone()),
            db_delta: Vec::new(),
            side_effects: Vec::new(),
            normalizers: Map::new(),
            mask: document["mask"].clone(),
            waiver: None,
            native_response: None,
        }
    }

    fn observation(response: Value) -> Observation {
        Observation {
            response,
            db_delta: Vec::new(),
            side_effects: Vec::new(),
        }
    }

    fn verdict_lines(home: &Path) -> Vec<Value> {
        std::fs::read_to_string(home.join(DIFFERENTIAL_FILE))
            .unwrap_or_default()
            .lines()
            .map(|line| serde_json::from_str(line).expect("verdict line"))
            .collect()
    }

    #[test]
    fn equal_legs_with_no_deltas_match() {
        let case = http_case("equal", "probe", "up", "/api/probe/native");
        let leg = observation(json!({"status": 200, "body": "same"}));
        assert_eq!(
            compare_legs(&case, &wide_window(), &leg, &leg).expect("compares"),
            Verdict::Match
        );
    }

    #[test]
    fn a_db_delta_difference_is_a_diff_at_its_recorded_pointer() {
        let case = http_case("db", "probe", "up", "/api/probe/native");
        let proxy = Observation {
            response: json!({"status": 200, "body": "same"}),
            db_delta: vec![json!({
                "added": [],
                "removed": [],
                "changed": [{"key": {"id": 1}, "column": "revision", "before": 1, "after": 2}]
            })],
            side_effects: Vec::new(),
        };
        let mut native = proxy.clone();
        native.db_delta[0]["changed"][0]["after"] = json!(3);
        assert_eq!(
            compare_legs(&case, &wide_window(), &proxy, &native).expect("compares"),
            Verdict::Diff {
                pointer: "/db_delta/0/recorded/changed/0/after".to_owned()
            }
        );
    }

    #[test]
    fn a_side_effect_difference_is_a_diff_at_its_recorded_pointer() {
        let case = http_case("effects", "probe", "up", "/api/probe/native");
        let proxy = Observation {
            response: json!({"status": 200, "body": "same"}),
            db_delta: Vec::new(),
            side_effects: vec![json!([{"change": "created", "path": "a.json"}])],
        };
        let mut native = proxy.clone();
        native.side_effects = vec![json!([{"change": "created", "path": "b.json"}])];
        assert_eq!(
            compare_legs(&case, &wide_window(), &proxy, &native).expect("compares"),
            Verdict::Diff {
                pointer: "/side_effects/0/recorded/0/path".to_owned()
            }
        );
    }

    #[test]
    fn a_timestamp_outside_the_window_is_an_error_not_a_verdict() {
        let mut case = http_case("timestamp", "probe", "up", "/api/probe/native");
        case.normalizers
            .insert("/response/body/at".to_owned(), json!("timestamp"));
        let leg = observation(json!({"status": 200, "body": {"at": "2023-11-15T00:00:00Z"}}));
        let error = compare_legs(&case, &narrow_window(), &leg, &leg).expect_err("outside");
        assert!(
            matches!(
                error,
                DifferentialError::Normalize(NormalizeError::OutsideWindow { .. })
            ),
            "{error:?}"
        );
    }

    #[tokio::test]
    async fn a_native_response_mismatch_is_a_diff_at_the_response_pointer() {
        let (target, _seen) = stub_target().await;
        let home = tempfile::tempdir().expect("home");
        let case = http_case("probe_native", "probe", "up", "/api/probe/native");
        let verdict = run_differential(
            &case,
            &[&PROBE],
            target,
            home.path(),
            &wide_window(),
            Extras::default(),
            Extras::default(),
        )
        .await
        .expect("runs");
        assert_eq!(
            verdict,
            Verdict::Diff {
                pointer: "/response/body".to_owned()
            }
        );
        assert_eq!(
            verdict_lines(home.path()),
            vec![json!({
                "case": "probe_native",
                "family": "probe",
                "outcome": "diff",
                "pointer": "/response/body"
            })]
        );
    }

    #[tokio::test]
    async fn health_matches_its_native_answer_when_the_backend_is_up() {
        let case = v2_case("health_ok.json");
        let target = recorded_target(case.response.as_ref().expect("recorded response")).await;
        let home = tempfile::tempdir().expect("home");
        let verdict = run_differential(
            &case,
            FAMILIES,
            target,
            home.path(),
            &wide_window(),
            Extras::default(),
            Extras::default(),
        )
        .await
        .expect("runs");
        assert_eq!(verdict, Verdict::Match);
        assert_eq!(verdict_lines(home.path())[0]["outcome"], "match");
    }

    #[tokio::test]
    async fn health_matches_its_native_answer_when_the_backend_is_down() {
        let case = v2_case("health_backend_down.json");
        let target = refused_addr().await;
        let home = tempfile::tempdir().expect("home");
        let verdict = run_differential(
            &case,
            FAMILIES,
            target,
            home.path(),
            &wide_window(),
            Extras::default(),
            Extras::default(),
        )
        .await
        .expect("runs");
        assert_eq!(verdict, Verdict::Match);
    }

    #[tokio::test]
    async fn a_path_without_a_native_handler_matches_when_both_legs_reach_the_stub() {
        let (target, seen) = stub_target().await;
        let home = tempfile::tempdir().expect("home");
        let case = http_case("probe_shared", "probe", "up", "/api/probe/shared");
        let verdict = run_differential(
            &case,
            &[&PROBE],
            target,
            home.path(),
            &wide_window(),
            Extras::default(),
            Extras::default(),
        )
        .await
        .expect("runs");
        assert_eq!(verdict, Verdict::Match);
        assert_eq!(seen.lock().expect("seen").len(), 2, "both legs reach the stub");
    }

    #[tokio::test]
    async fn a_family_without_a_native_backend_runs_no_leg() {
        let (target, seen) = stub_target().await;
        let home = tempfile::tempdir().expect("home");
        let case = http_case("unknown", "missing-family", "up", "/api/missing-family/x");
        let verdict = run_differential(
            &case,
            FAMILIES,
            target,
            home.path(),
            &wide_window(),
            Extras::default(),
            Extras::default(),
        )
        .await
        .expect("runs");
        assert_eq!(verdict, Verdict::NoNativeBackend);
        assert_eq!(verdict.outcome(), "no-native-backend");
        assert!(seen.lock().expect("seen").is_empty(), "no leg reaches the stub");
        assert_eq!(verdict_lines(home.path())[0]["outcome"], "no-native-backend");
    }

    #[test]
    fn a_verdict_line_names_the_case_family_outcome_and_pointer() {
        let case = http_case("line", "probe", "up", "/api/probe/native");
        assert_eq!(
            record_line(&case, &Verdict::Match),
            json!({"case": "line", "family": "probe", "outcome": "match", "pointer": null})
        );
    }
}
