//! Family lifecycle hooks, the job sink and dormancy, driven through the gcore runners
//! with fixture families and the daemon's own heartbeat host.
//!
//! The runners share one deadline per stop or unwind phase, and activation is
//! transactional: jobs reach the sink only when the whole pass succeeds.

use super::common;

use std::collections::BTreeMap;
use std::convert::Infallible;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::Router;
use axum::body::Body;
use axum::http::{Request, Response, StatusCode};
use bytes::Bytes;
use gobby_core::bootstrap::RouteBackend;
use gobby_core::family::FamilyContext;
use gobby_core::family::lifecycle::{
    Activation, ActivationEntry, HookError, JobRefused, JobRun, JobSink, JobStatus,
    OrdinaryFailure, SpawnKind, StartFailure, StartReport, StopFailure, StopMode,
    activate_families, deactivate_families, run_after_healthy, run_before_spawn,
    run_recovery_complete, start_families,
};
use gobby_core::route_family::{
    BoxFut, Credential, Gate, Kind, PathMatch, PeerRule, RouteClaim, RouteFamily,
};
use gobby_daemon::front_door::auth::AuthState;
use gobby_daemon::front_door::health::BackendState;
use gobby_daemon::front_door::registry::FAMILIES;
use gobby_daemon::front_door::routes::RouteTable;
use gobby_daemon::front_door::{FrontDoor, FrontDoorState};
use gobby_daemon::heartbeat::HeartbeatHost;
use http_body_util::{BodyExt, Full};
use hyper::body::Incoming;
use hyper::server::conn::http1 as server_http1;
use hyper::service::service_fn;
use hyper_util::rt::TokioIo;
use tokio::net::TcpListener;
use tokio::sync::Notify;
use tokio::time::Instant;

type Log = Arc<Mutex<Vec<String>>>;

/// What one fixture hook does when it runs.
#[derive(Clone, Copy, Debug)]
enum Step {
    Pass,
    Ordinary(&'static str),
    Fatal(&'static str, bool),
    Hang,
}

/// A family whose every hook records itself in a shared log and does what its step says.
/// Activation registers the fixture's jobs before the step runs.
struct Hooked {
    name: &'static str,
    backend: RouteBackend,
    prefixes: &'static [&'static str],
    start: Mutex<Step>,
    activate: Mutex<Step>,
    deactivate: Mutex<Step>,
    jobs: &'static [(&'static str, Duration)],
    per_cycle: bool,
    log: Log,
    activations: AtomicUsize,
    job_runs: Arc<AtomicUsize>,
    ran: Arc<Notify>,
    refusals: Mutex<Vec<JobRefused>>,
    routers: AtomicUsize,
}

impl Hooked {
    fn leak(name: &'static str, backend: RouteBackend, log: &Log) -> &'static Hooked {
        Self::build(name, backend, log, &[], false)
    }

    fn with_jobs(
        name: &'static str,
        backend: RouteBackend,
        log: &Log,
        jobs: &'static [(&'static str, Duration)],
    ) -> &'static Hooked {
        Self::build(name, backend, log, jobs, false)
    }

    /// A Native family that owns `prefixes`, so the routing table reaches its router.
    fn routed(
        name: &'static str,
        prefixes: &'static [&'static str],
        log: &Log,
        jobs: &'static [(&'static str, Duration)],
    ) -> &'static Hooked {
        Self::assemble(name, RouteBackend::Native, prefixes, log, jobs, false)
    }

    fn build(
        name: &'static str,
        backend: RouteBackend,
        log: &Log,
        jobs: &'static [(&'static str, Duration)],
        per_cycle: bool,
    ) -> &'static Hooked {
        Self::assemble(name, backend, &[], log, jobs, per_cycle)
    }

    fn assemble(
        name: &'static str,
        backend: RouteBackend,
        prefixes: &'static [&'static str],
        log: &Log,
        jobs: &'static [(&'static str, Duration)],
        per_cycle: bool,
    ) -> &'static Hooked {
        Box::leak(Box::new(Hooked {
            name,
            backend,
            prefixes,
            start: Mutex::new(Step::Pass),
            activate: Mutex::new(Step::Pass),
            deactivate: Mutex::new(Step::Pass),
            jobs,
            per_cycle,
            log: Arc::clone(log),
            activations: AtomicUsize::new(0),
            job_runs: Arc::new(AtomicUsize::new(0)),
            ran: Arc::new(Notify::new()),
            refusals: Mutex::new(Vec::new()),
            routers: AtomicUsize::new(0),
        }))
    }

    fn set_start(&self, step: Step) -> &Self {
        *self.start.lock().expect("start step") = step;
        self
    }

    fn set_activate(&self, step: Step) -> &Self {
        *self.activate.lock().expect("activate step") = step;
        self
    }

    fn set_deactivate(&self, step: Step) -> &Self {
        *self.deactivate.lock().expect("deactivate step") = step;
        self
    }

    fn record(&self, event: String) {
        self.log.lock().expect("hook log").push(event);
    }

    fn job_runs(&self) -> usize {
        self.job_runs.load(Ordering::SeqCst)
    }

    fn refusals(&self) -> Vec<JobRefused> {
        self.refusals.lock().expect("job refusals").clone()
    }

    fn job(&self) -> JobRun {
        let runs = Arc::clone(&self.job_runs);
        let ran = Arc::clone(&self.ran);
        Arc::new(move || {
            let runs = Arc::clone(&runs);
            let ran = Arc::clone(&ran);
            Box::pin(async move {
                runs.fetch_add(1, Ordering::SeqCst);
                ran.notify_one();
                Ok(())
            })
        })
    }
}

async fn perform(step: Step) -> Result<(), HookError> {
    match step {
        Step::Pass => Ok(()),
        Step::Ordinary(message) => Err(HookError::Ordinary {
            message: message.into(),
        }),
        Step::Fatal(message, relaunch_could_clear) => Err(HookError::Fatal {
            message: message.into(),
            relaunch_could_clear,
        }),
        Step::Hang => std::future::pending().await,
    }
}

impl RouteFamily<FrontDoorState> for Hooked {
    fn name(&self) -> &'static str {
        self.name
    }

    fn prefixes(&self) -> &'static [&'static str] {
        self.prefixes
    }

    fn router(&self) -> Router<FrontDoorState> {
        self.routers.fetch_add(1, Ordering::SeqCst);
        Router::new()
    }

    fn default_backend(&self) -> RouteBackend {
        self.backend
    }

    fn start<'a>(&'a self, _ctx: &'a FamilyContext) -> BoxFut<'a, Result<(), HookError>> {
        self.record(format!("start:{}", self.name));
        let step = *self.start.lock().expect("start step");
        Box::pin(perform(step))
    }

    fn activate<'a>(
        &'a self,
        activation: &'a mut Activation<'_>,
    ) -> BoxFut<'a, Result<(), HookError>> {
        self.record(format!("activate:{}", self.name));
        let cycle = self.activations.fetch_add(1, Ordering::SeqCst) + 1;
        for (job, interval) in self.jobs {
            let name = if self.per_cycle {
                format!("{}.{job}.{cycle}", self.name)
            } else {
                format!("{}.{job}", self.name)
            };
            if let Err(refused) = activation.register_job(&name, *interval, self.job()) {
                self.refusals.lock().expect("job refusals").push(refused);
            }
        }
        let step = *self.activate.lock().expect("activate step");
        Box::pin(perform(step))
    }

    fn deactivate<'a>(&'a self, _mode: StopMode) -> BoxFut<'a, Result<(), StopFailure>> {
        self.record(format!("deactivate:{}", self.name));
        let step = *self.deactivate.lock().expect("deactivate step");
        let family = self.name;
        Box::pin(async move {
            match step {
                Step::Pass => Ok(()),
                Step::Ordinary(message) | Step::Fatal(message, _) => Err(StopFailure {
                    family,
                    message: message.into(),
                }),
                Step::Hang => std::future::pending().await,
            }
        })
    }

    fn before_spawn<'a>(
        &'a self,
        _ctx: &'a FamilyContext,
        spawn: SpawnKind,
    ) -> BoxFut<'a, Result<(), HookError>> {
        self.record(format!("before_spawn:{}:{spawn:?}", self.name));
        Box::pin(async { Ok(()) })
    }

    fn after_healthy<'a>(&'a self, _ctx: &'a FamilyContext) -> BoxFut<'a, Result<(), HookError>> {
        self.record(format!("after_healthy:{}", self.name));
        Box::pin(async { Ok(()) })
    }

    fn recovery_complete<'a>(
        &'a self,
        _ctx: &'a FamilyContext,
    ) -> BoxFut<'a, Result<(), HookError>> {
        self.record(format!("recovery_complete:{}", self.name));
        Box::pin(async { Ok(()) })
    }
}

/// A Native family that overrides no hook.
struct Plain;

impl RouteFamily<FrontDoorState> for Plain {
    fn name(&self) -> &'static str {
        "plain"
    }

    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }

    fn router(&self) -> Router<FrontDoorState> {
        Router::new()
    }

    fn default_backend(&self) -> RouteBackend {
        RouteBackend::Native
    }
}

/// A job sink that keeps its registrations in order and refuses a duplicate name.
#[derive(Default)]
struct RecordingSink {
    jobs: Vec<(String, Duration)>,
}

impl JobSink for RecordingSink {
    fn register_job(&mut self, name: &str, interval: Duration, _run: JobRun) -> anyhow::Result<()> {
        anyhow::ensure!(
            !self.jobs.iter().any(|(existing, _)| existing == name),
            "duplicate job {name}"
        );
        self.jobs.push((name.to_owned(), interval));
        Ok(())
    }

    fn statuses(&self) -> Vec<(String, JobStatus)> {
        self.jobs
            .iter()
            .map(|(name, interval)| {
                (
                    name.clone(),
                    JobStatus {
                        enabled: true,
                        interval: *interval,
                        last_result: None,
                    },
                )
            })
            .collect()
    }
}

const TICK: &[(&str, Duration)] = &[("tick", Duration::from_secs(10))];

fn new_log() -> Log {
    Arc::new(Mutex::new(Vec::new()))
}

fn events(log: &Log) -> Vec<String> {
    log.lock().expect("hook log").clone()
}

fn entry(family: &'static Hooked) -> ActivationEntry<FrontDoorState> {
    ActivationEntry {
        family,
        backend: family.backend,
    }
}

fn job_names(sink: &dyn JobSink) -> Vec<String> {
    sink.statuses().into_iter().map(|(name, _)| name).collect()
}

fn soon() -> Instant {
    Instant::now() + Duration::from_secs(5)
}

fn noop_job() -> JobRun {
    Arc::new(|| Box::pin(async { Ok(()) }))
}

fn auth() -> Arc<AuthState> {
    Arc::new(AuthState::new("boot-secret".into(), None, Default::default()).expect("auth state"))
}

/// A backend that answers every request with the body `backend`.
async fn stub_backend() -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("stub backend");
    let addr = listener.local_addr().expect("stub backend address");
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            tokio::spawn(async move {
                let service = service_fn(|_request: Request<Incoming>| async {
                    Ok::<_, Infallible>(Response::new(Full::new(Bytes::from_static(b"backend"))))
                });
                let _ = server_http1::Builder::new()
                    .serve_connection(TokioIo::new(stream), service)
                    .await;
            });
        }
    });
    addr
}

async fn body_text(response: Response<Body>) -> String {
    let bytes = response
        .into_body()
        .collect()
        .await
        .expect("response body")
        .to_bytes();
    String::from_utf8_lossy(&bytes).into_owned()
}

#[tokio::test]
async fn default_hooks_change_nothing() {
    let ctx = FamilyContext::unconfigured();
    let plain: &'static Plain = &Plain;
    assert_eq!(plain.start(&ctx).await, Ok(()));
    assert_eq!(plain.deactivate(StopMode::Stop).await, Ok(()));
    assert_eq!(
        plain.before_spawn(&ctx, SpawnKind::FirstStart).await,
        Ok(())
    );
    assert_eq!(plain.after_healthy(&ctx).await, Ok(()));
    assert_eq!(plain.recovery_complete(&ctx).await, Ok(()));

    let mut entries = vec![ActivationEntry {
        family: plain as &'static dyn RouteFamily<FrontDoorState>,
        backend: RouteBackend::Native,
    }];
    entries.extend(FAMILIES.iter().map(|&family| ActivationEntry {
        family,
        backend: family.default_backend(),
    }));
    let mut sink = RecordingSink::default();

    assert_eq!(
        start_families(&entries, &ctx, soon()).await,
        Ok(StartReport::default())
    );
    assert_eq!(
        activate_families(&entries, &ctx, &mut sink, soon()).await,
        Ok(StartReport::default())
    );
    assert!(job_names(&sink).is_empty());
    assert_eq!(
        deactivate_families(&entries, StopMode::Stop, soon()).await,
        Vec::<StopFailure>::new()
    );
    assert!(ctx.degraded().names().is_empty());
}

#[tokio::test]
async fn ordinary_start_error_marks_family_and_serving_continues() {
    let log = new_log();
    let flaky = Hooked::leak("flaky", RouteBackend::Native, &log);
    flaky.set_start(Step::Ordinary("unavailable"));
    let after = Hooked::leak("after", RouteBackend::Native, &log);
    let ctx = FamilyContext::unconfigured();

    let report = start_families(&[entry(flaky), entry(after)], &ctx, soon()).await;

    assert_eq!(
        report,
        Ok(StartReport {
            ordinary: vec![OrdinaryFailure {
                family: "flaky",
                message: "unavailable".into(),
            }],
        })
    );
    assert!(ctx.degraded().contains("flaky"));
    assert_eq!(events(&log), vec!["start:flaky", "start:after"]);

    let state = FrontDoorState::new(stub_backend().await, BackendState::Down, auth());
    let table = RouteTable::new(FAMILIES, &BTreeMap::new(), &state);
    let health = Request::get("/api/health")
        .body(Body::empty())
        .expect("health request");
    let response = table.dispatch(&state, health).await;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(body_text(response).await, "backend");
}

#[tokio::test]
async fn fatal_start_error_stops_later_hooks_and_returns_the_form() {
    let log = new_log();
    let a = Hooked::leak("a", RouteBackend::Native, &log);
    let b = Hooked::leak("b", RouteBackend::Native, &log);
    b.set_start(Step::Fatal("boom", true));
    let c = Hooked::leak("c", RouteBackend::Native, &log);
    let ctx = FamilyContext::unconfigured();

    let result = start_families(&[entry(a), entry(b), entry(c)], &ctx, soon()).await;

    assert_eq!(
        result,
        Err(StartFailure {
            family: "b",
            message: "boom".into(),
            relaunch_could_clear: true,
        })
    );
    assert_eq!(events(&log), vec!["start:a", "start:b", "deactivate:a"]);
}

#[tokio::test(start_paused = true)]
async fn fatal_start_unwind_shares_one_deadline() {
    let log = new_log();
    let a = Hooked::leak("a", RouteBackend::Native, &log);
    a.set_deactivate(Step::Hang);
    let b = Hooked::leak("b", RouteBackend::Native, &log);
    b.set_deactivate(Step::Hang);
    let c = Hooked::leak("c", RouteBackend::Native, &log);
    c.set_start(Step::Fatal("refused", false));
    let ctx = FamilyContext::unconfigured();
    let began = Instant::now();
    let budget = Duration::from_millis(100);

    let result = start_families(&[entry(a), entry(b), entry(c)], &ctx, began + budget).await;

    assert_eq!(
        result,
        Err(StartFailure {
            family: "c",
            message: "refused".into(),
            relaunch_could_clear: false,
        })
    );
    assert_eq!(
        events(&log),
        vec![
            "start:a",
            "start:b",
            "start:c",
            "deactivate:b",
            "deactivate:a"
        ]
    );
    assert!(
        began.elapsed() <= budget,
        "two hung unwinds took {:?}, beyond the one {budget:?} deadline",
        began.elapsed()
    );
}

#[tokio::test]
async fn fatal_relaunch_flag_round_trips_for_start_and_activate() {
    let log = new_log();
    let ctx = FamilyContext::unconfigured();
    for relaunch_could_clear in [true, false] {
        let starts = Hooked::leak("starts", RouteBackend::Native, &log);
        starts.set_start(Step::Fatal("start refused", relaunch_could_clear));
        assert_eq!(
            start_families(&[entry(starts)], &ctx, soon()).await,
            Err(StartFailure {
                family: "starts",
                message: "start refused".into(),
                relaunch_could_clear,
            })
        );

        let activates = Hooked::leak("activates", RouteBackend::Native, &log);
        activates.set_activate(Step::Fatal("lease refused", relaunch_could_clear));
        let mut sink = RecordingSink::default();
        assert_eq!(
            activate_families(&[entry(activates)], &ctx, &mut sink, soon()).await,
            Err(StartFailure {
                family: "activates",
                message: "lease refused".into(),
                relaunch_could_clear,
            })
        );
    }
}

#[tokio::test]
async fn lease_cycles_reactivate_and_stop_in_reverse() {
    let log = new_log();
    let fixtures = [
        Hooked::build("x1", RouteBackend::Native, &log, TICK, true),
        Hooked::build("x2", RouteBackend::Native, &log, TICK, true),
        Hooked::build("x3", RouteBackend::Native, &log, TICK, true),
    ];
    let entries: Vec<_> = fixtures.iter().map(|&fixture| entry(fixture)).collect();
    let ctx = FamilyContext::unconfigured();

    let mut first_lease = RecordingSink::default();
    assert_eq!(
        activate_families(&entries, &ctx, &mut first_lease, soon()).await,
        Ok(StartReport::default())
    );
    let mut second_lease = RecordingSink::default();
    assert_eq!(
        activate_families(&entries, &ctx, &mut second_lease, soon()).await,
        Ok(StartReport::default())
    );

    assert_eq!(
        job_names(&first_lease),
        vec!["x1.tick.1", "x2.tick.1", "x3.tick.1"]
    );
    assert_eq!(
        job_names(&second_lease),
        vec!["x1.tick.2", "x2.tick.2", "x3.tick.2"]
    );
    for fixture in fixtures {
        assert_eq!(
            fixture.activations.load(Ordering::SeqCst),
            2,
            "{}",
            fixture.name
        );
    }

    log.lock().expect("hook log").clear();
    assert_eq!(
        deactivate_families(&entries, StopMode::Stop, soon()).await,
        Vec::<StopFailure>::new()
    );
    assert_eq!(
        events(&log),
        vec!["deactivate:x3", "deactivate:x2", "deactivate:x1"]
    );

    log.lock().expect("hook log").clear();
    fixtures[1].set_deactivate(Step::Ordinary("drain refused"));
    assert_eq!(
        deactivate_families(&entries, StopMode::Drain, soon()).await,
        vec![StopFailure {
            family: "x2",
            message: "drain refused".into(),
        }]
    );
    assert_eq!(
        events(&log),
        vec!["deactivate:x3", "deactivate:x2", "deactivate:x1"]
    );
}

#[tokio::test]
async fn hung_deactivate_is_bounded_to_a_stop_failure() {
    let log = new_log();
    let hung = Hooked::leak("hung", RouteBackend::Native, &log);
    hung.set_deactivate(Step::Hang);
    let deadline = Instant::now() + Duration::from_millis(50);

    let failures = tokio::time::timeout(
        Duration::from_secs(2),
        deactivate_families(&[entry(hung)], StopMode::Stop, deadline),
    )
    .await;

    assert_eq!(
        failures,
        Ok(vec![StopFailure {
            family: "hung",
            message: "deactivate exceeded the stop deadline".into(),
        }])
    );
}

#[tokio::test(start_paused = true)]
async fn hung_deactivates_share_one_stop_deadline() {
    let log = new_log();
    let fixtures = [
        Hooked::leak("h1", RouteBackend::Native, &log),
        Hooked::leak("h2", RouteBackend::Native, &log),
        Hooked::leak("h3", RouteBackend::Native, &log),
    ];
    for fixture in fixtures {
        fixture.set_deactivate(Step::Hang);
    }
    let entries: Vec<_> = fixtures.iter().map(|&fixture| entry(fixture)).collect();
    let began = Instant::now();
    let budget = Duration::from_millis(100);

    let failures = deactivate_families(&entries, StopMode::Stop, began + budget).await;

    let failed: Vec<&str> = failures.iter().map(|failure| failure.family).collect();
    assert_eq!(failed, vec!["h3", "h2", "h1"]);
    assert!(
        began.elapsed() <= budget,
        "three hung hooks took {:?}, beyond the one {budget:?} deadline",
        began.elapsed()
    );
}

#[tokio::test]
async fn hook_points_run_at_their_points() {
    let log = new_log();
    let points = Hooked::leak("points", RouteBackend::Native, &log);
    let proxied = Hooked::leak("proxied", RouteBackend::Proxy, &log);
    let plain: &'static dyn RouteFamily<FrontDoorState> = &Plain;
    let entries = vec![
        entry(points),
        entry(proxied),
        ActivationEntry {
            family: plain,
            backend: RouteBackend::Native,
        },
    ];
    let ctx = FamilyContext::unconfigured();

    assert_eq!(
        run_before_spawn(&entries, &ctx, SpawnKind::FirstStart).await,
        Ok(StartReport::default())
    );
    assert_eq!(
        run_before_spawn(&entries, &ctx, SpawnKind::Respawn).await,
        Ok(StartReport::default())
    );
    for _ in 0..2 {
        assert_eq!(
            run_after_healthy(&entries, &ctx).await,
            Ok(StartReport::default())
        );
    }
    assert_eq!(
        run_recovery_complete(&entries, &ctx).await,
        Ok(StartReport::default())
    );

    assert_eq!(
        events(&log),
        vec![
            "before_spawn:points:FirstStart",
            "before_spawn:points:Respawn",
            "after_healthy:points",
            "after_healthy:points",
            "recovery_complete:points",
        ]
    );
    assert!(ctx.degraded().names().is_empty());
}

#[tokio::test]
async fn jobs_register_only_while_native_and_active() {
    let log = new_log();
    let ctx = FamilyContext::unconfigured();
    let native = Hooked::with_jobs("timer", RouteBackend::Native, &log, TICK);
    let mut native_sink = RecordingSink::default();
    assert!(job_names(&native_sink).is_empty());
    assert_eq!(
        activate_families(&[entry(native)], &ctx, &mut native_sink, soon()).await,
        Ok(StartReport::default())
    );
    assert_eq!(job_names(&native_sink), vec!["timer.tick"]);
    assert!(native.refusals().is_empty());

    let compare = Hooked::with_jobs("timer_compare", RouteBackend::Compare, &log, TICK);
    let mut compare_sink = RecordingSink::default();
    assert_eq!(
        activate_families(&[entry(compare)], &ctx, &mut compare_sink, soon()).await,
        Ok(StartReport::default())
    );
    assert_eq!(compare.refusals(), vec![JobRefused::Compare]);
    assert!(job_names(&compare_sink).is_empty());

    let proxy = Hooked::with_jobs("timer_proxy", RouteBackend::Proxy, &log, TICK);
    let mut proxy_sink = RecordingSink::default();
    assert_eq!(
        activate_families(&[entry(proxy)], &ctx, &mut proxy_sink, soon()).await,
        Ok(StartReport::default())
    );
    assert!(job_names(&proxy_sink).is_empty());
    assert_eq!(
        events(&log),
        vec!["activate:timer", "activate:timer_compare"]
    );
}

#[tokio::test]
async fn job_sink_refuses_duplicates_zero_interval_and_late_registration() {
    let mut host = HeartbeatHost::new();
    let interval = Duration::from_secs(5);

    assert!(JobSink::register_job(&mut host, "tick", interval, noop_job()).is_ok());
    assert!(
        JobSink::register_job(&mut host, "tick", interval, noop_job()).is_err(),
        "a duplicate name is refused"
    );
    assert!(
        JobSink::register_job(&mut host, "zero", Duration::ZERO, noop_job()).is_err(),
        "a zero interval is refused"
    );
    host.start().expect("host starts");
    assert!(
        JobSink::register_job(&mut host, "late", interval, noop_job()).is_err(),
        "a registration after start is refused"
    );
    host.stop().await.expect("host stops");
}

#[tokio::test]
async fn job_status_defaults_to_none() {
    let mut host = HeartbeatHost::new();
    assert!(JobSink::register_job(&mut host, "tick", Duration::from_secs(5), noop_job()).is_ok());

    assert_eq!(JobSink::status(&host, "tick"), None);
    assert_eq!(RecordingSink::default().status("tick"), None);
}

#[tokio::test]
async fn job_sink_lists_registered_jobs() {
    const TWO: &[(&str, Duration)] = &[
        ("first", Duration::from_secs(5)),
        ("second", Duration::from_secs(10)),
    ];
    let log = new_log();
    let lister = Hooked::with_jobs("lister", RouteBackend::Native, &log, TWO);
    let ctx = FamilyContext::unconfigured();
    let mut host = HeartbeatHost::new();

    assert_eq!(
        activate_families(&[entry(lister)], &ctx, &mut host, soon()).await,
        Ok(StartReport::default())
    );

    let listed: Vec<(String, Duration)> = host
        .statuses()
        .into_iter()
        .map(|(name, status)| (name, status.interval))
        .collect();
    assert_eq!(
        listed,
        vec![
            ("lister.first".to_owned(), Duration::from_secs(5)),
            ("lister.second".to_owned(), Duration::from_secs(10)),
        ]
    );
}

#[tokio::test(start_paused = true)]
async fn fatal_activation_discards_staged_jobs_and_retries_cleanly() {
    let log = new_log();
    let a = Hooked::with_jobs("a", RouteBackend::Native, &log, TICK);
    let b = Hooked::with_jobs("b", RouteBackend::Native, &log, TICK);
    b.set_activate(Step::Fatal("lease refused", true));
    let entries = [entry(a), entry(b)];
    let ctx = FamilyContext::unconfigured();
    let mut host = HeartbeatHost::new();

    let failed = activate_families(&entries, &ctx, &mut host, soon()).await;

    assert_eq!(
        failed,
        Err(StartFailure {
            family: "b",
            message: "lease refused".into(),
            relaunch_could_clear: true,
        })
    );
    assert!(
        host.statuses().is_empty(),
        "a failed pass registers nothing"
    );
    assert_eq!(
        events(&log),
        vec!["activate:a", "activate:b", "deactivate:a"]
    );

    b.set_activate(Step::Pass);
    let retried = activate_families(&entries, &ctx, &mut host, soon()).await;
    assert_eq!(retried, Ok(StartReport::default()));
    assert_eq!(job_names(&host), vec!["a.tick", "b.tick"]);

    host.start().expect("host starts");
    let both_ran = tokio::time::timeout(Duration::from_secs(30), async {
        a.ran.notified().await;
        b.ran.notified().await;
    })
    .await;
    host.stop().await.expect("host stops");
    assert!(both_ran.is_ok(), "the retried pass's jobs run");
    assert_eq!(
        (a.job_runs(), b.job_runs()),
        (1, 1),
        "only the retried pass's jobs run, once each"
    );
}

#[tokio::test(start_paused = true)]
async fn fatal_activation_unwind_shares_one_deadline() {
    let log = new_log();
    let a = Hooked::leak("a", RouteBackend::Native, &log);
    a.set_deactivate(Step::Hang);
    let b = Hooked::leak("b", RouteBackend::Native, &log);
    b.set_deactivate(Step::Hang);
    let c = Hooked::leak("c", RouteBackend::Native, &log);
    c.set_activate(Step::Fatal("lease refused", false));
    let ctx = FamilyContext::unconfigured();
    let mut sink = RecordingSink::default();
    let began = Instant::now();
    let budget = Duration::from_millis(100);

    let result = activate_families(
        &[entry(a), entry(b), entry(c)],
        &ctx,
        &mut sink,
        began + budget,
    )
    .await;

    assert_eq!(
        result,
        Err(StartFailure {
            family: "c",
            message: "lease refused".into(),
            relaunch_could_clear: false,
        })
    );
    assert_eq!(
        events(&log),
        vec![
            "activate:a",
            "activate:b",
            "activate:c",
            "deactivate:b",
            "deactivate:a"
        ]
    );
    assert!(
        began.elapsed() <= budget,
        "two hung unwinds took {:?}, beyond the one {budget:?} deadline",
        began.elapsed()
    );
}

#[tokio::test]
async fn ordinary_activation_error_discards_only_that_family_jobs() {
    let log = new_log();
    let a = Hooked::with_jobs("a", RouteBackend::Native, &log, TICK);
    let b = Hooked::with_jobs("b", RouteBackend::Native, &log, TICK);
    b.set_activate(Step::Ordinary("unavailable"));
    let c = Hooked::with_jobs("c", RouteBackend::Native, &log, TICK);
    let ctx = FamilyContext::unconfigured();
    let mut sink = RecordingSink::default();

    let report = activate_families(&[entry(a), entry(b), entry(c)], &ctx, &mut sink, soon()).await;

    assert_eq!(
        report,
        Ok(StartReport {
            ordinary: vec![OrdinaryFailure {
                family: "b",
                message: "unavailable".into(),
            }],
        })
    );
    assert_eq!(job_names(&sink), vec!["a.tick", "c.tick"]);
    assert!(ctx.degraded().contains("b"));
}

#[tokio::test]
async fn constructing_and_routing_a_family_runs_no_hook() {
    let log = new_log();
    let dormant = Hooked::routed("dormant", &["/dormant"], &log, TICK);
    let families: [&'static dyn RouteFamily<FrontDoorState>; 1] = [dormant];
    let state = FrontDoorState::new(common::refused_addr().await, BackendState::Down, auth());

    let table = RouteTable::new(&families, &BTreeMap::new(), &state);
    let request = Request::get("/dormant/ping")
        .body(Body::empty())
        .expect("dormant request");
    let _ = table.dispatch(&state, request).await;

    assert_eq!(dormant.routers.load(Ordering::SeqCst), 1);
    assert!(
        events(&log).is_empty(),
        "routing ran a hook: {:?}",
        events(&log)
    );
    assert_eq!(dormant.activations.load(Ordering::SeqCst), 0);
    assert_eq!(dormant.job_runs(), 0);
}

#[tokio::test]
async fn proxied_family_gets_no_hooks_or_jobs() {
    let log = new_log();
    let proxied = Hooked::with_jobs("dorm_proxy", RouteBackend::Proxy, &log, TICK);
    let native = Hooked::with_jobs("dorm_native", RouteBackend::Native, &log, TICK);
    let ctx = FamilyContext::unconfigured();
    let mut sink = RecordingSink::default();

    assert_eq!(
        activate_families(&[entry(proxied), entry(native)], &ctx, &mut sink, soon()).await,
        Ok(StartReport::default())
    );

    assert_eq!(events(&log), vec!["activate:dorm_native"]);
    assert_eq!(job_names(&sink), vec!["dorm_native.tick"]);
}

/// A Native family whose one pre-authentication row answers with the caller the
/// context reads from the request.
struct CallerProbe;

static FORGED_CLAIM: &[RouteClaim] = &[RouteClaim {
    method: None,
    path: PathMatch::Exact("/fixture/forged"),
    gate: Gate::BeforeRefusal,
    credential: Credential::Unchecked,
    peer: PeerRule::Any,
    kind: Kind::Any,
}];

impl RouteFamily<FrontDoorState> for CallerProbe {
    fn name(&self) -> &'static str {
        "caller_probe"
    }

    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }

    fn router(&self) -> Router<FrontDoorState> {
        Router::new()
    }

    fn default_backend(&self) -> RouteBackend {
        RouteBackend::Native
    }

    fn route_claims(&self) -> &'static [RouteClaim] {
        FORGED_CLAIM
    }

    fn serve_claim<'a>(
        &'a self,
        state: &'a FrontDoorState,
        _peer: SocketAddr,
        request: Request<Body>,
    ) -> BoxFut<'a, Result<Request<Body>, Response<Body>>> {
        let (parts, _body) = request.into_parts();
        let caller = state
            .context
            .caller(&parts)
            .map_or_else(|| "none".to_owned(), |caller| caller.user_id);
        Box::pin(async move { Err(Response::new(Body::from(caller))) })
    }
}

#[tokio::test]
async fn pre_auth_caller_is_none_for_forged_headers() {
    let families: [&'static dyn RouteFamily<FrontDoorState>; 1] = [&CallerProbe];
    let state = FrontDoorState::new(common::refused_addr().await, BackendState::Down, auth());
    let table = RouteTable::new(&families, &BTreeMap::new(), &state);
    let door = FrontDoor::new(state, table);
    let request = Request::get("/fixture/forged")
        .header("x-gobby-user-id", "forged-user")
        .header("x-gobby-machine-id", "forged-machine")
        .header("x-gobby-key-id", "forged-key")
        .body(Body::empty())
        .expect("forged request");

    let response = door
        .handle(request, "127.0.0.1:4000".parse().expect("peer"), false)
        .await;

    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(body_text(response).await, "none");
}
