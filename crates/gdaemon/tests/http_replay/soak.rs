// Seeded soak loop in Compare mode (design-v2 sections 7.10 and H12).
//
// Iteration `i` runs every scored case once, in the order `order_for(seed, i, n)` gives. Each
// iteration has its own home, so its compare log is fresh. Its records are checked against the
// expected count, then appended to the run's own log, which the summary reads. The run stops
// when its wall-clock budget is spent or its iteration cap is reached, whichever comes first.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use gobby_core::route_family::RouteFamily;
use gobby_daemon::front_door::FrontDoorState;
use gobby_daemon::front_door::compare::Outcome;
use gobby_daemon::front_door::routes::FAMILIES;
use gobby_test_support::corpus_v2::{load_all_cases, load_manifest};
use rand::rngs::StdRng;
use rand::seq::SliceRandom;
use rand::SeedableRng;
use serde_json::{Value, json};

use super::backend::read_records;
use super::corpus::{Case, parse_case};

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
pub fn partition(
    cases: &[Case],
    families: &[&'static dyn RouteFamily<FrontDoorState>],
) -> (Vec<Case>, Vec<Excluded>) {
    let _ = (cases, families);
    unimplemented!("not wired: soak::partition")
}

/// The order of the scored cases in iteration `iteration`: a permutation of `0..len`, shuffled
/// by `StdRng::seed_from_u64(seed + iteration)`.
pub fn order_for(seed: u64, iteration: u64, len: usize) -> Vec<usize> {
    let _ = (seed, iteration, len);
    unimplemented!("not wired: soak::order_for")
}

/// The expected records for `iterations` passes over the scored cases.
pub fn expected_counts(scored: &[Case], iterations: u64) -> Counts {
    let _ = (scored, iterations);
    unimplemented!("not wired: soak::expected_counts")
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
        let _ = hook;
        unimplemented!("not wired: soak::Soak::run_with_hook")
    }
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
    assert_eq!(first, order_for(7, 3, 16), "same seed and iteration, same order");
    let mut sorted = first.clone();
    sorted.sort_unstable();
    assert_eq!(sorted, (0..16).collect::<Vec<usize>>(), "a permutation of the indices");
    let mut expected: Vec<usize> = (0..16).collect();
    expected.shuffle(&mut StdRng::seed_from_u64(7 + 3));
    assert_eq!(first, expected, "the shuffle is seeded with seed + iteration and nothing else");
}

#[test]
fn order_depends_on_the_seed_and_the_iteration() {
    let base = order_for(7, 3, 16);
    assert_ne!(base, order_for(7, 4, 16), "a different iteration shuffles differently");
    assert_ne!(base, order_for(8, 3, 16), "a different seed shuffles differently");
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
    post.request = Some(json!({"method": "POST", "path": "/api/health", "query": {}, "headers": {}, "body": null}));
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
    assert!(records.iter().all(|record| record.outcome == Outcome::Match));
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
