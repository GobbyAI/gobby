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
use crate::route_family::RouteFamily;

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

    /// Stages a job for this lease. A Compare family is refused.
    pub fn register_job(
        &mut self,
        _name: &str,
        _interval: Duration,
        _run: JobRun,
    ) -> Result<(), JobRefused> {
        Ok(())
    }
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

fn unimplemented_runner() -> StartFailure {
    StartFailure {
        family: "unimplemented",
        message: String::new(),
        relaunch_could_clear: false,
    }
}

/// Runs each entry's `start` once per process, before admission. An ordinary error marks
/// the family degraded and continues. A fatal error stops the pass, deactivates the
/// families already started in reverse with `StopMode::Stop`, and returns the failure.
/// That unwind shares one deadline, `unwind_deadline`, across every hook it calls.
pub async fn start_families<S: 'static>(
    _entries: &[ActivationEntry<S>],
    _ctx: &FamilyContext,
    _unwind_deadline: Instant,
) -> Result<StartReport, StartFailure> {
    Err(unimplemented_runner())
}

/// Runs each entry's `activate` once per lease. Jobs staged during the pass reach `jobs`
/// only when the pass succeeds. A fatal error discards every staged job, deactivates the
/// families activated so far in reverse under the one `unwind_deadline`, and returns the
/// failure, so a retry registers the same names cleanly.
pub async fn activate_families<S: 'static>(
    _entries: &[ActivationEntry<S>],
    _ctx: &FamilyContext,
    _jobs: &mut (dyn JobSink + Send),
    _unwind_deadline: Instant,
) -> Result<StartReport, StartFailure> {
    Err(unimplemented_runner())
}

/// Runs each entry's `deactivate` in reverse start order and returns every failure. The
/// whole pass shares one `deadline`: each hook gets the time that remains, and a hook still
/// running at the deadline becomes a failure with the message
/// `deactivate exceeded the stop deadline`.
pub async fn deactivate_families<S: 'static>(
    _entries: &[ActivationEntry<S>],
    _mode: StopMode,
    _deadline: Instant,
) -> Vec<StopFailure> {
    Vec::new()
}

/// Runs each entry's `before_spawn` before a worker spawn.
pub async fn run_before_spawn<S: 'static>(
    _entries: &[ActivationEntry<S>],
    _ctx: &FamilyContext,
    _spawn: SpawnKind,
) -> Result<StartReport, StartFailure> {
    Err(unimplemented_runner())
}

/// Runs each entry's `after_healthy` after a healthy start or respawn.
pub async fn run_after_healthy<S: 'static>(
    _entries: &[ActivationEntry<S>],
    _ctx: &FamilyContext,
) -> Result<StartReport, StartFailure> {
    Err(unimplemented_runner())
}

/// Runs each entry's `recovery_complete` once, when recovery completes.
pub async fn run_recovery_complete<S: 'static>(
    _entries: &[ActivationEntry<S>],
    _ctx: &FamilyContext,
) -> Result<StartReport, StartFailure> {
    Err(unimplemented_runner())
}
