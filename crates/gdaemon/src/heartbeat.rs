//! Timer-driven daemon work. Event-driven loops and connection pumps stay outside this host.

use std::future::{Future, poll_fn};
use std::pin::Pin;
use std::sync::Arc;
use std::task::Poll;
use std::time::Duration;

use anyhow::{Result, bail};
use gobby_core::family::LeaseState;
use gobby_core::family::lifecycle::{JobRun, JobSink, JobStatus};
use tokio::sync::{oneshot, watch};
use tokio::task::JoinHandle;
use tokio::time::{Instant, Interval, MissedTickBehavior, interval_at, timeout};

pub const BASE_CADENCE: Duration = Duration::from_secs(5);
pub const SHUTDOWN_GRACE: Duration = Duration::from_secs(5);

type Callback = Arc<dyn Fn() -> Pin<Box<dyn Future<Output = Result<()>> + Send>> + Send + Sync>;

struct Job {
    name: String,
    interval: Duration,
    callback: Callback,
}

/// How the host's owner ends the jobs.
#[derive(Clone, Copy)]
enum Stop {
    /// The active child gets [`SHUTDOWN_GRACE`] to finish.
    Drain,
    /// The active child is aborted at once.
    Cancel,
}

struct RunningHost {
    shutdown: Option<oneshot::Sender<Stop>>,
    supervisor: JoinHandle<()>,
}

impl Drop for RunningHost {
    fn drop(&mut self) {
        // Dropping the owner must not detach either the supervisor or its active job.
        self.supervisor.abort();
    }
}

struct Run(JoinHandle<Result<()>>);

impl Drop for Run {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// Register jobs before starting. First runs become due after their interval;
/// the base tick rounds execution up to the next five-second boundary.
/// Jobs run sequentially, with no catch-up bursts or overlapping executions.
#[derive(Default)]
pub struct HeartbeatHost {
    jobs: Vec<Job>,
    /// Every registered job's name and interval, in registration order. It outlives
    /// `start`, which takes the jobs themselves.
    registered: Vec<(String, Duration)>,
    started: bool,
    running: Option<RunningHost>,
}

impl HeartbeatHost {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register<F, Fut>(
        &mut self,
        name: impl Into<String>,
        interval: Duration,
        callback: F,
    ) -> Result<()>
    where
        F: Fn() -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<()>> + Send + 'static,
    {
        self.push(
            name.into(),
            interval,
            Arc::new(move || Box::pin(callback())),
        )
    }

    /// The one registration rule: no registration after start, a name and a nonzero
    /// interval, and no name registered twice.
    fn check(&self, name: &str, interval: Duration) -> Result<()> {
        if self.started {
            bail!("heartbeat registration is frozen after start");
        }
        if name.is_empty() || interval.is_zero() {
            bail!("heartbeat jobs require a name and a nonzero interval");
        }
        if self.registered.iter().any(|(existing, _)| existing == name) {
            bail!("duplicate heartbeat job: {name}");
        }
        Ok(())
    }

    fn push(&mut self, name: String, interval: Duration, callback: Callback) -> Result<()> {
        self.check(&name, interval)?;
        self.registered.push((name.clone(), interval));
        self.jobs.push(Job {
            name,
            interval,
            callback,
        });
        Ok(())
    }

    /// Start once, inside a Tokio runtime. Registration remains frozen after stop.
    pub fn start(&mut self) -> Result<()> {
        self.start_with(None)
    }

    /// Starts once, like [`HeartbeatHost::start`], fenced by `lease`: once the lease is no
    /// longer held (or its sender is gone), the running job is cancelled at once and no job
    /// runs again. The fence takes precedence over a stop and is watched through its drain.
    pub fn start_fenced(&mut self, lease: watch::Receiver<LeaseState>) -> Result<()> {
        self.start_with(Some(lease))
    }

    fn start_with(&mut self, lease: Option<watch::Receiver<LeaseState>>) -> Result<()> {
        if self.started {
            bail!("heartbeat host has already started");
        }
        let now = Instant::now();
        let jobs = std::mem::take(&mut self.jobs)
            .into_iter()
            .map(|job| {
                let mut timer = interval_at(now + job.interval, job.interval);
                timer.set_missed_tick_behavior(MissedTickBehavior::Delay);
                (job, timer)
            })
            .collect();
        let (shutdown, stopped) = oneshot::channel();
        self.running = Some(RunningHost {
            shutdown: Some(shutdown),
            supervisor: tokio::spawn(supervise(jobs, stopped, now, lease)),
        });
        self.started = true;
        Ok(())
    }

    /// Stop scheduling, drain the active child for at most five seconds, then
    /// abort and join it. Cancelling this wait leaves ownership with the host.
    pub async fn stop(&mut self) -> Result<()> {
        self.end(Stop::Drain).await
    }

    /// Stop scheduling and abort and join the active child at once, with no grace: for
    /// work that must not outlive a lease that is already lost.
    pub async fn cancel(&mut self) -> Result<()> {
        self.end(Stop::Cancel).await
    }

    async fn end(&mut self, stop: Stop) -> Result<()> {
        if let Some(running) = self.running.as_mut() {
            if let Some(shutdown) = running.shutdown.take() {
                // A supervisor the fence already ended has dropped its receiver.
                let _ = shutdown.send(stop);
            }
            let result = (&mut running.supervisor).await;
            self.running = None;
            result?;
        }
        Ok(())
    }
}

/// Family jobs reach the host through the job sink, with the registration rules of
/// [`HeartbeatHost::register`]. Starting and stopping stay on the host.
impl JobSink for HeartbeatHost {
    fn register_job(&mut self, name: &str, interval: Duration, run: JobRun) -> Result<()> {
        self.push(name.to_owned(), interval, run)
    }

    fn validate_job(&self, name: &str, interval: Duration) -> Result<()> {
        self.check(name, interval)
    }

    /// Removal is frozen after start with registration, so a started host ignores it.
    fn remove_job(&mut self, name: &str) {
        if self.started {
            return;
        }
        self.registered.retain(|(existing, _)| existing != name);
        self.jobs.retain(|job| job.name != name);
    }

    fn statuses(&self) -> Vec<(String, JobStatus)> {
        self.registered
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

async fn supervise(
    mut jobs: Vec<(Job, Interval)>,
    mut shutdown: oneshot::Receiver<Stop>,
    start: Instant,
    mut lease: Option<watch::Receiver<LeaseState>>,
) {
    let mut base = interval_at(start + BASE_CADENCE, BASE_CADENCE);
    // Keep the base grid without replaying missed polls; each job delays its own timer.
    base.set_missed_tick_behavior(MissedTickBehavior::Skip);
    loop {
        tokio::select! {
            biased;
            () = lease_ended(&mut lease) => return,
            _ = &mut shutdown => return,
            _ = base.tick() => {}
        }
        for (job, timer) in &mut jobs {
            match shutdown.try_recv() {
                Err(oneshot::error::TryRecvError::Empty) => {}
                _ => return,
            }
            if !lease_held(lease.as_ref()) {
                return;
            }
            let due = poll_fn(|cx| Poll::Ready(timer.poll_tick(cx).is_ready())).await;
            if !due {
                continue;
            }
            // Invoke the factory inside the child too, so synchronous panics are isolated.
            let callback = Arc::clone(&job.callback);
            let mut run = Run(tokio::spawn(async move { callback().await }));
            // Lease loss wins over shutdown: a loss already ready when the stop arrives
            // cancels the job instead of draining it.
            let result = tokio::select! {
                biased;
                () = lease_ended(&mut lease) => {
                    cancel_for_lost_lease(&job.name, &mut run).await;
                    return;
                }
                stop = &mut shutdown => {
                    // A sender dropped without a stop ends the jobs as a stop does.
                    match stop.unwrap_or(Stop::Drain) {
                        Stop::Drain => drain(&job.name, &mut run, &mut lease).await,
                        Stop::Cancel => abort(&mut run).await,
                    }
                    return;
                }
                result = &mut run.0 => result,
            };
            report(&job.name, result);
        }
    }
}

/// Whether `lease`, when the host is fenced by one, is still held.
fn lease_held(lease: Option<&watch::Receiver<LeaseState>>) -> bool {
    lease.is_none_or(|lease| *lease.borrow() == LeaseState::Active && lease.has_changed().is_ok())
}

/// Resolves once the fencing lease is no longer held or its sender is gone; never for an
/// unfenced host.
async fn lease_ended(lease: &mut Option<watch::Receiver<LeaseState>>) {
    match lease {
        Some(lease) => {
            // A closed channel is an end of the lease too.
            let _ = lease.wait_for(|state| *state != LeaseState::Active).await;
        }
        None => std::future::pending().await,
    }
}

/// Gives the active child [`SHUTDOWN_GRACE`] to finish, then aborts and joins it. The
/// fencing lease stays watched through the grace: once it is no longer held, the child is
/// aborted and joined at once.
async fn drain(name: &str, run: &mut Run, lease: &mut Option<watch::Receiver<LeaseState>>) {
    let finished = tokio::select! {
        biased;
        () = lease_ended(lease) => None,
        finished = timeout(SHUTDOWN_GRACE, &mut run.0) => Some(finished),
    };
    match finished {
        None => cancel_for_lost_lease(name, run).await,
        Some(Ok(result)) => report(name, result),
        Some(Err(_)) => {
            tracing::warn!(
                target: "gobby.heartbeat",
                "gdaemon heartbeat job {name} exceeded shutdown grace; aborting"
            );
            abort(run).await;
        }
    }
}

/// Aborts and joins the active child because the fencing lease is no longer held.
async fn cancel_for_lost_lease(name: &str, run: &mut Run) {
    tracing::warn!(
        target: "gobby.heartbeat",
        "gdaemon heartbeat job {name} cancelled: the lease is no longer held"
    );
    abort(run).await;
}

/// Aborts the active child and joins it.
async fn abort(run: &mut Run) {
    run.0.abort();
    // The join reports the cancellation this abort caused.
    let _ = (&mut run.0).await;
}

fn report(name: &str, result: Result<Result<()>, tokio::task::JoinError>) {
    match result {
        Ok(Ok(())) => {}
        Ok(Err(error)) => tracing::warn!(
            target: "gobby.heartbeat",
            "gdaemon heartbeat job {name} failed: {error:#}"
        ),
        Err(error) => tracing::warn!(
            target: "gobby.heartbeat",
            "gdaemon heartbeat job {name} failed: {error}"
        ),
    }
}
