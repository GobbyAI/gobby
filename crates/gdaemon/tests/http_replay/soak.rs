// Seeded soak loop in Compare mode.
//
// Iteration `i` runs every scored case once, in the order `order_for(seed, i, n)` gives. Each
// iteration has its own home, so its compare log is fresh. Its records are checked against the
// expected count, then appended to the run's own log, which the summary reads. The run stops
// when its wall-clock budget is spent or its iteration cap is reached, whichever comes first.

use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use gobby_core::bootstrap::RouteBackend;
use gobby_core::route_family::RouteFamily;
use gobby_daemon::front_door::FrontDoorState;
use gobby_daemon::front_door::compare::{Outcome, Record, log_path};
use gobby_daemon::front_door::routes::FAMILIES;
use gobby_test_support::corpus_v2::{load_all_cases, load_manifest, request_target};
use rand::SeedableRng;
use rand::rngs::StdRng;
use rand::seq::SliceRandom;
use serde_json::{Value, json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::task::JoinHandle;

use super::backend::{Backends, native_family, read_records, start_listener_task};
use super::corpus::{Case, Kind, parse_case};
use super::differential::recorded_target_task;

/// The sidecar the run writes into its home: one JSON line per iteration.
pub const SIDECAR_FILE: &str = "soak.jsonl";

/// A non-GET claimed request: the family's `check_proxied` answers `Skipped` (route_family.rs).
pub const REASON_NON_GET: &str = "non-GET claimed request: check_proxied is skipped";
/// No native family claims the request path, so the front door writes no compare record.
pub const REASON_NO_NATIVE: &str = "no native family claims the request path";
/// Compare has no route for a non-HTTP case kind.
pub const REASON_NOT_HTTP: &str = "not an HTTP case: Compare has no route for its kind";
/// The case has no recorded response to compare the native answer with.
pub const REASON_NO_RECORDING: &str = "the case has no recorded response";

/// The longest one request of a leg may take.
const TIMEOUT: Duration = Duration::from_secs(10);

/// Outcome counts per family, the shape `compare-report` prints.
pub type Counts = BTreeMap<String, BTreeMap<Outcome, u64>>;

/// A case the soak does not run, with the reason it is not scored.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Excluded {
    pub case: String,
    pub family: String,
    pub reason: &'static str,
}

/// Why a soak run failed. A run with a diff is not an error: the summary decides that.
#[derive(Debug)]
pub enum SoakError {
    /// No case in the list is scored in Compare mode.
    NoScoredCases,
    /// The iteration cap was zero.
    ZeroIterations,
    /// The budget is not a finite, non-negative number of hours.
    InvalidHours(f64),
    /// A file in the run home could not be read or written.
    Io(String),
    /// A leg of one iteration could not be run.
    Leg { case: String, message: String },
    /// An iteration's compare log does not hold the expected records (a dropped or extra record).
    Count {
        iteration: u64,
        family: String,
        expected: u64,
        recorded: u64,
    },
}

impl std::fmt::Display for SoakError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NoScoredCases => write!(f, "no case is scored in Compare mode"),
            Self::ZeroIterations => write!(f, "the iteration cap must be at least one"),
            Self::InvalidHours(hours) => write!(f, "invalid budget of {hours} hours"),
            Self::Io(message) => write!(f, "soak file: {message}"),
            Self::Leg { case, message } => write!(f, "case {case}: {message}"),
            Self::Count {
                iteration,
                family,
                expected,
                recorded,
            } => write!(
                f,
                "iteration {iteration}: family {family} has {recorded} compare records, expected {expected} (a record was dropped or duplicated)"
            ),
        }
    }
}

impl std::error::Error for SoakError {}

/// One soak run: its run home, budget, and the expected counts from the iterations it ran.
#[derive(Debug, Clone, PartialEq)]
pub struct SoakRun {
    /// The run home. It holds `soak.jsonl`, `front-door-compare.jsonl` and the summary.
    pub home: PathBuf,
    pub seed: u64,
    /// The budget as given.
    pub hours: f64,
    pub iterations: u64,
    pub elapsed_seconds: f64,
    /// Whole Unix seconds, at or before the first request.
    pub since: i64,
    /// The scored case names, in case-list order.
    pub scored: Vec<String>,
    pub excluded: Vec<Excluded>,
    /// Expected records per family and outcome: each scored case once per iteration, as Match.
    pub expected: Counts,
}

/// Splits the case list into the cases Compare can score and the excluded ones, in list order.
///
/// A case is scored when it is an HTTP GET or HEAD, a native family claims its path, and it has
/// a recorded response. Any other case is excluded with the reason it is not scored.
pub fn partition(
    cases: &[Case],
    families: &[&'static dyn RouteFamily<FrontDoorState>],
) -> (Vec<Case>, Vec<Excluded>) {
    let mut scored = Vec::new();
    let mut excluded = Vec::new();
    for case in cases {
        match exclusion(case, families) {
            None => scored.push(case.clone()),
            Some(reason) => excluded.push(Excluded {
                case: case.name.clone(),
                family: case.family.clone(),
                reason,
            }),
        }
    }
    (scored, excluded)
}

/// The order of the scored cases in iteration `iteration`: a permutation of `0..len`, shuffled
/// by `StdRng::seed_from_u64(seed + iteration)`.
pub fn order_for(seed: u64, iteration: u64, len: usize) -> Vec<usize> {
    let mut order: Vec<usize> = (0..len).collect();
    order.shuffle(&mut StdRng::seed_from_u64(seed.wrapping_add(iteration)));
    order
}

/// The expected records for `iterations` passes over the scored cases.
pub fn expected_counts(scored: &[Case], iterations: u64) -> Counts {
    let mut counts = Counts::new();
    for case in scored {
        *counts
            .entry(case.family.clone())
            .or_default()
            .entry(Outcome::Match)
            .or_default() += iterations;
    }
    counts
}

/// One soak run over a case list.
pub struct Soak<'a> {
    pub cases: &'a [Case],
    pub families: &'a [&'static dyn RouteFamily<FrontDoorState>],
    pub seed: u64,
    /// Wall-clock budget in hours; fractions are allowed.
    pub hours: f64,
    /// An optional cap on the number of iterations.
    pub iterations: Option<u64>,
    /// The run home.
    pub home: &'a Path,
}

impl Soak<'_> {
    /// Runs the soak with no hook.
    pub async fn run(&self) -> Result<SoakRun, SoakError> {
        self.run_with_hook(&mut |_, _| {}).await
    }

    /// Runs the soak. `hook` is called once per iteration, after its requests and before its
    /// compare log is checked, with the iteration number and that iteration's home.
    pub async fn run_with_hook(
        &self,
        hook: &mut dyn FnMut(u64, &Path),
    ) -> Result<SoakRun, SoakError> {
        if self.iterations == Some(0) {
            return Err(SoakError::ZeroIterations);
        }
        let budget = Duration::try_from_secs_f64(self.hours * 3600.0)
            .map_err(|_| SoakError::InvalidHours(self.hours))?;
        let (scored, excluded) = partition(self.cases, self.families);
        if scored.is_empty() {
            return Err(SoakError::NoScoredCases);
        }
        std::fs::create_dir_all(self.home).map_err(|error| io_error(self.home, &error))?;
        let since = now_seconds();
        let started = Instant::now();
        let mut iterations = 0_u64;
        loop {
            self.run_iteration(iterations, &scored, hook).await?;
            iterations += 1;
            if started.elapsed() >= budget || self.iterations.is_some_and(|cap| iterations >= cap) {
                break;
            }
        }
        let elapsed = started.elapsed();
        Ok(SoakRun {
            home: self.home.to_path_buf(),
            seed: self.seed,
            hours: self.hours,
            iterations,
            elapsed_seconds: elapsed.as_secs_f64(),
            since,
            scored: scored.iter().map(|case| case.name.clone()).collect(),
            excluded,
            expected: expected_counts(&scored, iterations),
        })
    }

    /// One pass over the scored cases in this iteration's order, in a fresh home.
    async fn run_iteration(
        &self,
        iteration: u64,
        scored: &[Case],
        hook: &mut dyn FnMut(u64, &Path),
    ) -> Result<(), SoakError> {
        let home = self.home.join("iterations").join(iteration.to_string());
        std::fs::create_dir_all(&home).map_err(|error| io_error(&home, &error))?;
        let order = order_for(self.seed, iteration, scored.len());
        for &index in &order {
            self.send_case(&scored[index], &home).await?;
        }
        hook(iteration, &home);
        check_counts(iteration, scored, &read_records(&home))?;

        let iteration_log = log_path(&home);
        let text = read_text(&iteration_log)?;
        append_text(&log_path(self.home), &text)?;
        let names: Vec<&str> = order
            .iter()
            .map(|&index| scored[index].name.as_str())
            .collect();
        let sidecar = json!({"i": iteration, "seed": self.seed, "order": names});
        append_text(&self.home.join(SIDECAR_FILE), &format!("{sidecar}\n"))?;
        std::fs::remove_dir_all(&home).map_err(|error| io_error(&home, &error))
    }

    /// One scored case, through a listener in Compare mode whose proxied leg answers with the
    /// case's recording. Both tasks of the leg stop when the leg is dropped.
    async fn send_case(&self, case: &Case, home: &Path) -> Result<(), SoakError> {
        let leg_error = |message: String| SoakError::Leg {
            case: case.name.clone(),
            message,
        };
        let Some(recorded) = case.response.as_ref() else {
            return Err(leg_error("the case has no recorded response".to_owned()));
        };
        let (proxied, proxied_task) = recorded_target_task(recorded).await;
        let backends = Backends::new([(case.family.as_str(), RouteBackend::Compare)]);
        let (addr, listener_task) =
            start_listener_task(self.families, &backends, proxied, home).await;
        let _leg = Leg(vec![proxied_task, listener_task]);
        send(addr, &method(case), &target(case))
            .await
            .map(|_| ())
            .map_err(leg_error)
    }
}

/// The tasks of one leg. Dropping the leg stops them, so no listener outlives its request.
struct Leg(Vec<JoinHandle<()>>);

impl Drop for Leg {
    fn drop(&mut self) {
        for task in &self.0 {
            task.abort();
        }
    }
}

fn exclusion(
    case: &Case,
    families: &[&'static dyn RouteFamily<FrontDoorState>],
) -> Option<&'static str> {
    if case.kind != Kind::Http {
        return Some(REASON_NOT_HTTP);
    }
    if !matches!(method(case).as_str(), "GET" | "HEAD") {
        return Some(REASON_NON_GET);
    }
    let target = target(case);
    let claimed =
        native_family(families, &case.family).is_ok_and(|family| claims_path(family, &target));
    if !claimed {
        return Some(REASON_NO_NATIVE);
    }
    if case.response.is_none() {
        return Some(REASON_NO_RECORDING);
    }
    None
}

/// Whether the family's prefixes claim the path, matching whole segments as the front door does.
fn claims_path(family: &dyn RouteFamily<FrontDoorState>, target: &str) -> bool {
    let path = target.split('?').next().unwrap_or_default();
    family.prefixes().iter().any(|prefix| {
        path == *prefix
            || path
                .strip_prefix(*prefix)
                .is_some_and(|rest| rest.starts_with('/'))
    })
}

fn method(case: &Case) -> String {
    case.request
        .as_ref()
        .and_then(|request| request["method"].as_str())
        .unwrap_or_default()
        .to_ascii_uppercase()
}

fn target(case: &Case) -> String {
    case.request
        .as_ref()
        .map(request_target)
        .unwrap_or_default()
}

/// Fails when an iteration's compare log does not hold the expected record of each family.
fn check_counts(iteration: u64, scored: &[Case], records: &[Record]) -> Result<(), SoakError> {
    let mut expected: BTreeMap<&str, u64> = BTreeMap::new();
    for case in scored {
        *expected.entry(case.family.as_str()).or_default() += 1;
    }
    let mut recorded: BTreeMap<&str, u64> = BTreeMap::new();
    for record in records {
        *recorded.entry(record.family.as_str()).or_default() += 1;
    }
    let families: BTreeSet<&str> = expected.keys().chain(recorded.keys()).copied().collect();
    for family in families {
        let wanted = expected.get(family).copied().unwrap_or(0);
        let got = recorded.get(family).copied().unwrap_or(0);
        if wanted != got {
            return Err(SoakError::Count {
                iteration,
                family: family.to_owned(),
                expected: wanted,
                recorded: got,
            });
        }
    }
    Ok(())
}

/// Sends one request over a fresh connection and returns the status and the body text.
async fn send(addr: SocketAddr, method: &str, target: &str) -> Result<(u16, String), String> {
    let exchange = async {
        let mut stream = TcpStream::connect(addr)
            .await
            .map_err(|error| error.to_string())?;
        let request =
            format!("{method} {target} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n");
        stream
            .write_all(request.as_bytes())
            .await
            .map_err(|error| error.to_string())?;
        let mut raw = Vec::new();
        stream
            .read_to_end(&mut raw)
            .await
            .map_err(|error| error.to_string())?;
        Ok::<_, String>(String::from_utf8_lossy(&raw).into_owned())
    };
    let text = tokio::time::timeout(TIMEOUT, exchange)
        .await
        .map_err(|_| "the request timed out".to_owned())??;
    let (head, body) = text
        .split_once("\r\n\r\n")
        .ok_or_else(|| "the response has no header end".to_owned())?;
    let status = head
        .split_whitespace()
        .nth(1)
        .and_then(|code| code.parse().ok())
        .ok_or_else(|| "the response has no status code".to_owned())?;
    Ok((status, body.to_owned()))
}

fn io_error(path: &Path, error: &std::io::Error) -> SoakError {
    SoakError::Io(format!("{}: {error}", path.display()))
}

fn read_text(path: &Path) -> Result<String, SoakError> {
    match std::fs::read_to_string(path) {
        Ok(text) => Ok(text),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(String::new()),
        Err(error) => Err(io_error(path, &error)),
    }
}

fn append_text(path: &Path, text: &str) -> Result<(), SoakError> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| io_error(parent, &error))?;
    }
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|error| io_error(path, &error))?;
    file.write_all(text.as_bytes())
        .map_err(|error| io_error(path, &error))
}

fn now_seconds() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| {
            i64::try_from(elapsed.as_secs()).unwrap_or(i64::MAX)
        })
}

fn corpus() -> Vec<Case> {
    let manifest = load_manifest();
    load_all_cases(&manifest)
        .iter()
        .map(|document| parse_case(document).expect("v2 case parses"))
        .collect()
}

fn json_lines(path: &Path) -> Vec<Value> {
    std::fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .map(|line| serde_json::from_str(line).expect("json line"))
        .collect()
}

fn health_counts(matches: u64) -> Counts {
    Counts::from([(
        "health".to_owned(),
        BTreeMap::from([(Outcome::Match, matches)]),
    )])
}

const SEED: u64 = 11;

#[test]
fn order_is_a_seeded_permutation_reproduced_from_seed_and_iteration() {
    let first = order_for(7, 3, 16);
    assert_eq!(
        first,
        order_for(7, 3, 16),
        "same seed and iteration, same order"
    );
    let mut sorted = first.clone();
    sorted.sort_unstable();
    assert_eq!(
        sorted,
        (0..16).collect::<Vec<usize>>(),
        "a permutation of the indices"
    );
    let mut expected: Vec<usize> = (0..16).collect();
    expected.shuffle(&mut StdRng::seed_from_u64(7 + 3));
    assert_eq!(
        first, expected,
        "the shuffle is seeded with seed + iteration and nothing else"
    );
}

#[test]
fn order_depends_on_the_seed_and_the_iteration() {
    let base = order_for(7, 3, 16);
    assert_ne!(
        base,
        order_for(7, 4, 16),
        "a different iteration shuffles differently"
    );
    assert_ne!(
        base,
        order_for(8, 3, 16),
        "a different seed shuffles differently"
    );
}

#[test]
fn expected_counts_are_the_case_list_minus_the_excluded_set() {
    let cases = corpus();
    let (scored, excluded) = partition(&cases, FAMILIES);
    let mut names: Vec<&str> = scored.iter().map(|case| case.name.as_str()).collect();
    names.sort_unstable();
    assert_eq!(names, ["health_backend_down", "health_ok"]);
    assert_eq!(scored.len() + excluded.len(), cases.len());
    assert_eq!(expected_counts(&scored, 5), health_counts(10));
}

#[test]
fn excluded_cases_are_listed_with_their_reason() {
    let cases = corpus();
    let (_, excluded) = partition(&cases, FAMILIES);
    assert_eq!(excluded.len(), 14, "16 v2 cases minus the two health cases");
    let by_name: BTreeMap<&str, &Excluded> = excluded
        .iter()
        .map(|entry| (entry.case.as_str(), entry))
        .collect();
    assert_eq!(by_name["tasks_list"].reason, REASON_NO_NATIVE);
    assert_eq!(by_name["terminal_backend_down"].reason, REASON_NO_NATIVE);
    assert_eq!(by_name["hooks_execute_ok"].reason, REASON_NON_GET);
}

#[test]
fn a_post_to_a_native_family_is_excluded_as_skipped() {
    let cases = corpus();
    let mut post = cases
        .iter()
        .find(|case| case.name == "health_ok")
        .expect("health_ok")
        .clone();
    post.name = "health_post".to_owned();
    post.request = Some(
        json!({"method": "POST", "path": "/api/health", "query": {}, "headers": {}, "body": null}),
    );
    let (scored, excluded) = partition(std::slice::from_ref(&post), FAMILIES);
    assert!(scored.is_empty(), "a POST is never scored");
    assert_eq!(
        excluded,
        vec![Excluded {
            case: "health_post".to_owned(),
            family: "health".to_owned(),
            reason: REASON_NON_GET,
        }]
    );
}

#[tokio::test]
async fn a_run_writes_a_sidecar_with_the_seed_and_each_iterations_order() {
    let home = tempfile::tempdir().expect("home");
    let cases = corpus();
    let run = Soak {
        cases: &cases,
        families: FAMILIES,
        seed: SEED,
        hours: 1.0,
        iterations: Some(3),
        home: home.path(),
    }
    .run()
    .await
    .expect("run");
    assert_eq!(run.iterations, 3);
    let sidecar = json_lines(&run.home.join(SIDECAR_FILE));
    assert_eq!(sidecar.len(), 3, "one line per iteration");
    for (iteration, line) in sidecar.iter().enumerate() {
        let order: Vec<&str> = order_for(SEED, iteration as u64, run.scored.len())
            .into_iter()
            .map(|index| run.scored[index].as_str())
            .collect();
        assert_eq!(
            *line,
            json!({"i": iteration, "seed": SEED, "order": order}),
            "iteration {iteration}"
        );
    }
}

#[tokio::test]
async fn a_run_scores_every_scored_case_as_match() {
    let home = tempfile::tempdir().expect("home");
    let cases = corpus();
    let run = Soak {
        cases: &cases,
        families: FAMILIES,
        seed: SEED,
        hours: 1.0,
        iterations: Some(2),
        home: home.path(),
    }
    .run()
    .await
    .expect("run");
    let records = read_records(&run.home);
    assert_eq!(records.len(), 4, "two scored cases, two iterations");
    assert!(records.iter().all(|record| record.family == "health"));
    assert!(
        records
            .iter()
            .all(|record| record.outcome == Outcome::Match)
    );
    assert_eq!(run.expected, health_counts(4));
}

#[tokio::test]
async fn a_dropped_record_fails_the_run() {
    let home = tempfile::tempdir().expect("home");
    let cases = corpus();
    let mut drop_last = |iteration: u64, iteration_home: &Path| {
        if iteration == 0 {
            let path = gobby_daemon::front_door::compare::log_path(iteration_home);
            let text = std::fs::read_to_string(&path).expect("iteration log");
            let mut lines: Vec<&str> = text.lines().collect();
            lines.pop();
            let kept = lines.join("\n") + "\n";
            std::fs::write(&path, kept).expect("drop a record");
        }
    };
    let error = Soak {
        cases: &cases,
        families: FAMILIES,
        seed: SEED,
        hours: 1.0,
        iterations: Some(2),
        home: home.path(),
    }
    .run_with_hook(&mut drop_last)
    .await
    .expect_err("a dropped record must fail the run");
    assert!(
        matches!(error, SoakError::Count { iteration: 0, .. }),
        "got {error}"
    );
}

#[tokio::test]
async fn runs_until_budget() {
    let home = tempfile::tempdir().expect("home");
    let cases = corpus();
    let budget = 0.001 * 3600.0;
    let run = Soak {
        cases: &cases,
        families: FAMILIES,
        seed: SEED,
        hours: 0.001,
        iterations: None,
        home: home.path(),
    }
    .run()
    .await
    .expect("run");
    assert!(run.iterations >= 1, "at least one iteration is recorded");
    assert!(
        run.elapsed_seconds >= budget,
        "stops at the deadline: {} seconds against a budget of {budget}",
        run.elapsed_seconds
    );
    assert!(run.elapsed_seconds < budget + 60.0, "stops at the deadline");
    assert_eq!(run.hours, 0.001);
    assert_eq!(read_records(&run.home).len() as u64, 2 * run.iterations);
}
