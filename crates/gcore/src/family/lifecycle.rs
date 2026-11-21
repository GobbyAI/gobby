//! Lifecycle types that route families and their hook points share, and the runners that
//! call each family's hooks in start order.
//!
//! Start order is `start_rank` ascending, with equal ranks in registry order; deactivation
//! runs in the reverse of start order. Hooks run for Native and Compare entries; a Proxy
//! entry runs nothing.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use tokio::time::Instant;

use super::FamilyContext;
use crate::bootstrap::RouteBackend;
use crate::route_family::{BoxFut, RouteFamily};

/// Whether a worker spawn is the first of the process or a respawn after an exit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SpawnKind {
    /// The first spawn of the process.
    FirstStart,
    /// A spawn after the worker exited.
    Respawn,
}

/// How the front door serves a family: itself, both ways with a comparison, or by proxy.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FamilyMode {
    /// The family's native handlers answer.
    Native,
    /// The backend answers and the native handlers run beside it for comparison.
    Compare,
    /// The backend answers alone.
    Proxy,
}

/// Why the families are being deactivated.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StopMode {
    /// An ordinary stop or lease release: failures are reported and the rest continue.
    Stop,
    /// An explicit drain: the caller propagates the first failure.
    Drain,
}

/// A hook's failure.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HookError {
    /// The family runs degraded: its name is marked and the phase continues.
    Ordinary { message: String },
    /// The phase stops. `relaunch_could_clear` says whether a relaunch could clear the
    /// cause, which decides between a nonzero exit and a sticky refusal.
    Fatal {
        message: String,
        relaunch_could_clear: bool,
    },
}

/// A fatal hook failure, named by the family that returned it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StartFailure {
    pub family: &'static str,
    pub message: String,
    pub relaunch_could_clear: bool,
}

/// A deactivation failure, named by its family.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StopFailure {
    pub family: &'static str,
    pub message: String,
}

/// An ordinary hook failure, named by its family.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrdinaryFailure {
    pub family: &'static str,
    pub message: String,
}

/// What a phase that did not fail reports: its ordinary failures, in start order.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct StartReport {
    pub ordinary: Vec<OrdinaryFailure>,
}

/// A registered job's schedule and its last outcome.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JobStatus {
    pub enabled: bool,
    pub interval: Duration,
    pub last_result: Option<Result<(), String>>,
}

/// One run of a job.
pub type JobRun =
    Arc<dyn Fn() -> Pin<Box<dyn Future<Output = anyhow::Result<()>> + Send>> + Send + Sync>;

/// Where a lease's jobs are registered and scheduled.
pub trait JobSink: Send {
    /// Registers a job. A duplicate name, a zero interval and a registration after the
    /// sink started are refused.
    fn register_job(&mut self, name: &str, interval: Duration, run: JobRun) -> anyhow::Result<()>;
    /// Checks a registration against the rule [`JobSink::register_job`] applies, without
    /// registering it, so staging refuses every job the commit would refuse.
    /// `register_job` applies this same check before it registers.
    fn validate_job(&self, name: &str, interval: Duration) -> anyhow::Result<()>;
    /// Removes the registered job `name`; an unknown name is ignored. A pass whose commit
    /// failed for a family removes the jobs it had registered for that family.
    fn remove_job(&mut self, name: &str);
    /// The status of the job `name`, when the sink tracks it.
    fn status(&self, _name: &str) -> Option<JobStatus> {
        None
    }
    /// Every registered job, in registration order.
    fn statuses(&self) -> Vec<(String, JobStatus)> {
        Vec::new()
    }
}

/// Why a job registration was refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum JobRefused {
    /// Only a Native family registers jobs; a Compare family's backend keeps running its own.
    Compare,
    /// The sink refused the job, with its reason.
    Sink(String),
}

/// One family's view of an activation pass. Only the activation runner builds one.
///
/// Registrations are staged. The runner hands them to the job sink only when the whole
/// pass succeeds; a fatal failure anywhere in the pass discards every staged job, and an
/// ordinary failure discards only the failing family's.
pub struct Activation<'a> {
    ctx: &'a FamilyContext,
    mode: FamilyMode,
    family: &'static str,
    sink: &'a mut (dyn JobSink + Send),
    staged: &'a mut Vec<StagedJob>,
}

impl Activation<'_> {
    /// The process's family context.
    pub fn ctx(&self) -> &FamilyContext {
        self.ctx
    }

    /// The mode this family is activated in.
    pub fn mode(&self) -> FamilyMode {
        self.mode
    }

    /// Stages a job for this lease. A Compare family is refused. A name this pass already
    /// staged is refused, and so is every registration the sink's own rule
    /// ([`JobSink::validate_job`]) refuses, so the family sees the refusal here rather
    /// than when the pass commits.
    pub fn register_job(
        &mut self,
        name: &str,
        interval: Duration,
        run: JobRun,
    ) -> Result<(), JobRefused> {
        if self.mode != FamilyMode::Native {
            return Err(JobRefused::Compare);
        }
        if self.staged.iter().any(|job| job.name == name) {
            return Err(JobRefused::Sink(format!(
                "job {name} is already registered"
            )));
        }
        self.sink
            .validate_job(name, interval)
            .map_err(|error| JobRefused::Sink(format!("{error:#}")))?;
        self.staged.push(StagedJob {
            family: self.family,
            name: name.to_owned(),
            interval,
            run,
        });
        Ok(())
    }
}

/// One job staged during an activation pass.
struct StagedJob {
    family: &'static str,
    name: String,
    interval: Duration,
    run: JobRun,
}

/// One family and the backend its route resolved to.
pub struct ActivationEntry<S: 'static> {
    pub family: &'static dyn RouteFamily<S>,
    pub backend: RouteBackend,
}

impl<S: 'static> Clone for ActivationEntry<S> {
    fn clone(&self) -> Self {
        *self
    }
}

impl<S: 'static> Copy for ActivationEntry<S> {}

/// The message of a deactivation that was still running at the stop deadline.
const STOP_DEADLINE_EXCEEDED: &str = "deactivate exceeded the stop deadline";

fn mode_of(backend: RouteBackend) -> FamilyMode {
    match backend {
        RouteBackend::Native => FamilyMode::Native,
        RouteBackend::Compare => FamilyMode::Compare,
        RouteBackend::Proxy => FamilyMode::Proxy,
    }
}

/// The entries whose hooks run, in start order: Proxy entries dropped, then a stable sort
/// on `start_rank`, so equal ranks keep their registry order.
fn start_order<S: 'static>(entries: &[ActivationEntry<S>]) -> Vec<ActivationEntry<S>> {
    let mut ordered: Vec<_> = entries
        .iter()
        .copied()
        .filter(|entry| entry.backend != RouteBackend::Proxy)
        .collect();
    ordered.sort_by_key(|entry| entry.family.start_rank());
    ordered
}

fn record_ordinary(
    ctx: &FamilyContext,
    report: &mut StartReport,
    family: &'static str,
    message: String,
) {
    ctx.degraded().mark(family);
    report.ordinary.push(OrdinaryFailure { family, message });
}

/// How one pass of a hook over the families ended.
struct Pass<S: 'static> {
    /// The entries whose hook was called, in start order. After a fatal error the failing
    /// entry is the last one, since its hook may have acquired part of what it holds.
    ran: Vec<ActivationEntry<S>>,
    report: StartReport,
    fatal: Option<StartFailure>,
}

/// Calls `hook` for each entry in start order. An ordinary error marks the family and
/// continues; a fatal error ends the pass.
async fn run_pass<'h, S: 'static, H>(
    entries: &[ActivationEntry<S>],
    ctx: &FamilyContext,
    hook: H,
) -> Pass<S>
where
    H: Fn(&'static dyn RouteFamily<S>) -> BoxFut<'h, Result<(), HookError>>,
{
    let mut pass = Pass {
        ran: Vec::new(),
        report: StartReport::default(),
        fatal: None,
    };
    for entry in start_order(entries) {
        let family = entry.family;
        let outcome = hook(family).await;
        pass.ran.push(entry);
        match outcome {
            Ok(()) => {}
            Err(HookError::Ordinary { message }) => {
                record_ordinary(ctx, &mut pass.report, family.name(), message);
            }
            Err(HookError::Fatal {
                message,
                relaunch_could_clear,
            }) => {
                pass.fatal = Some(StartFailure {
                    family: family.name(),
                    message,
                    relaunch_could_clear,
                });
                return pass;
            }
        }
    }
    pass
}

fn pass_result<S: 'static>(pass: Pass<S>) -> Result<StartReport, StartFailure> {
    match pass.fatal {
        Some(failure) => Err(failure),
        None => Ok(pass.report),
    }
}

/// Runs each entry's `start` once per process, before admission. An ordinary error marks
/// the family degraded and continues. A fatal error stops the pass, deactivates in reverse
/// with `StopMode::Stop` the failing family first (its `start` may have acquired part of
/// what it holds) and then the families already started, and returns the failure.
///
/// That unwind shares one deadline across every hook it calls, `unwind_budget` from the
/// moment the unwind begins, so time spent in a slow `start` cannot exhaust the cleanup.
/// A family whose `start` failed in the ordinary way counts as started. The unwind's own
/// failures are not reported: the fatal failure is what the caller acts on.
pub async fn start_families<S: 'static>(
    entries: &[ActivationEntry<S>],
    ctx: &FamilyContext,
    unwind_budget: Duration,
) -> Result<StartReport, StartFailure> {
    let pass = run_pass(entries, ctx, |family| family.start(ctx)).await;
    if pass.fatal.is_some() {
        let deadline = Instant::now() + unwind_budget;
        deactivate_families(&pass.ran, StopMode::Stop, deadline).await;
    }
    pass_result(pass)
}

/// Runs each entry's `activate` once per lease. Jobs staged during the pass reach `jobs`
/// only when the pass succeeds. A fatal error discards every staged job, deactivates in
/// reverse the failing family first and then the families activated so far, and returns
/// the failure, so a retry registers the same names cleanly. A family whose `activate`
/// failed in the ordinary way counts as activated.
///
/// That unwind shares one deadline across every hook it calls, `unwind_budget` from the
/// moment the unwind begins, so time spent in a slow `activate` cannot exhaust the cleanup.
///
/// Staging applies the sink's own rule, so a family sees a refusal when it stages the
/// job. An ordinary error discards only that family's staged jobs and marks its name. If
/// the sink still refuses a job when the pass commits, that is one ordinary failure of the
/// family that staged it: the jobs this pass registered for that family are removed and
/// its remaining staged jobs are not registered.
pub async fn activate_families<S: 'static>(
    entries: &[ActivationEntry<S>],
    ctx: &FamilyContext,
    jobs: &mut (dyn JobSink + Send),
    unwind_budget: Duration,
) -> Result<StartReport, StartFailure> {
    let mut staged = Vec::new();
    let mut activated = Vec::new();
    let mut report = StartReport::default();
    for entry in start_order(entries) {
        let family = entry.family;
        let first_staged = staged.len();
        let mut activation = Activation {
            ctx,
            mode: mode_of(entry.backend),
            family: family.name(),
            sink: &mut *jobs,
            staged: &mut staged,
        };
        let outcome = family.activate(&mut activation).await;
        activated.push(entry);
        match outcome {
            Ok(()) => {}
            Err(HookError::Ordinary { message }) => {
                staged.truncate(first_staged);
                record_ordinary(ctx, &mut report, family.name(), message);
            }
            Err(HookError::Fatal {
                message,
                relaunch_could_clear,
            }) => {
                drop(staged);
                let deadline = Instant::now() + unwind_budget;
                deactivate_families(&activated, StopMode::Stop, deadline).await;
                return Err(StartFailure {
                    family: family.name(),
                    message,
                    relaunch_could_clear,
                });
            }
        }
    }
    commit_staged(staged, ctx, jobs, &mut report);
    Ok(report)
}

/// Hands the staged jobs to the sink in staging order. The first refusal for a family
/// removes the jobs already registered for it and skips the rest of its staged jobs.
fn commit_staged(
    staged: Vec<StagedJob>,
    ctx: &FamilyContext,
    jobs: &mut (dyn JobSink + Send),
    report: &mut StartReport,
) {
    let mut registered: Vec<(&'static str, String)> = Vec::new();
    let mut refused: Vec<&'static str> = Vec::new();
    for job in staged {
        if refused.contains(&job.family) {
            continue;
        }
        match jobs.register_job(&job.name, job.interval, job.run) {
            Ok(()) => registered.push((job.family, job.name)),
            Err(error) => {
                refused.push(job.family);
                for (_, name) in registered
                    .iter()
                    .filter(|(family, _)| *family == job.family)
                {
                    jobs.remove_job(name);
                }
                record_ordinary(
                    ctx,
                    report,
                    job.family,
                    format!("job {}: {error:#}", job.name),
                );
            }
        }
    }
}

/// Runs each entry's `deactivate` in reverse start order and returns every failure. The
/// whole pass shares one `deadline`: each hook gets the time that remains, and a hook still
/// running at the deadline becomes a failure with the message
/// `deactivate exceeded the stop deadline`. Every hook is called, even once the deadline
/// has passed. Under `StopMode::Drain` the caller propagates the first failure.
pub async fn deactivate_families<S: 'static>(
    entries: &[ActivationEntry<S>],
    mode: StopMode,
    deadline: Instant,
) -> Vec<StopFailure> {
    let mut failures = Vec::new();
    for entry in start_order(entries).into_iter().rev() {
        let family = entry.family;
        match tokio::time::timeout_at(deadline, family.deactivate(mode)).await {
            Ok(Ok(())) => {}
            Ok(Err(failure)) => failures.push(failure),
            Err(_elapsed) => failures.push(StopFailure {
                family: family.name(),
                message: STOP_DEADLINE_EXCEEDED.to_owned(),
            }),
        }
    }
    failures
}

/// Runs each entry's `before_spawn` before a worker spawn, with the error forms of
/// `start`: an ordinary error marks the family and continues, and a fatal error stops the
/// pass and is returned. Nothing is unwound; the caller acts on the failure.
pub async fn run_before_spawn<S: 'static>(
    entries: &[ActivationEntry<S>],
    ctx: &FamilyContext,
    spawn: SpawnKind,
) -> Result<StartReport, StartFailure> {
    pass_result(run_pass(entries, ctx, |family| family.before_spawn(ctx, spawn)).await)
}

/// Runs each entry's `after_healthy` after a healthy start or respawn, with the error
/// forms of [`run_before_spawn`].
pub async fn run_after_healthy<S: 'static>(
    entries: &[ActivationEntry<S>],
    ctx: &FamilyContext,
) -> Result<StartReport, StartFailure> {
    pass_result(run_pass(entries, ctx, |family| family.after_healthy(ctx)).await)
}

/// Runs each entry's `recovery_complete` once, when recovery completes, with the error
/// forms of [`run_before_spawn`].
pub async fn run_recovery_complete<S: 'static>(
    entries: &[ActivationEntry<S>],
    ctx: &FamilyContext,
) -> Result<StartReport, StartFailure> {
    pass_result(run_pass(entries, ctx, |family| family.recovery_complete(ctx)).await)
}
