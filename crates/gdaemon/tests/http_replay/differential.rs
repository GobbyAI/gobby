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
use std::time::Duration;

use bytes::Bytes;
use gobby_core::bootstrap::RouteBackend;
use gobby_core::route_family::RouteFamily;
use gobby_daemon::front_door::FrontDoorState;
use gobby_test_support::corpus_v2::{RESPONSE_HEADER_ALLOWLIST, is_json, request_target};
use gobby_test_support::listeners::{bind_loopback, local_addr};
use http_body_util::{BodyExt, Full};
use hyper::body::Incoming;
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Request, Response};
use hyper_util::rt::TokioIo;
use serde_json::{Map, Value, json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::task::JoinHandle;

use super::backend::{Backends, NO_NATIVE_BACKEND, native_family, start_listener};
use super::compare::{first_difference, normalized_document};
use super::corpus::{Case, Kind};
use super::normalize::{NormalizeError, Window};

/// The longest a leg may take to answer.
const TIMEOUT: Duration = Duration::from_secs(10);

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
    /// The native leg's request reached the proxy target: the native family did not answer the
    /// path itself, so the legs show no native parity and the run is never a match (fix3 V24).
    NativeFallthrough,
}

impl Verdict {
    /// The outcome name written to `differential.jsonl`.
    pub fn outcome(&self) -> &'static str {
        match self {
            Verdict::Match => "match",
            Verdict::Diff { .. } => "diff",
            Verdict::NoNativeBackend => NO_NATIVE_BACKEND,
            Verdict::NativeFallthrough => NATIVE_FALLTHROUGH,
        }
    }
}

/// The outcome name a native leg that reached the proxy target writes.
pub const NATIVE_FALLTHROUGH: &str = "native-fallthrough";

/// What one leg produced: the observed response and the observed `db_delta` and side-effect
/// values, in the recorded shape.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Observation {
    pub response: Value,
    pub db_delta: Vec<Value>,
    pub side_effects: Vec<Value>,
}

/// The values a leg's observer supplies: each `db_delta` entry as `db_delta::table_delta` returns
/// it (already normalized), and each side-effect entry's raw captures.
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
    let expected = normalized_document(
        case,
        &proxy.response,
        &proxy.db_delta,
        &proxy.side_effects,
        window,
    )
    .map_err(DifferentialError::Normalize)?;
    let actual = normalized_document(
        case,
        &native.response,
        &native.db_delta,
        &native.side_effects,
        window,
    )
    .map_err(DifferentialError::Normalize)?;
    Ok(match first_difference(&expected, &actual) {
        Some(pointer) => Verdict::Diff { pointer },
        None => Verdict::Match,
    })
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
    if case.kind != Kind::Http {
        return Err(DifferentialError::Unsupported {
            case: case.name.clone(),
            kind: case.kind,
        });
    }
    let request = case
        .request
        .as_ref()
        .ok_or_else(|| shape(format!("{} has no request", case.name)))?;
    let backends = Backends::new([(case.family.as_str(), backend)]);
    let addr = start_listener(families, &backends, target, home).await;
    let (status, headers, body) = send_request(addr, request).await?;
    Ok(Observation {
        response: json!({"status": status, "headers": headers, "body": body}),
        db_delta: extras.db_delta,
        side_effects: extras.side_effects,
    })
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
    if case.kind != Kind::Http {
        return Err(DifferentialError::Unsupported {
            case: case.name.clone(),
            kind: case.kind,
        });
    }
    let verdict = if native_family(families, &case.family).is_err() {
        Verdict::NoNativeBackend
    } else {
        let proxied = run_leg(case, families, RouteBackend::Proxy, target, home, proxy).await?;
        let native_leg =
            run_leg(case, families, RouteBackend::Native, target, home, native).await?;
        compare_legs(case, window, &proxied, &native_leg)?
    };
    append_line(home, &record_line(case, &verdict))?;
    Ok(verdict)
}

/// The line one verdict appends: `{"case", "family", "outcome", "pointer"}`, where `pointer` is
/// the differing pointer for `diff` and `null` otherwise.
pub fn record_line(case: &Case, verdict: &Verdict) -> Value {
    let pointer = match verdict {
        Verdict::Diff { pointer } => Value::String(pointer.clone()),
        _ => Value::Null,
    };
    json!({
        "case": case.name,
        "family": case.family,
        "outcome": verdict.outcome(),
        "pointer": pointer,
    })
}

/// A stub backend that answers every request with the status, content type and body of
/// `recorded`, a v2 or v3 `response` object. This is test scaffolding, not the behaviour under
/// test, so it is real.
pub async fn recorded_target(recorded: &Value) -> SocketAddr {
    recorded_target_task(recorded).await.0
}

/// As [`recorded_target`], and also returns the task that accepts connections, so a caller
/// that starts many targets can stop each one.
pub async fn recorded_target_task(recorded: &Value) -> (SocketAddr, JoinHandle<()>) {
    let status = u16::try_from(recorded["status"].as_u64().expect("recorded status"))
        .expect("status fits u16");
    let content_type = recorded["headers"]["content-type"]
        .as_str()
        .map(str::to_owned);
    let headers: Vec<(String, String)> = recorded["headers"]
        .as_object()
        .into_iter()
        .flatten()
        .filter(|(name, _)| {
            !matches!(
                name.as_str(),
                "content-length" | "transfer-encoding" | "connection"
            )
        })
        .filter_map(|(name, value)| Some((name.clone(), value.as_str()?.to_owned())))
        .collect();
    let body: Vec<u8> = match &recorded["body"] {
        Value::Null => Vec::new(),
        Value::String(text) if !is_json(content_type.as_deref()) => text.clone().into_bytes(),
        body => serde_json::to_vec(body).expect("serialize recorded body"),
    };
    let listener = bind_loopback(0).await.expect("bind recorded target");
    let addr = local_addr(&listener).expect("recorded target addr");
    let task = tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            let headers = headers.clone();
            let body = body.clone();
            tokio::spawn(async move {
                let service = service_fn(move |request: Request<Incoming>| {
                    let headers = headers.clone();
                    let body = body.clone();
                    async move {
                        let _ = request.into_body().collect().await;
                        let mut response = Response::builder().status(status);
                        for (name, value) in &headers {
                            response = response.header(name.as_str(), value.as_str());
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
    (addr, task)
}

fn shape(detail: impl Into<String>) -> DifferentialError {
    DifferentialError::Shape(detail.into())
}

/// Append one verdict line to `home/differential.jsonl`.
fn append_line(home: &Path, line: &Value) -> Result<(), DifferentialError> {
    use std::io::Write;
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(home.join(DIFFERENTIAL_FILE))
        .map_err(DifferentialError::Io)?;
    writeln!(file, "{line}").map_err(DifferentialError::Io)
}

/// Send one case request over a fresh connection and read the whole answer. The response is
/// `(status, body)`: a JSON body when its content type is JSON, otherwise its text.
pub(super) async fn send_request(
    addr: SocketAddr,
    request: &Value,
) -> Result<(u16, Map<String, Value>, Value), DifferentialError> {
    let method = request["method"]
        .as_str()
        .ok_or_else(|| shape("request has no method"))?;
    let mut head = format!(
        "{method} {} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n",
        request_target(request)
    );
    for (name, value) in request["headers"].as_object().into_iter().flatten() {
        head.push_str(&format!(
            "{name}: {}\r\n",
            value.as_str().unwrap_or_default()
        ));
    }
    let body = match &request["body"] {
        Value::Null => Vec::new(),
        body => {
            head.push_str("content-type: application/json\r\n");
            serde_json::to_vec(body).expect("serialize request body")
        }
    };
    head.push_str(&format!("content-length: {}\r\n\r\n", body.len()));

    let exchange = async {
        let mut stream = TcpStream::connect(addr).await?;
        stream.write_all(head.as_bytes()).await?;
        stream.write_all(&body).await?;
        let mut raw = Vec::new();
        stream.read_to_end(&mut raw).await?;
        Ok::<Vec<u8>, std::io::Error>(raw)
    };
    let raw = tokio::time::timeout(TIMEOUT, exchange)
        .await
        .map_err(|_| shape("the leg did not answer in time"))?
        .map_err(DifferentialError::Io)?;
    parse_response(&raw)
}

fn parse_response(raw: &[u8]) -> Result<(u16, Map<String, Value>, Value), DifferentialError> {
    let split = raw
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .ok_or_else(|| shape("the response has no header end"))?;
    let head =
        std::str::from_utf8(&raw[..split]).map_err(|_| shape("the response head is not UTF-8"))?;
    let status = head
        .split_whitespace()
        .nth(1)
        .and_then(|code| code.parse::<u16>().ok())
        .ok_or_else(|| shape("the response has no status code"))?;
    // A repeated header is one field whose values join with ", ", as httpx reports it.
    let header = |wanted: &str| {
        let values: Vec<&str> = head
            .lines()
            .skip(1)
            .filter_map(|line| {
                let (name, value) = line.split_once(':')?;
                name.trim()
                    .eq_ignore_ascii_case(wanted)
                    .then(|| value.trim())
            })
            .collect();
        (!values.is_empty()).then(|| values.join(", "))
    };
    let content_type = header("content-type");
    let raw_body = &raw[split + 4..];
    let body =
        if header("transfer-encoding").is_some_and(|value| value.eq_ignore_ascii_case("chunked")) {
            dechunk(raw_body)?
        } else {
            raw_body.to_vec()
        };
    let value = if body.is_empty() {
        Value::Null
    } else if is_json(content_type.as_deref()) {
        serde_json::from_slice(&body)
            .map_err(|error| shape(format!("the body is not JSON: {error}")))?
    } else {
        Value::String(String::from_utf8_lossy(&body).into_owned())
    };
    let headers: Map<String, Value> = RESPONSE_HEADER_ALLOWLIST
        .iter()
        .filter_map(|name| Some(((*name).to_owned(), Value::String(header(name)?))))
        .collect();
    Ok((status, headers, value))
}

/// Decode a `chunked` transfer-coding body.
fn dechunk(mut body: &[u8]) -> Result<Vec<u8>, DifferentialError> {
    let mut out = Vec::new();
    loop {
        let line_end = body
            .windows(2)
            .position(|window| window == b"\r\n")
            .ok_or_else(|| shape("a chunk size line is unterminated"))?;
        let size_text = std::str::from_utf8(&body[..line_end])
            .map_err(|_| shape("a chunk size is not UTF-8"))?;
        let size = usize::from_str_radix(size_text.split(';').next().unwrap_or("").trim(), 16)
            .map_err(|_| shape("a chunk size is not hex"))?;
        body = &body[line_end + 2..];
        if size == 0 {
            return Ok(out);
        }
        if body.len() < size + 2 {
            return Err(shape("a chunk is truncated"));
        }
        out.extend_from_slice(&body[..size]);
        body = &body[size + 2..];
    }
}

#[cfg(test)]
mod tests {
    use super::super::backend::{PROBE, stub_target};
    use super::*;
    use crate::common::refused_addr;
    use gobby_daemon::front_door::routes::FAMILIES;
    use gobby_test_support::corpus_v2::{CORPUS_DIR, read_json};
    use serde_json::{Map, json};
    use time::format_description::well_known::Rfc3339;
    use time::{Duration, OffsetDateTime};

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
            request: Some(
                json!({"method": "GET", "path": path, "query": {}, "headers": {}, "body": null}),
            ),
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
        let recorded = case.response.clone().expect("recorded response");
        let target = recorded_target(&recorded).await;
        let home = tempfile::tempdir().expect("home");
        let proxy = run_leg(
            &case,
            FAMILIES,
            RouteBackend::Proxy,
            target,
            home.path(),
            Extras::default(),
        )
        .await
        .expect("proxy leg");
        assert_eq!(
            proxy.response["status"], recorded["status"],
            "the proxy leg answers the recorded status"
        );
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
        // Open item (fix3 V24): the health family's native leg is `proxy::forward` to the proxy
        // target, so this is a fall-through, not native parity. Recorded with FW-S4.
        assert_eq!(verdict, Verdict::NativeFallthrough);
        assert_eq!(verdict_lines(home.path())[0]["outcome"], "native-fallthrough");
    }

    #[tokio::test]
    async fn health_matches_its_native_answer_when_the_backend_is_down() {
        let case = v2_case("health_backend_down.json");
        let recorded = case.response.clone().expect("recorded response");
        let target = refused_addr().await;
        let home = tempfile::tempdir().expect("home");
        let proxy = run_leg(
            &case,
            FAMILIES,
            RouteBackend::Proxy,
            target,
            home.path(),
            Extras::default(),
        )
        .await
        .expect("proxy leg");
        assert_eq!(
            proxy.response["status"], recorded["status"],
            "the proxy leg answers the recorded status"
        );
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
        // Open item (fix3 V24): the native health leg falls through to the proxy target, so the
        // backend-down case is a fall-through too. Recorded with FW-S4.
        assert_eq!(verdict, Verdict::NativeFallthrough);
    }

    #[tokio::test]
    async fn a_path_without_a_native_handler_is_a_fall_through_never_a_match() {
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
        assert_eq!(verdict, Verdict::NativeFallthrough);
        assert_eq!(
            seen.lock().expect("seen").len(),
            1,
            "only the proxied leg reaches the proxy target; the native leg reaches the tripwire"
        );
        assert_eq!(
            verdict_lines(home.path())[0]["outcome"],
            "native-fallthrough"
        );
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
        assert!(
            seen.lock().expect("seen").is_empty(),
            "no leg reaches the stub"
        );
        assert_eq!(
            verdict_lines(home.path())[0]["outcome"],
            "no-native-backend"
        );
    }

    #[test]
    fn a_verdict_line_names_the_case_family_outcome_and_pointer() {
        let case = http_case("line", "probe", "up", "/api/probe/native");
        assert_eq!(
            record_line(&case, &Verdict::Match),
            json!({"case": "line", "family": "probe", "outcome": "match", "pointer": null})
        );
    }

    #[test]
    fn a_duplicated_allowlisted_header_joins_its_values_as_httpx_does() {
        // Golden (Python http_corpus.normalize_response over httpx): retry-after 1 and 2 join as
        // "1, 2"; date is not on the allowlist and is absent.
        let raw = b"HTTP/1.1 200 OK\r\nRetry-After: 1\r\nretry-after: 2\r\ncontent-type: application/json\r\nDate: Tue, 01 Mar 2026 12:00:00 GMT\r\ncontent-length: 2\r\n\r\n{}";
        let (status, headers, body) = parse_response(raw).expect("the response parses");
        assert_eq!(status, 200);
        assert_eq!(headers["retry-after"], json!("1, 2"));
        assert!(!headers.contains_key("date"));
        assert_eq!(body, json!({}));
    }

    #[test]
    fn a_header_only_difference_between_the_legs_is_a_diff_at_the_header() {
        // Golden (Python _first_difference): /response/headers/retry-after.
        let case = http_case("hdr", "health", "up", "/api/health");
        let proxy =
            observation(json!({"status": 200, "headers": {"retry-after": "1"}, "body": null}));
        let native =
            observation(json!({"status": 200, "headers": {"retry-after": "2"}, "body": null}));
        let verdict =
            compare_legs(&case, &wide_window(), &proxy, &native).expect("the legs compare");
        assert_eq!(
            verdict,
            Verdict::Diff {
                pointer: "/response/headers/retry-after".to_owned()
            }
        );
    }

    #[test]
    fn a_missing_allowlisted_header_is_a_diff_at_its_header() {
        // Golden (Python _first_difference): /response/headers/cache-control.
        let case = http_case("hdr", "health", "up", "/api/health");
        let proxy = observation(
            json!({"status": 200, "headers": {"cache-control": "no-store"}, "body": null}),
        );
        let native = observation(json!({"status": 200, "headers": {}, "body": null}));
        let verdict =
            compare_legs(&case, &wide_window(), &proxy, &native).expect("the legs compare");
        assert_eq!(
            verdict,
            Verdict::Diff {
                pointer: "/response/headers/cache-control".to_owned()
            }
        );
    }
}
