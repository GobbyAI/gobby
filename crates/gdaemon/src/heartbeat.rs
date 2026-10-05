//! Timer-driven daemon work. Event-driven loops and connection pumps stay outside this host.

use std::future::{Future, poll_fn};
use std::pin::Pin;
use std::sync::Arc;
use std::task::Poll;
use std::time::Duration;

use anyhow::{Result, bail};
use tokio::sync::oneshot;
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

struct RunningHost {
    shutdown: Option<oneshot::Sender<()>>,
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
        if self.started {
            bail!("heartbeat registration is frozen after start");
        }
        let name = name.into();
        if name.is_empty() || interval.is_zero() {
            bail!("heartbeat jobs require a name and a nonzero interval");
        }
        if self.jobs.iter().any(|job| job.name == name) {
            bail!("duplicate heartbeat job: {name}");
        }
        self.jobs.push(Job {
            name,
            interval,
            callback: Arc::new(move || Box::pin(callback())),
        });
        Ok(())
    }

    /// Start once, inside a Tokio runtime. Registration remains frozen after stop.
    pub fn start(&mut self) -> Result<()> {
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
            supervisor: tokio::spawn(supervise(jobs, stopped, now)),
        });
        self.started = true;
        Ok(())
    }

    /// Stop scheduling, drain the active child for at most five seconds, then
    /// abort and join it. Cancelling this wait leaves ownership with the host.
    pub async fn stop(&mut self) -> Result<()> {
        if let Some(running) = self.running.as_mut() {
            if let Some(shutdown) = running.shutdown.take() {
                let _ = shutdown.send(());
            }
            (&mut running.supervisor).await?;
            self.running = None;
        }
        Ok(())
    }
}

async fn supervise(
    mut jobs: Vec<(Job, Interval)>,
    mut shutdown: oneshot::Receiver<()>,
    start: Instant,
) {
    let mut base = interval_at(start + BASE_CADENCE, BASE_CADENCE);
    // Keep the base grid without replaying missed polls; each job delays its own timer.
    base.set_missed_tick_behavior(MissedTickBehavior::Skip);
    loop {
        tokio::select! {
            biased;
            _ = &mut shutdown => return,
            _ = base.tick() => {}
        }
        for (job, timer) in &mut jobs {
            match shutdown.try_recv() {
                Err(oneshot::error::TryRecvError::Empty) => {}
                _ => return,
            }
            let due = poll_fn(|cx| Poll::Ready(timer.poll_tick(cx).is_ready())).await;
            if !due {
                continue;
            }
            // Invoke the factory inside the child too, so synchronous panics are isolated.
            let callback = Arc::clone(&job.callback);
            let mut run = Run(tokio::spawn(async move { callback().await }));
            let result = tokio::select! {
                biased;
                _ = &mut shutdown => {
                    match timeout(SHUTDOWN_GRACE, &mut run.0).await {
                        Ok(result) => report(&job.name, result),
                        Err(_) => {
                            eprintln!("gdaemon heartbeat job {} exceeded shutdown grace; aborting", job.name);
                            run.0.abort();
                            let _ = (&mut run.0).await;
                        }
                    }
                    return;
                }
                result = &mut run.0 => result,
            };
            report(&job.name, result);
        }
    }
}

fn report(name: &str, result: Result<Result<()>, tokio::task::JoinError>) {
    match result {
        Ok(Ok(())) => {}
        Ok(Err(error)) => eprintln!("gdaemon heartbeat job {name} failed: {error:#}"),
        Err(error) => eprintln!("gdaemon heartbeat job {name} failed: {error}"),
    }
}
