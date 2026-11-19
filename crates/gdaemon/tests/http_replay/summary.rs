// Compare-report summary of a soak run (design-v2 section 7.10, H12).
//
// The summary reads the run's front-door compare log through the shipped `gdaemon
// compare-report` command, so the pass verdict is the command's own. A run passes when the
// report passes and its recorded counts equal the expected counts per family and outcome.
// The summary JSON is written to `soak-summary.json` in the run home.

use std::path::Path;

use gobby_core::route_family::RouteFamily;
use gobby_daemon::front_door::FrontDoorState;
use gobby_daemon::front_door::compare::Outcome;
use gobby_daemon::front_door::routes::FAMILIES;
use gobby_test_support::corpus_v2::{load_all_cases, load_manifest};
use serde_json::{Map, Value, json};

use super::backend::{PROBE, read_records};
use super::corpus::{Case, Kind, parse_case};
use super::soak::{SIDECAR_FILE, Soak, SoakRun};

/// The summary file the run home receives.
pub const SUMMARY_FILE: &str = "soak-summary.json";

/// Why the summary could not be made. A failing run is not an error here: it is `passed: false`.
#[derive(Debug)]
pub enum SummaryError {
    /// A file could not be read or written.
    Io(String),
    /// `gdaemon compare-report` printed no report that parses.
    Report(String),
}

impl std::fmt::Display for SummaryError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(message) => write!(f, "summary file: {message}"),
            Self::Report(message) => write!(f, "compare-report: {message}"),
        }
    }
}

impl std::error::Error for SummaryError {}

/// The result of `gdaemon compare-report` over one home.
#[derive(Debug, Clone, PartialEq)]
pub struct CompareReport {
    /// Whether the command exited zero, which is its pass verdict.
    pub success: bool,
    /// The report the command printed.
    pub report: Value,
}

/// One summarized run: the summary JSON and whether the run passes.
#[derive(Debug, Clone, PartialEq)]
pub struct Summary {
    pub passed: bool,
    pub json: Value,
}

/// Runs `gdaemon compare-report --since <since>` with `GOBBY_HOME` set to `home`.
pub fn compare_report(home: &Path, since: i64) -> Result<CompareReport, SummaryError> {
    let _ = (home, since);
    unimplemented!("not wired: summary::compare_report")
}

/// Summarizes a finished soak run and writes the summary to `<run home>/soak-summary.json`.
pub fn summarize(run: &SoakRun) -> Result<Summary, SummaryError> {
    let _ = run;
    unimplemented!("not wired: summary::summarize")
}

/// Every record that is not a match, with the iteration and case that made it. The run log
/// holds the records of the iterations in order, and the sidecar gives each iteration's order.
fn failures(run: &SoakRun) -> Result<Vec<Value>, SummaryError> {
    let path = run.home.join(SIDECAR_FILE);
    let text = std::fs::read_to_string(&path)
        .map_err(|error| SummaryError::Io(format!("{}: {error}", path.display())))?;
    let records = read_records(&run.home);
    let mut failures = Vec::new();
    let mut position = 0;
    for line in text.lines() {
        let line: Value = serde_json::from_str(line)
            .map_err(|error| SummaryError::Io(format!("{}: {error}", path.display())))?;
        let order = line["order"].as_array().cloned().unwrap_or_default();
        for name in order {
            if let Some(record) = records.get(position)
                && record.outcome != Outcome::Match
            {
                failures.push(json!({
                    "i": line["i"],
                    "case": name,
                    "family": record.family,
                    "method": record.method,
                    "path": record.path,
                    "outcome": record.outcome,
                    "detail": record.detail,
                }));
            }
            position += 1;
        }
    }
    Ok(failures)
}

fn corpus() -> Vec<Case> {
    let manifest = load_manifest();
    load_all_cases(&manifest)
        .iter()
        .map(|document| parse_case(document).expect("v2 case parses"))
        .collect()
}

/// Two probe GETs, each recorded as the stub answer. The native route of `/api/probe/native`
/// answers `native-answer`, so that case differs; the other falls through to the proxy and
/// matches.
fn probe_cases() -> Vec<Case> {
    let probe = |name: &str, path: &str| Case {
        name: name.to_owned(),
        schema_version: 2,
        kind: Kind::Http,
        family: "probe".to_owned(),
        backend: "up".to_owned(),
        ledger_key: None,
        request: Some(
            json!({"method": "GET", "path": path, "query": {}, "headers": {}, "body": null}),
        ),
        invocation: None,
        response: Some(json!({
            "status": 200,
            "headers": {"content-type": "text/plain; charset=utf-8"},
            "body": "stub-answer",
        })),
        db_delta: Vec::new(),
        side_effects: Vec::new(),
        normalizers: Map::new(),
        mask: json!([]),
        waiver: None,
        native_response: None,
    };
    vec![
        probe("probe_native", "/api/probe/native"),
        probe("probe_shared", "/api/probe/shared"),
    ]
}

async fn run_soak(
    cases: &[Case],
    families: &[&'static dyn RouteFamily<FrontDoorState>],
    iterations: u64,
    home: &Path,
) -> SoakRun {
    Soak {
        cases,
        families,
        seed: 11,
        hours: 1.0,
        iterations: Some(iterations),
        home,
    }
    .run()
    .await
    .expect("soak run")
}

#[tokio::test]
async fn compare_report_passes_on_a_match_only_window() {
    let home = tempfile::tempdir().expect("home");
    let run = run_soak(&corpus(), FAMILIES, 2, home.path()).await;
    let report = compare_report(&run.home, run.since).expect("compare-report");
    assert!(
        report.success,
        "a match-only window passes: {}",
        report.report
    );
    assert_eq!(report.report["families"]["health"]["match"], 4);
}

#[tokio::test]
async fn compare_report_fails_on_a_diff() {
    let home = tempfile::tempdir().expect("home");
    let run = run_soak(&probe_cases(), &[&PROBE], 1, home.path()).await;
    let report = compare_report(&run.home, run.since).expect("compare-report");
    assert!(
        !report.success,
        "a diff fails the report: {}",
        report.report
    );
    assert_eq!(report.report["families"]["probe"]["diff"], 1);
    assert_eq!(report.report["families"]["probe"]["match"], 1);
}

#[tokio::test]
async fn summarize_passes_a_match_only_run() {
    let home = tempfile::tempdir().expect("home");
    let run = run_soak(&corpus(), FAMILIES, 2, home.path()).await;
    let summary = summarize(&run).expect("summary");
    assert!(summary.passed);
    assert_eq!(summary.json["expected"], summary.json["recorded"]);
    assert_eq!(summary.json["failures"], json!([]));
}

#[tokio::test]
async fn summary_json_has_the_documented_fields() {
    let home = tempfile::tempdir().expect("home");
    let run = run_soak(&corpus(), FAMILIES, 2, home.path()).await;
    let summary = summarize(&run).expect("summary");
    let json = &summary.json;
    assert_eq!(json["iterations"], 2);
    assert_eq!(json["seed"], 11);
    assert_eq!(json["N"], 1.0);
    assert_eq!(json["since"], run.since);
    assert_eq!(
        json["expected"],
        serde_json::to_value(&run.expected).expect("counts")
    );
    assert_eq!(json["recorded"]["health"]["match"], 4);
    assert_eq!(json["excluded"].as_array().map(Vec::len), Some(14));
    assert!(json["failures"].is_array());
    assert!(
        json["elapsed_seconds"]
            .as_f64()
            .is_some_and(|seconds| seconds >= 0.0)
    );
}

#[tokio::test]
async fn a_diff_run_fails_and_lists_the_failing_case() {
    let home = tempfile::tempdir().expect("home");
    let run = run_soak(&probe_cases(), &[&PROBE], 1, home.path()).await;
    let summary = summarize(&run).expect("summary");
    assert!(!summary.passed, "a run with a diff does not pass");
    let failures = summary.json["failures"].as_array().expect("failures");
    assert_eq!(failures.len(), 1);
    assert_eq!(failures[0]["case"], "probe_native");
    assert_eq!(failures[0]["i"], 0);
    assert_eq!(failures[0]["outcome"], "diff");
    assert_ne!(summary.json["expected"], summary.json["recorded"]);
}

#[tokio::test]
async fn the_summary_file_is_written_into_the_run_home() {
    let home = tempfile::tempdir().expect("home");
    let run = run_soak(&corpus(), FAMILIES, 1, home.path()).await;
    let summary = summarize(&run).expect("summary");
    let written: Value = serde_json::from_str(
        &std::fs::read_to_string(run.home.join(SUMMARY_FILE)).expect("summary file"),
    )
    .expect("summary json");
    assert_eq!(written, summary.json);
    assert!(
        read_records(&run.home)
            .iter()
            .all(|record| record.outcome == Outcome::Match)
    );
}

#[tokio::test]
async fn a_missing_record_fails_the_summary_though_the_report_passes() {
    let home = tempfile::tempdir().expect("home");
    let run = run_soak(&corpus(), FAMILIES, 2, home.path()).await;
    let log = gobby_daemon::front_door::compare::log_path(&run.home);
    let text = std::fs::read_to_string(&log).expect("run log");
    let mut lines: Vec<&str> = text.lines().collect();
    lines.pop();
    std::fs::write(&log, lines.join("\n") + "\n").expect("drop a record");
    let report = compare_report(&run.home, run.since).expect("compare-report");
    assert!(
        report.success,
        "a match-only window still passes the report: {}",
        report.report
    );
    let summary = summarize(&run).expect("summary");
    assert!(
        !summary.passed,
        "recorded counts differ from the expected counts"
    );
    assert_eq!(summary.json["recorded"]["health"]["match"], 3);
    assert_ne!(summary.json["expected"], summary.json["recorded"]);
}
