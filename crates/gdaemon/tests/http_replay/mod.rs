//! Replay driver for the v2 and v3 corpus.
//!
//! Each corpus case is observed through its own transport in proxy mode, and the observation is
//! classified against the case's recording. The golden adapters that replay the existing
//! fixture files through this driver live in `adapters`.
//!
//! Proxy self-replay is the fixture-validity gate. The observation is the recording itself, with
//! each normalized token re-expanded to a raw value that the case's normalizers map back to the
//! same token. The transport (the front door, the WS route, the CLI process) is real; the
//! database and `pg_notify` are not observed in-process, so their observed values come from the
//! recording (the full-daemon leg owns those observers, a found line).

mod adapters;
mod backend;
mod compare;
mod corpus;
mod db_delta;
mod differential;
#[cfg(test)]
mod driver_tests;
mod normalize;
mod side_effects;
mod soak;
mod summary;

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

use backend::{Backends, WsHop, start_listener, start_ws_hop};
use compare::{comparison_document, first_difference, normalized_document};
use corpus::{Case, CorpusError, Kind, parse_case, recorded_db_delta, recorded_side_effects};
use db_delta::{DeltaError, Row, table_delta};
use differential::{DifferentialError, Observation, Verdict, compare_legs, send_request};
use gobby_core::bootstrap::RouteBackend;
use gobby_daemon::front_door::routes::FAMILIES;
use gobby_daemon::front_door::waivers::{CaseSurface, WaiverManifest, is_native_extension};
use normalize::{NormalizeError, Window, normalize_document};
use serde_json::{Map, Value, json};
use time::OffsetDateTime;
use time::format_description::well_known::Rfc3339;

use crate::common::{TIMEOUT, frame, read_exact_into, start_front_door, ws_backend, ws_client};

/// A final, unmasked text frame: the frame header of one text payload.
const TEXT_FIN: u8 = 0x81;

/// How long a WS observation waits for more bytes after the expected reply before it stops.
const QUIET: Duration = Duration::from_millis(200);

/// The classification of one replay: the Rust names of `passed`, `diff`, `waived`
/// and `native-extension`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Replay {
    Match,
    Diff { pointer: String },
    Waived { waiver: String },
    NativeExtension { waiver: String },
}

/// A replay that cannot give an outcome.
#[derive(Debug)]
pub enum DriverError {
    Corpus(CorpusError),
    Normalize(NormalizeError),
    Delta(DeltaError),
    Differential(DifferentialError),
    Waiver(String),
    Io(std::io::Error),
    Shape(String),
}

impl std::fmt::Display for DriverError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Corpus(error) => write!(f, "corpus: {error}"),
            Self::Normalize(error) => write!(f, "normalize: {error}"),
            Self::Delta(error) => write!(f, "db_delta: {error:?}"),
            Self::Differential(error) => write!(f, "differential: {error:?}"),
            Self::Waiver(detail) => write!(f, "waiver: {detail}"),
            Self::Io(error) => write!(f, "io: {error}"),
            Self::Shape(detail) => write!(f, "shape: {detail}"),
        }
    }
}

/// The transports one observation runs through. [`Transports::recorded`] is the self-replay: the
/// repo's `gobby` entry point and the recording's own WS reply frames, with no hop.
#[derive(Debug, Clone)]
pub struct Transports {
    /// The `gobby` entry point a CLI case runs.
    pub gobby: PathBuf,
    /// The text frames the WS stub sends in place of the recording's reply, when set.
    pub ws_reply: Option<Vec<String>>,
    /// A relay between the front door and the WS stub, when set.
    pub ws_hop: Option<WsHop>,
}

impl Transports {
    /// The self-replay transports: the repo's `gobby` entry point, the recorded WS reply, no hop.
    pub fn recorded() -> Self {
        Self {
            gobby: PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../.venv/bin/gobby"),
            ws_reply: None,
            ws_hop: None,
        }
    }
}

/// Observe one case through its own transport in proxy mode: the response, the observed
/// `db_delta` (each entry a `db_delta::table_delta` output) and the captured side effects.
pub async fn observe(
    case: &Case,
    home: &Path,
    window: &Window,
    transports: &Transports,
) -> Result<Observation, DriverError> {
    match case.kind {
        Kind::Http => {
            let request = case
                .request
                .clone()
                .ok_or_else(|| shape(format!("{} has no request", case.name)))?;
            let recording = recorded_response(case)?;
            observe_request(case, &request, recording, home, window).await
        }
        Kind::Mcp => {
            let request = mcp_request(case)?;
            // The MCP recording is the JSON-RPC body alone, served with a 200 JSON answer.
            let recording = json!({
                "status": 200,
                "headers": {"content-type": "application/json"},
                "body": recorded_response(case)?,
            });
            observe_request(case, &request, recording, home, window).await
        }
        Kind::Ws => observe_ws(case, window, transports).await,
        Kind::Cli => observe_cli(case, home, &transports.gobby),
    }
}

/// Classify one observation of `case` against the case's recording. The recorded
/// side and the observed side both carry the case's masks, so a masked value is equal on both.
pub fn classify(
    case: &Case,
    waivers: &WaiverManifest,
    observed: &Observation,
    window: &Window,
) -> Result<Replay, DriverError> {
    let recorded_db = recorded_db_delta(case).map_err(DriverError::Corpus)?;
    let recorded_side = recorded_side_effects(case).map_err(DriverError::Corpus)?;
    let (expected_response, waiver) = match case.waiver.as_deref() {
        None => (recorded_response(case)?, None),
        Some(id) => {
            waivers
                .cite(id, surface_of(case)?)
                .map_err(|error| DriverError::Waiver(error.to_string()))?;
            let native = case.native_response.clone().ok_or_else(|| {
                shape(format!(
                    "{}: a waived case has no native_response",
                    case.name
                ))
            })?;
            (native, Some(id))
        }
    };
    let expected = masked_recording(
        case,
        &decoded_frames(case, expected_response),
        &recorded_db,
        &recorded_side,
        window,
    )?;
    let actual = normalized_document(
        case,
        &decoded_frames(case, observed.response.clone()),
        &observed.db_delta,
        &observed.side_effects,
        window,
    )
    .map_err(DriverError::Normalize)?;
    Ok(match first_difference(&expected, &actual) {
        Some(pointer) => Replay::Diff { pointer },
        None => match waiver {
            None => Replay::Match,
            Some(id) if is_native_extension(id) => Replay::NativeExtension {
                waiver: id.to_owned(),
            },
            Some(id) => Replay::Waived {
                waiver: id.to_owned(),
            },
        },
    })
}

/// The WS frames of a response decoded as JSON, as the Python corpus compares them: a frame that is
/// JSON text becomes its value, and any other frame stays text. Other kinds are unchanged.
fn decoded_frames(case: &Case, response: Value) -> Value {
    if case.kind != Kind::Ws {
        return response;
    }
    match response {
        Value::Array(frames) => Value::Array(
            frames
                .into_iter()
                .map(|frame| match frame {
                    Value::String(text) => {
                        serde_json::from_str(&text).unwrap_or(Value::String(text))
                    }
                    other => other,
                })
                .collect(),
        ),
        other => other,
    }
}

fn shape(detail: impl Into<String>) -> DriverError {
    DriverError::Shape(detail.into())
}

fn recorded_response(case: &Case) -> Result<Value, DriverError> {
    case.response
        .clone()
        .ok_or_else(|| shape(format!("{}: no recorded response", case.name)))
}

/// The surface a case presents to the waiver matcher.
fn surface_of(case: &Case) -> Result<CaseSurface<'_>, DriverError> {
    match case.kind {
        Kind::Http => {
            let request = case
                .request
                .as_ref()
                .ok_or_else(|| shape(format!("{} has no request", case.name)))?;
            Ok(CaseSurface::Http {
                method: request["method"].as_str().unwrap_or_default(),
                path: request["path"].as_str().unwrap_or_default(),
            })
        }
        _ => Ok(CaseSurface::Keyed {
            key: case
                .ledger_key
                .as_deref()
                .ok_or_else(|| shape(format!("{} has no ledger key", case.name)))?,
        }),
    }
}

/// The comparison document of the recording, with the case's masks applied and no rules: the
/// recorded side is compared as stored, with masks on both sides (Python `_comparison`).
fn masked_recording(
    case: &Case,
    response: &Value,
    db_delta: &[Value],
    side_effects: &[Value],
    window: &Window,
) -> Result<Value, DriverError> {
    let document = comparison_document(response, db_delta, side_effects);
    normalize_document(&document, &case.mask, &[], window).map_err(DriverError::Normalize)
}

/// An HTTP-shaped observation: the request goes through a listener whose family is proxied to a
/// stub that answers the recording (or to a refused address for a `down` case).
async fn observe_request(
    case: &Case,
    request: &Value,
    recording: Value,
    home: &Path,
    window: &Window,
) -> Result<Observation, DriverError> {
    let target = if case.backend == "up" {
        differential::recorded_target(&expanded(recording, window)).await
    } else {
        crate::common::refused_addr().await
    };
    let backends = Backends::new([(case.family.as_str(), RouteBackend::Proxy)]);
    let addr = start_listener(FAMILIES, &backends, target, home).await;
    let (status, headers, body) = send_request(addr, request)
        .await
        .map_err(DriverError::Differential)?;
    // An MCP recording is the JSON-RPC body alone, so its observation is that body; an HTTP
    // recording is the status, the allowlisted headers and the body.
    let response = match case.kind {
        Kind::Mcp => body,
        _ => json!({"status": status, "headers": headers, "body": body}),
    };
    Ok(Observation {
        response,
        db_delta: self_db_delta(case, window)?,
        side_effects: self_side_effects(case, window)?,
    })
}

/// The MCP request of a case: `POST /api/mcp/<server>/tools/<tool>` with the arguments as body.
fn mcp_request(case: &Case) -> Result<Value, DriverError> {
    let invocation = case
        .invocation
        .as_ref()
        .ok_or_else(|| shape(format!("{} has no invocation", case.name)))?;
    let server = invocation["server"].as_str().unwrap_or_default();
    let tool = invocation["tool"].as_str().unwrap_or_default();
    Ok(json!({
        "method": "POST",
        "path": format!("/api/mcp/{server}/tools/{tool}"),
        "query": {},
        "headers": {},
        "body": invocation["arguments"].clone(),
    }))
}

/// A WS observation: the invocation frames go through the front door's terminal route to a stub
/// that answers the recorded reply frames.
async fn observe_ws(
    case: &Case,
    window: &Window,
    transports: &Transports,
) -> Result<Observation, DriverError> {
    let invocation = case
        .invocation
        .as_ref()
        .ok_or_else(|| shape(format!("{} has no invocation", case.name)))?;
    let sent: Vec<String> = text_list(&invocation["frames"], &case.name)?;
    let replies: Vec<String> = match &transports.ws_reply {
        Some(frames) => frames.clone(),
        None => expanded_frames(recorded_response(case)?, window, &case.name)?,
    };
    let client: Vec<u8> = sent
        .iter()
        .flat_map(|payload| frame(TEXT_FIN, payload.as_bytes(), true))
        .collect();
    let reply: Vec<u8> = replies
        .iter()
        .flat_map(|payload| frame(TEXT_FIN, payload.as_bytes(), false))
        .collect();
    let (backend, captured) = ws_backend(client.len(), reply.clone()).await;
    let upstream = match &transports.ws_hop {
        Some(hop) => start_ws_hop(backend, hop).await,
        None => backend,
    };
    let door = start_front_door(upstream).await;
    let (mut stream, head, mut received) = ws_client(door, "/api/terminals/ws").await;
    if !head.starts_with("HTTP/1.1 101") {
        return Err(shape(format!("the upgrade was refused: {head}")));
    }
    tokio::io::AsyncWriteExt::write_all(&mut stream, &client)
        .await
        .map_err(DriverError::Io)?;
    tokio::time::timeout(
        TIMEOUT,
        read_exact_into(&mut stream, &mut received, reply.len()),
    )
    .await
    .map_err(|_| shape("the WS reply timed out"))?;
    read_until_quiet(&mut stream, &mut received).await?;
    let capture = tokio::time::timeout(TIMEOUT, captured)
        .await
        .map_err(|_| shape("the WS backend did not report its client leg"))?
        .map_err(|_| shape("the WS backend's capture was dropped"))?;
    // The backend must receive exactly the invocation's frames: a front door that rewrites them
    // is a failed replay, not a match.
    if capture.received != client {
        return Err(shape("client leg changed the invocation frames"));
    }
    Ok(Observation {
        response: json!(decode_text_frames(&received)?),
        db_delta: self_db_delta(case, window)?,
        side_effects: self_side_effects(case, window)?,
    })
}

/// Reads what else arrives on `stream` after the expected bytes: until the peer closes, or until it
/// stays quiet for [`QUIET`], bounded by [`TIMEOUT`]. A surplus frame is then observed,
/// not dropped.
async fn read_until_quiet<S: tokio::io::AsyncRead + Unpin>(
    stream: &mut S,
    buffer: &mut Vec<u8>,
) -> Result<(), DriverError> {
    let deadline = tokio::time::Instant::now() + TIMEOUT;
    loop {
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        let mut chunk = [0_u8; 4096];
        let read = tokio::time::timeout(
            QUIET.min(remaining),
            tokio::io::AsyncReadExt::read(stream, &mut chunk),
        )
        .await;
        match read {
            Err(_) | Ok(Ok(0)) => return Ok(()),
            Ok(Ok(read)) => buffer.extend_from_slice(&chunk[..read]),
            Ok(Err(error)) => return Err(DriverError::Io(error)),
        }
    }
}

fn text_list(value: &Value, case: &str) -> Result<Vec<String>, DriverError> {
    value
        .as_array()
        .ok_or_else(|| shape(format!("{case}: expected a list of text frames")))?
        .iter()
        .map(|item| {
            item.as_str()
                .map(str::to_owned)
                .ok_or_else(|| shape(format!("{case}: a frame is not text")))
        })
        .collect()
}

/// The recorded WS frames with tokens expanded inside each JSON frame, as text again. A token inside
/// a frame sits in a JSON value (`"client_id":"@uuid:1@"`), so the frame is parsed, expanded and
/// serialized; a frame that is not JSON is expanded as a whole string.
fn expanded_frames(
    response: Value,
    window: &Window,
    case: &str,
) -> Result<Vec<String>, DriverError> {
    Ok(text_list(&response, case)?
        .into_iter()
        .map(|text| match serde_json::from_str::<Value>(&text) {
            Ok(value) => expanded(value, window).to_string(),
            Err(_) => expand_token(&text, window, &mut 0),
        })
        .collect())
}

/// The text payloads of the unmasked server frames in `bytes`.
fn decode_text_frames(mut bytes: &[u8]) -> Result<Vec<String>, DriverError> {
    let mut payloads = Vec::new();
    while !bytes.is_empty() {
        if bytes.len() < 2 || bytes[0] != TEXT_FIN {
            return Err(shape("a server frame is not a final text frame"));
        }
        let (length, header) = match bytes[1] & 0x7f {
            short @ 0..=125 => (usize::from(short), 2),
            126 if bytes.len() >= 4 => (usize::from(u16::from_be_bytes([bytes[2], bytes[3]])), 4),
            127 if bytes.len() >= 10 => {
                let length = u64::from_be_bytes(bytes[2..10].try_into().expect("8 bytes"));
                (
                    usize::try_from(length).map_err(|_| shape("frame too long"))?,
                    10,
                )
            }
            _ => return Err(shape("a server frame header is truncated")),
        };
        if bytes.len() < header + length {
            return Err(shape("a server frame payload is truncated"));
        }
        let text = std::str::from_utf8(&bytes[header..header + length])
            .map_err(|_| shape("a server frame payload is not UTF-8"))?;
        payloads.push(text.to_owned());
        bytes = &bytes[header + length..];
    }
    Ok(payloads)
}

/// A CLI observation: the sealed environment of `composer_proof.py` (no database URL, a temporary
/// home), the repo's `gobby` entry point, and the recorded exit, stdout and stderr.
fn observe_cli(case: &Case, home: &Path, binary: &Path) -> Result<Observation, DriverError> {
    let invocation = case
        .invocation
        .as_ref()
        .ok_or_else(|| shape(format!("{} has no invocation", case.name)))?;
    let argv = text_list(&invocation["argv"], &case.name)?;
    let stdin = invocation["stdin"].as_str().map(str::to_owned);
    let mut command = Command::new(binary);
    command
        .args(&argv)
        .env_clear()
        .env("HOME", home)
        .env("GOBBY_HOME", home)
        .env("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        .stdin(if stdin.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn().map_err(DriverError::Io)?;
    if let (Some(text), Some(mut pipe)) = (stdin, child.stdin.take()) {
        std::io::Write::write_all(&mut pipe, text.as_bytes()).map_err(DriverError::Io)?;
    }
    let output = child.wait_with_output().map_err(DriverError::Io)?;
    Ok(Observation {
        response: json!({
            "exit": output.status.code().unwrap_or(-1),
            "stdout": String::from_utf8_lossy(&output.stdout),
            "stderr": String::from_utf8_lossy(&output.stderr),
        }),
        db_delta: Vec::new(),
        side_effects: Vec::new(),
    })
}

/// The observed `db_delta` of a self-replay: each entry's recorded rows, rebuilt as the before and
/// after snapshots, run through `table_delta` with the entry's own normalizers.
fn self_db_delta(case: &Case, window: &Window) -> Result<Vec<Value>, DriverError> {
    let recorded = recorded_db_delta(case).map_err(DriverError::Corpus)?;
    case.db_delta
        .iter()
        .zip(recorded)
        .map(|(entry, recorded)| {
            let (before, after) = rows_of_recording(&expanded(recorded, window))?;
            table_delta(&before, &after, entry, window).map_err(DriverError::Delta)
        })
        .collect()
}

/// The before and after snapshots a recorded `{added, removed, changed}` value came from. A
/// changed column is set on both sides of its key's row; other columns are absent on both sides.
fn rows_of_recording(recorded: &Value) -> Result<(Vec<Row>, Vec<Row>), DriverError> {
    let row_of = |value: &Value| {
        value
            .as_object()
            .cloned()
            .ok_or_else(|| shape("a recorded row is not an object"))
    };
    let mut before = Vec::new();
    let mut after = Vec::new();
    for value in recorded["added"].as_array().into_iter().flatten() {
        after.push(row_of(value)?);
    }
    for value in recorded["removed"].as_array().into_iter().flatten() {
        before.push(row_of(value)?);
    }
    let mut pairs: BTreeMap<String, (Row, Row)> = BTreeMap::new();
    for change in recorded["changed"].as_array().into_iter().flatten() {
        let key = change["key"]
            .as_object()
            .ok_or_else(|| shape("a recorded change has no key object"))?;
        let column = change["column"]
            .as_str()
            .ok_or_else(|| shape("a recorded change has no column"))?;
        let identity = Value::Object(key.clone()).to_string();
        let (left, right) = pairs
            .entry(identity)
            .or_insert_with(|| (key.clone(), key.clone()));
        left.insert(column.to_owned(), change["before"].clone());
        right.insert(column.to_owned(), change["after"].clone());
    }
    for (left, right) in pairs.into_values() {
        before.push(left);
        after.push(right);
    }
    Ok((before, after))
}

/// The observed side effects of a self-replay: each entry's recorded captures, expanded.
fn self_side_effects(case: &Case, window: &Window) -> Result<Vec<Value>, DriverError> {
    let recorded = recorded_side_effects(case).map_err(DriverError::Corpus)?;
    Ok(recorded
        .into_iter()
        .map(|captures| expanded(captures, window))
        .collect())
}

/// The recording with each normalized token re-expanded to a raw value that the case's normalizers
/// map back to the same token: `@uuid:N@` to a canonical UUID, `@seq@` to the next integer text in
/// the value's order, and `@ts@` to the window start. Every other value, masks included, is kept.
fn expanded(value: Value, window: &Window) -> Value {
    let mut sequence = 0_u64;
    expand_value(value, window, &mut sequence)
}

fn expand_value(value: Value, window: &Window, sequence: &mut u64) -> Value {
    match value {
        Value::String(text) => Value::String(expand_token(&text, window, sequence)),
        Value::Array(items) => Value::Array(
            items
                .into_iter()
                .map(|item| expand_value(item, window, sequence))
                .collect(),
        ),
        Value::Object(map) => Value::Object(
            map.into_iter()
                .map(|(key, item)| (key, expand_value(item, window, sequence)))
                .collect::<Map<String, Value>>(),
        ),
        other => other,
    }
}

fn expand_token(text: &str, window: &Window, sequence: &mut u64) -> String {
    if text == "@seq@" {
        *sequence += 1;
        return sequence.to_string();
    }
    if text == "@ts@" {
        return window
            .start
            .format(&Rfc3339)
            .expect("the window start formats as RFC 3339");
    }
    let number = text
        .strip_prefix("@uuid:")
        .and_then(|rest| rest.strip_suffix('@'))
        .and_then(|digits| digits.parse::<u64>().ok());
    match number {
        Some(number) => format!("00000000-0000-4000-8000-{number:012x}"),
        None => text.to_owned(),
    }
}

fn contracts_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/contracts")
}

fn v2_manifest() -> PathBuf {
    contracts_dir().join("http/manifest.json")
}

fn v3_manifest() -> PathBuf {
    contracts_dir().join("v3/manifest.json")
}

fn read_value(path: &Path) -> Value {
    serde_json::from_slice(&std::fs::read(path).expect("read json")).expect("parse json")
}

fn manifest_len(path: &Path) -> usize {
    read_value(path)["cases"]
        .as_array()
        .expect("manifest cases")
        .len()
}

/// A window that holds every timestamp of the recorded corpus.
fn open_window() -> Window {
    Window {
        start: OffsetDateTime::UNIX_EPOCH,
        end: OffsetDateTime::from_unix_timestamp(4_102_444_800).expect("year 2100"),
    }
}

fn no_waivers() -> WaiverManifest {
    WaiverManifest::embedded().expect("embedded waiver manifest")
}

fn v3_case(file: &str) -> (Value, Case) {
    let value = read_value(&contracts_dir().join("v3").join(file));
    let case = parse_case(&value).expect("v3 case");
    (value, case)
}

#[tokio::test]
async fn v2_down_cases_in_process() {
    let cases = corpus::load_manifest(&v2_manifest()).expect("v2 manifest");
    let down: Vec<&Case> = cases.iter().filter(|case| case.backend == "down").collect();
    assert_eq!(down.len(), 3, "the v2 corpus has three backend-down cases");
    let home = tempfile::tempdir().expect("home");
    for case in down {
        let observed = observe(case, home.path(), &open_window(), &Transports::recorded())
            .await
            .expect("observe");
        let replay = classify(case, &no_waivers(), &observed, &open_window()).expect("classify");
        assert_eq!(replay, Replay::Match, "{}", case.name);
    }
}

#[tokio::test]
async fn v2_16_proxy_driver_replay() {
    let cases = corpus::load_manifest(&v2_manifest()).expect("v2 manifest");
    assert_eq!(cases.len(), manifest_len(&v2_manifest()));
    assert_eq!(cases.len(), 16, "the v2 corpus has sixteen cases");
    let home = tempfile::tempdir().expect("home");
    for case in &cases {
        let observed = observe(case, home.path(), &open_window(), &Transports::recorded())
            .await
            .expect("observe");
        let replay = classify(case, &no_waivers(), &observed, &open_window()).expect("classify");
        if case.name == "runtime_handshake_challenge" {
            // Known gap (report item): the handshake is gdaemon-authored with no Python counterpart,
            // and the proxy-mode front door answers it natively instead of the recorded answer. Its
            // waiver (`runtime.handshake.native-extension`) lives in docs/contracts/parity-waivers.json,
            // which this driver reads but does not own, so the gap is pinned here until that waiver
            // is registered and the case is classified as a native extension.
            assert_eq!(
                replay,
                Replay::Diff {
                    pointer: "/response/status".to_owned()
                },
                "{} (proxy mode, known gap)",
                case.name
            );
            assert_ne!(observed.response["status"], json!(200), "{}", case.name);
            continue;
        }
        assert_eq!(replay, Replay::Match, "{} (proxy mode)", case.name);
    }
}

#[tokio::test]
async fn v3_cases_proxy_self_replay() {
    let cases = corpus::load_manifest(&v3_manifest()).expect("v3 manifest");
    let listed = manifest_len(&v3_manifest());
    let home = tempfile::tempdir().expect("home");
    let mut replayed = 0;
    for case in &cases {
        let observed = observe(case, home.path(), &open_window(), &Transports::recorded())
            .await
            .expect("observe");
        let replay = classify(case, &no_waivers(), &observed, &open_window()).expect("classify");
        assert_eq!(replay, Replay::Match, "{} (proxy self-replay)", case.name);
        replayed += 1;
    }
    assert_eq!(
        replayed, listed,
        "every manifest case replays through the driver"
    );
}

#[test]
fn cited_kinds_replay_waived() {
    // One synthetic cited case per kind (rule 16: synthetic definitions for behaviour). Its
    // native answer is the observation that is Waived; the recorded Python answer is a Diff.
    let mcp_response = |text: &str| json!({"jsonrpc": "2.0", "id": 1, "result": {"text": text}});
    let kinds = [
        (
            "http",
            "HTTP GET /api/probe/shared",
            json!({"method": "GET", "path": "/api/probe/shared", "query": {}, "headers": {}, "body": null}),
            json!({"status": 200, "headers": {}, "body": "python"}),
            json!({"status": 200, "headers": {}, "body": "native"}),
        ),
        (
            "mcp",
            "MCP HTTP gobby:list_tools",
            json!(null),
            mcp_response("python"),
            mcp_response("native"),
        ),
        (
            "ws",
            "WS message terminal_list",
            json!(null),
            json!(["{\"type\":\"python\"}"]),
            json!(["{\"type\":\"native\"}"]),
        ),
        (
            "cli",
            "CLI gobby schema",
            json!(null),
            json!({"exit": 0, "stdout": "python", "stderr": ""}),
            json!({"exit": 0, "stdout": "native", "stderr": ""}),
        ),
    ];
    let mut entries = Vec::new();
    let mut cases = Vec::new();
    for (kind, surface, request, python, native) in kinds {
        let id = format!("probe.{kind}.waived");
        let invocation = match kind {
            "mcp" => json!({
                "transport": "http", "server": "gobby", "tool": "list_tools", "arguments": {}
            }),
            "ws" => json!({"frames": ["{\"type\":\"terminal_list\"}"]}),
            "cli" => json!({"argv": ["schema", "--help"]}),
            _ => json!(null),
        };
        entries.push(json!({
            "id": id, "surface": surface,
            "behaviour": "The case answers its native contract.", "decision": "#25938",
            "replacement": "The asserted native_response."
        }));
        // Keys in the order the loader requires: request or invocation, then
        // response, the declared entries, mask, waiver and native_response.
        let mut case = serde_json::Map::new();
        for (key, value) in [
            ("schema_version", json!(3)),
            ("name", json!(format!("cited_{kind}"))),
            ("kind", json!(kind)),
            ("family", json!("probe")),
            ("backend", json!("up")),
            ("credential", json!("none")),
            ("ledger_key", json!(surface)),
        ] {
            case.insert(key.to_owned(), value);
        }
        if !request.is_null() {
            case.insert("request".to_owned(), request);
        }
        if !invocation.is_null() {
            case.insert("invocation".to_owned(), invocation);
        }
        for (key, value) in [
            ("response", python.clone()),
            ("db_delta", json!([])),
            ("side_effects", json!([])),
            ("normalizers", json!({})),
            ("mask", json!([])),
            ("waiver", json!(id)),
            ("native_response", native.clone()),
        ] {
            case.insert(key.to_owned(), value);
        }
        cases.push(Value::Object(case));
    }
    let waivers =
        WaiverManifest::parse(&json!({"schema_version": 1, "waivers": entries}).to_string())
            .expect("synthetic waiver manifest");
    for value in cases {
        let case = parse_case(&value).expect("cited case");
        let native_observed = Observation {
            response: value["native_response"].clone(),
            db_delta: Vec::new(),
            side_effects: Vec::new(),
        };
        let python_observed = Observation {
            response: value["response"].clone(),
            db_delta: Vec::new(),
            side_effects: Vec::new(),
        };
        let waiver = value["waiver"].as_str().expect("waiver id").to_owned();
        assert_eq!(
            classify(&case, &waivers, &native_observed, &open_window()).expect("classify native"),
            Replay::Waived {
                waiver: waiver.clone()
            },
            "{} on its native answer",
            case.name
        );
        assert!(
            matches!(
                classify(&case, &waivers, &python_observed, &open_window())
                    .expect("classify python"),
                Replay::Diff { .. }
            ),
            "{} on the Python answer is a diff",
            case.name
        );
    }
}

#[tokio::test]
async fn one_changed_db_delta_column_is_a_diff_on_self_replay_and_differential() {
    let (value, case) = v3_case("config_values_patch.json");
    let home = tempfile::tempdir().expect("home");
    let window = open_window();
    let observed = observe(&case, home.path(), &window, &Transports::recorded())
        .await
        .expect("observe");
    assert_eq!(
        classify(&case, &no_waivers(), &observed, &window).expect("classify"),
        Replay::Match,
        "the recording replays unchanged"
    );

    // Self-replay: the recording changes one column of one changed row.
    let mut changed = value.clone();
    changed["db_delta"][0]["recorded"]["changed"][0]["after"] = json!(3);
    let changed_case = parse_case(&changed).expect("changed case");
    assert_eq!(
        classify(&changed_case, &no_waivers(), &observed, &window).expect("classify"),
        Replay::Diff {
            pointer: "/db_delta/0/recorded/changed/0/after".to_owned()
        }
    );

    // Differential: the native leg observes the changed column.
    let mut native = observed.clone();
    native.db_delta[0]["changed"][0]["after"] = json!(3);
    let verdict = compare_legs(&case, &window, &observed, &native).expect("compare legs");
    assert!(
        matches!(
            &verdict,
            Verdict::Diff { pointer } if pointer == "/db_delta/0/recorded/changed/0/after"
        ),
        "{verdict:?}"
    );
}

#[tokio::test]
async fn one_changed_side_effect_is_a_diff_on_self_replay_and_differential() {
    let (value, case) = v3_case("config_values_patch.json");
    let home = tempfile::tempdir().expect("home");
    let window = open_window();
    let observed = observe(&case, home.path(), &window, &Transports::recorded())
        .await
        .expect("observe");

    // Self-replay: the recording names another channel for its one capture.
    let mut changed = value.clone();
    changed["side_effects"][0]["recorded"][0]["channel"] = json!("other_channel");
    let changed_case = parse_case(&changed).expect("changed case");
    assert_eq!(
        classify(&changed_case, &no_waivers(), &observed, &window).expect("classify"),
        Replay::Diff {
            pointer: "/side_effects/0/recorded/0/channel".to_owned()
        }
    );

    // Differential: the native leg captured the other channel.
    let mut native = observed.clone();
    native.side_effects[0][0]["channel"] = json!("other_channel");
    let verdict = compare_legs(&case, &window, &observed, &native).expect("compare legs");
    assert!(
        matches!(
            &verdict,
            Verdict::Diff { pointer } if pointer == "/side_effects/0/recorded/0/channel"
        ),
        "{verdict:?}"
    );
}
