//! Replay driver for the v2 and v3 corpus.
//!
//! Each corpus case is observed through its own transport in proxy mode, and the observation is
//! classified by the rule of design-v2 section 7.8. The golden adapters that replay the existing
//! fixture files through this driver live in `adapters`.

mod adapters;
mod backend;
mod compare;
mod corpus;
mod db_delta;
mod differential;
mod normalize;
mod side_effects;
mod soak;
mod summary;

use std::path::{Path, PathBuf};

use corpus::{Case, CorpusError, parse_case};
use differential::{DifferentialError, Observation, Verdict, compare_legs};
use gobby_daemon::front_door::waivers::WaiverManifest;
use normalize::{NormalizeError, Window};
use serde_json::{Value, json};
use time::OffsetDateTime;

/// The classification of one replay (design 7.8): the Rust names of `passed`, `diff`, `waived`
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
    Differential(DifferentialError),
    Waiver(String),
    Io(std::io::Error),
    Shape(String),
}

/// Observe one case through its own transport in proxy mode: the response, the observed
/// `db_delta` (each entry a `db_delta::table_delta` output) and the captured side effects.
/// Test-only observation; the transport is the case's own (HTTP, MCP over HTTP, WS, CLI).
pub async fn observe(
    case: &Case,
    home: &Path,
    window: &Window,
) -> Result<Observation, DriverError> {
    let _ = (case, home, window);
    unimplemented!("not wired: observe")
}

/// Classify one observation of `case` against the case's recording (design 7.8).
pub fn classify(
    case: &Case,
    waivers: &WaiverManifest,
    observed: &Observation,
    window: &Window,
) -> Result<Replay, DriverError> {
    let _ = (case, waivers, observed, window);
    unimplemented!("not wired: classify")
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
        let observed = observe(case, home.path(), &open_window())
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
        let observed = observe(case, home.path(), &open_window())
            .await
            .expect("observe");
        let replay = classify(case, &no_waivers(), &observed, &open_window()).expect("classify");
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
        let observed = observe(case, home.path(), &open_window())
            .await
            .expect("observe");
        let replay = classify(case, &no_waivers(), &observed, &open_window()).expect("classify");
        assert_eq!(replay, Replay::Match, "{} (proxy self-replay)", case.name);
        replayed += 1;
    }
    assert_eq!(replayed, listed, "every manifest case replays through the driver");
}

#[test]
fn cited_kinds_replay_waived() {
    // One synthetic cited case per kind (rule 16: synthetic definitions for behaviour). Its
    // native answer is the observation that is Waived; the recorded Python answer is a Diff.
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
            json!({"status": 200, "headers": {}, "body": "python"}),
            json!({"status": 200, "headers": {}, "body": "native"}),
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
        entries.push(json!({
            "id": id, "surface": surface,
            "behaviour": "The case answers its native contract.", "decision": "#25938",
            "replacement": "The asserted native_response."
        }));
        let mut case = json!({
            "schema_version": 3, "name": format!("cited_{kind}"), "kind": kind,
            "family": "probe", "backend": "up", "credential": "none",
            "ledger_key": surface, "response": python, "native_response": native,
            "waiver": id, "mask": [], "normalizers": {}, "db_delta": [], "side_effects": [],
        });
        if !request.is_null() {
            case["request"] = request;
        }
        cases.push(case);
    }
    let waivers = WaiverManifest::parse(
        &json!({"schema_version": 1, "waivers": entries}).to_string(),
    )
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
                classify(&case, &waivers, &python_observed, &open_window()).expect("classify python"),
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
    let observed = observe(&case, home.path(), &window).await.expect("observe");
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
    let observed = observe(&case, home.path(), &window).await.expect("observe");

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
