//! Native backend supervision for the runtime owner.

use super::{
    pid_file::PidFileClaim,
    shutdown_intent::{ExitReadHold, Intent, IntentMarker},
    worker_channel::{
        WORKER_CHANNEL_FD_ENV, WORKER_CREDENTIAL_FD_ENV, WORKER_MARKER_ENV, WorkerAttachment,
        WorkerSpawn, attach,
    },
    worker_handshake::{GenerationSource, RUNNER_DEFAULT, RunnerMode, WorkerSession},
};
use crate::lease::{ActiveDaemonLease, LeaseLoss, NamedGuard, NamedLock};
use anyhow::{Context, Result, bail, ensure};
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use gobby_terminals::write_api::{TokenLease, TokenSlot};
use rand::RngCore;
use std::{
    collections::BTreeMap,
    ffi::OsString,
    io::{self, Write as _},
    os::fd::{AsRawFd, OwnedFd},
    path::{Path, PathBuf},
    process::{ExitStatus, Stdio},
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::{
    io::AsyncReadExt,
    process::{Child, Command},
    sync::{oneshot, watch},
    task::JoinHandle,
    time::{Instant, interval_at, sleep_until, timeout},
};

#[cfg(unix)]
mod handshake;
mod monitor;

use handshake::HandshakeStep;
use monitor::Pulse;

pub use gobby_core::family::BackendStatus;
pub use monitor::{HEARTBEAT_LOCK_LIMIT, LeaseScoped};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SupervisorExit {
    Standby,
    Stopped,
    LeaseLost,
    Refused,
    StopTerminals,
    RestartTerminals,
}

impl SupervisorExit {
    pub fn drains_terminals(self) -> bool {
        matches!(self, Self::StopTerminals | Self::RestartTerminals)
    }
}

pub struct BackendConfig {
    pub home: PathBuf,
    pub command: Vec<OsString>,
    pub environment: BTreeMap<OsString, OsString>,
    /// Handed to each backend on its own inherited pipe, never in argv or env.
    pub front_door_secret: Option<String>,
    pub drain_timeout: Duration,
    pub heartbeat_interval: Duration,
    /// Holds the running child's write token; each spawn installs a fresh one.
    pub write_token: TokenSlot,
    /// Grace a backend gets after an observed shutdown intent before it is drained.
    pub exit_grace: Duration,
    /// Cadence of the supervisor's wait reports while a backend has not exited.
    pub report_interval: Duration,
    /// Longest the supervisor waits for the lease lock at a lifetime's entry and at its release
    /// before it fails, naming the holder. A supervised heartbeat waits at most
    /// [`HEARTBEAT_LOCK_LIMIT`].
    pub lease_lock_limit: Duration,
    /// Sink for supervisor wait reports; stderr unless a caller captures them.
    pub reporter: Arc<dyn Fn(&str) + Send + Sync>,
}

impl BackendConfig {
    pub fn from_environment(home: &Path) -> Result<Self> {
        let value = match std::env::var("GOBBY_BACKEND_COMMAND") {
            Ok(value) => Some(value),
            Err(std::env::VarError::NotPresent) => None,
            Err(error) => return Err(error).context("GOBBY_BACKEND_COMMAND must be UTF-8"),
        };
        Self::from_command_json(home, value.as_deref())
    }

    pub fn from_command_json(home: &Path, command: Option<&str>) -> Result<Self> {
        let argv: Vec<String> = match command {
            Some(value) => serde_json::from_str(value)
                .context("GOBBY_BACKEND_COMMAND must be a JSON argv array")?,
            None => vec!["python".into(), "-m".into(), "gobby.runner".into()],
        };
        ensure!(
            !argv.is_empty() && !argv[0].is_empty() && argv.iter().all(|s| !s.contains('\0')),
            "GOBBY_BACKEND_COMMAND requires a nonempty executable and no NUL bytes"
        );
        Ok(Self {
            home: home.into(),
            command: argv.into_iter().map(OsString::from).collect(),
            environment: BTreeMap::new(),
            front_door_secret: None,
            // Python runner_lifecycle_shutdown's overall settlement budget.
            drain_timeout: Duration::from_secs(17),
            heartbeat_interval: Duration::from_secs(1),
            write_token: TokenSlot::default(),
            exit_grace: Duration::from_secs(60),
            report_interval: Duration::from_secs(60),
            lease_lock_limit: Duration::from_secs(120),
            reporter: Arc::new(|line: &str| crate::logging::raw_stderr(line)),
        })
    }
}

pub struct BackendSupervisor {
    lease: Option<Arc<NamedLock<ActiveDaemonLease>>>,
    /// Publishes the lease as lost without its lock; taken at each lifetime's standby check.
    loss: Option<LeaseLoss>,
    pid_claim: Option<PidFileClaim>,
    config: BackendConfig,
    state: watch::Sender<BackendStatus>,
    /// The reason the supervisor is waiting now (a lease-lock holder or the backend's exit),
    /// or None when it is not waiting. The front door reports it beside the backend state.
    wait: watch::Sender<Option<String>>,
    /// Which runner the spawned workers serve. Only Rust mode runs the handshake.
    mode: RunnerMode,
    /// Assigns the generation of each worker session this supervisor spawns.
    generations: GenerationSource,
    /// A test's hold on the marker read after each exit; production sets none.
    exit_read_hold: Option<ExitReadHold>,
}

impl BackendSupervisor {
    pub fn new(lease: ActiveDaemonLease, pid_claim: PidFileClaim, config: BackendConfig) -> Self {
        Self::with_lease(Some(Arc::new(NamedLock::new(lease))), pid_claim, config)
    }

    pub fn with_lease(
        lease: Option<Arc<NamedLock<ActiveDaemonLease>>>,
        pid_claim: PidFileClaim,
        config: BackendConfig,
    ) -> Self {
        Self::with_runner(lease, pid_claim, config, RUNNER_DEFAULT)
    }

    /// Builds a supervisor whose worker handshake runs for `mode`.
    pub fn with_runner(
        lease: Option<Arc<NamedLock<ActiveDaemonLease>>>,
        pid_claim: PidFileClaim,
        config: BackendConfig,
        mode: RunnerMode,
    ) -> Self {
        let (state, _) = watch::channel(BackendStatus::Standby);
        let (wait, _) = watch::channel(None);
        Self {
            lease,
            loss: None,
            pid_claim: Some(pid_claim),
            config,
            state,
            wait,
            mode,
            generations: GenerationSource::default(),
            exit_read_hold: None,
        }
    }

    /// The runner mode the worker handshake runs for.
    pub fn runner_mode(&self) -> RunnerMode {
        self.mode
    }

    /// The generation source for a spawned worker: Some only in Rust mode, where each spawn
    /// draws a generation and runs the handshake. Python spawns draw none.
    fn spawn_generations(&self) -> Option<&GenerationSource> {
        match self.mode {
            RunnerMode::Python => None,
            RunnerMode::Rust => Some(&self.generations),
        }
    }

    /// The number of generations this supervisor has drawn for its workers.
    #[cfg(test)]
    pub(crate) fn issued_generations(&self) -> u64 {
        self.generations.issued()
    }

    pub fn subscribe(&self) -> watch::Receiver<BackendStatus> {
        self.state.subscribe()
    }

    /// Publishes a sticky refusal without running: a start failure no relaunch clears.
    pub(crate) fn refuse(&self, reason: String) {
        self.state.send_replace(BackendStatus::Refused { reason });
    }

    /// The supervisor's current wait reason, or None when it is not waiting.
    pub fn subscribe_wait(&self) -> watch::Receiver<Option<String>> {
        self.wait.subscribe()
    }

    /// Publishes a wait reason for the front door and echoes it to the configured reporter.
    fn report(&self, line: &str) {
        self.wait.send_replace(Some(line.to_owned()));
        (self.config.reporter)(line);
    }

    /// Waits for the lease lock, publishing each wait reason and clearing it once the wait ends.
    async fn lock_lease<'a>(
        &'a self,
        lease: &'a NamedLock<ActiveDaemonLease>,
        limit: Duration,
    ) -> Result<NamedGuard<'a, ActiveDaemonLease>> {
        let started = Instant::now();
        let reporter = |line: &str| {
            self.report(line);
            self.record_progress(std::process::id(), "lease lock wait", started);
        };
        let outcome = lock_reporting(lease, self.config.report_interval, limit, &reporter).await;
        if outcome.is_ok() {
            self.wait.send_replace(None);
        }
        outcome
    }

    /// Run one ownership lifetime. Refused instances never acquire or spawn again.
    /// Dropping/cancelling this future kills its child and closes the liveness writer.
    pub async fn run(&mut self, shutdown: watch::Receiver<bool>) -> Result<SupervisorExit> {
        self.run_ending(shutdown, &mut ()).await
    }

    /// [`BackendSupervisor::run`], admitting `scoped` at the lifetime's start and ending it on
    /// the lifetime's exit (None for an error) while the lease is still held, so lease-scoped
    /// work ends before release.
    pub async fn run_ending(
        &mut self,
        mut shutdown: watch::Receiver<bool>,
        scoped: &mut impl LeaseScoped,
    ) -> Result<SupervisorExit> {
        if matches!(*self.state.borrow(), BackendStatus::Refused { .. }) {
            return Ok(SupervisorExit::Refused);
        }
        if self.pid_claim.is_none() {
            return Ok(SupervisorExit::Stopped);
        }
        if let Some(lease) = &self.lease {
            let mut guard = self.lock_lease(lease, self.config.lease_lock_limit).await?;
            if let Err(error) = guard.heartbeat().await {
                tracing::warn!(
                    target: "gobby.lifecycle",
                    "backend lease not held at lifetime start: {error:#}"
                );
                return Ok(SupervisorExit::Standby);
            }
            let loss = guard.loss();
            scoped.admit()?;
            drop(guard);
            self.loss = Some(loss);
        } else {
            scoped.admit()?;
        }
        let result = {
            let mut pulse = Pulse::new(self);
            let result = self.supervise(&mut shutdown, &mut pulse).await;
            // The lease keeps its heartbeat through the end, so a loss during the end is published.
            let exit = result.as_ref().ok().copied();
            pulse.through(exit, scoped.end(exit)).await;
            // No heartbeat is left in flight past this point, on any exit.
            if let Err(error) = pulse.settle().await {
                tracing::warn!(target: "gobby.lifecycle", "backend lease lost before release: {error:#}");
            }
            result
        };
        // Nothing verifies the lease from here to its release, so the watch must not read Active
        // through the release's lock wait. A terminal restart keeps the lease, and its watch.
        if !matches!(result, Ok(SupervisorExit::RestartTerminals))
            && let Some(loss) = &self.loss
        {
            loss.publish();
        }
        // release takes the dedicated connection before awaiting, so failures and
        // cancellation cannot retain the advisory lock. Keep the machine claim
        // through lease loss and sticky refusal; release it on stop or error.
        if !matches!(
            result,
            Ok(SupervisorExit::Refused
                | SupervisorExit::LeaseLost
                | SupervisorExit::RestartTerminals)
        ) {
            self.pid_claim.take();
            self.state.send_replace(BackendStatus::Stopped);
        }
        let release = match &self.lease {
            _ if matches!(result, Ok(SupervisorExit::RestartTerminals)) => Ok(()),
            Some(lease) => match self.lock_lease(lease, self.config.lease_lock_limit).await {
                Ok(mut guard) => guard.release().await,
                Err(error) => Err(error),
            },
            None => Ok(()),
        };
        match result {
            Err(error) => {
                if let Err(release_error) = release {
                    tracing::warn!(target: "gobby.lifecycle", "lease release: {release_error:#}");
                }
                Err(error)
            }
            Ok(outcome) => {
                release?;
                Ok(outcome)
            }
        }
    }

    async fn supervise(
        &self,
        shutdown: &mut watch::Receiver<bool>,
        pulse: &mut Pulse<'_>,
    ) -> Result<SupervisorExit> {
        ensure!(
            !self.config.heartbeat_interval.is_zero(),
            "heartbeat interval must be positive"
        );
        ensure!(
            !self.config.report_interval.is_zero(),
            "report interval must be positive"
        );
        let mut markers =
            IntentMarker::new(&self.config.home).with_hold(self.exit_read_hold.clone());
        let mut crashes = 0u32;
        loop {
            tokio::select! { biased;
                _ = shutdown_signal(shutdown) => return Ok(SupervisorExit::Stopped),
                beaten = pulse.now() => {
                    if let Err(error) = beaten {
                        tracing::warn!(target: "gobby.lifecycle", "backend lease lost: {error:#}");
                        return Ok(SupervisorExit::LeaseLost);
                    }
                }
            }
            markers.before_spawn().await?;
            if shutdown_requested(shutdown) {
                return Ok(SupervisorExit::Stopped);
            }
            let mut backend = BackendChild::spawn(&self.config, self.spawn_generations()).await?;
            let started_at = Instant::now();
            let pid = backend.child.id().context("spawned backend has no PID")?;
            self.state.send_replace(BackendStatus::Starting { pid });
            // A Rust-mode child answers its handshake before the wait loop supervises it.
            if let Some(attachment) = backend.worker.as_mut() {
                match self.await_handshake(attachment, shutdown, pulse).await {
                    HandshakeStep::Ready => {}
                    HandshakeStep::Shutdown => {
                        return self
                            .drain_for_shutdown(&mut backend, &mut markers, pulse)
                            .await;
                    }
                    HandshakeStep::LeaseLost => {
                        backend.drain(self.config.drain_timeout).await?;
                        self.state.send_replace(BackendStatus::Stopped);
                        return Ok(SupervisorExit::LeaseLost);
                    }
                    HandshakeStep::Failed(error) => {
                        // The child is ended and reaped here, so its status is known.
                        backend.kill_group()?;
                        let status = pulse.through(None, backend.child.wait()).await?;
                        pulse.through(None, backend.finish_stderr()).await;
                        if let Some(exit) = self.handle_handshake_failure(&error) {
                            return Ok(exit);
                        }
                        if status.code() == Some(78) {
                            return Ok(self.refuse_on_exit_78(&backend));
                        }
                        // Any other end is a lost reply: a crash. No intent is consulted before a
                        // handshake, so the next spawn follows the crash backoff directly.
                        self.state.send_replace(BackendStatus::Stopped);
                        let delay = crash_delay(crashes);
                        crashes = crashes.saturating_add(1);
                        self.state.send_replace(BackendStatus::Backoff { delay });
                        if let Some(exit) = pulse.back_off(delay, shutdown).await {
                            return Ok(exit);
                        }
                        continue;
                    }
                }
            }
            let mut reports = interval_at(
                Instant::now() + self.config.report_interval,
                self.config.report_interval,
            );
            reports.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            // The first intent this backend announced, and when the supervisor first saw it.
            let mut announced: Option<(Intent, Instant)> = None;
            let (status, reaped): (ExitStatus, bool) = loop {
                tokio::select! { biased;
                    _ = shutdown_signal(shutdown) => {
                        return self.drain_for_shutdown(&mut backend, &mut markers, pulse).await;
                    }
                    status = backend.child.wait() => break (status?, false),
                    beaten = pulse.next() => {
                        if let Err(error) = beaten {
                            tracing::warn!(target: "gobby.lifecycle", "backend lease lost: {error:#}");
                            backend.drain(self.config.drain_timeout).await?;
                            self.state.send_replace(BackendStatus::Stopped);
                            return Ok(SupervisorExit::LeaseLost);
                        }
                        if announced.is_none() {
                            let now = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs_f64();
                            if let Some(intent) = pulse.through(None, markers.pending(now)).await? {
                                announced = Some((intent, Instant::now()));
                            }
                        }
                        // A backend that announced its own exit gets the grace to leave on its
                        // own; a lingering one is drained, and its intent is then applied below.
                        if let Some((_, seen)) = announced
                            && seen.elapsed() >= self.config.exit_grace
                        {
                            let drain = backend.drain(self.config.drain_timeout);
                            break (pulse.through(None, drain).await?, true);
                        }
                    }
                    _ = reports.tick() => {
                        self.report_wait(
                            pid,
                            started_at,
                            announced.map(|(intent, seen)| (intent, seen.elapsed())),
                        );
                    }
                }
            };
            // The child is reaped, so it no longer holds the supervisor's wait.
            self.wait.send_replace(None);
            if !reaped {
                // The leader may exit while descendants retain sockets or stderr.
                // Retain process-group ownership through reaping, including exit 78.
                backend.kill_group()?;
                pulse.through(None, backend.finish_stderr()).await;
            }
            if started_at.elapsed() >= Duration::from_secs(30) {
                crashes = 0;
            }
            if status.code() == Some(78) {
                return Ok(self.refuse_on_exit_78(&backend));
            }
            // The child is gone: report it down until the next spawn or stop, not starting.
            self.state.send_replace(BackendStatus::Stopped);
            let now = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs_f64();
            // A heartbeat may be in flight here: the read runs beside it, never bare.
            let intent = pulse.through(None, markers.after_exit(now)).await?;
            if shutdown_requested(shutdown) {
                return Ok(
                    if matches!(
                        intent,
                        Some(Intent::StopTerminals | Intent::RestartTerminals)
                    ) {
                        SupervisorExit::StopTerminals
                    } else {
                        SupervisorExit::Stopped
                    },
                );
            }
            match intent {
                Some(Intent::Stop) => return Ok(SupervisorExit::Stopped),
                Some(Intent::StopTerminals) => return Ok(SupervisorExit::StopTerminals),
                Some(Intent::RestartTerminals) => return Ok(SupervisorExit::RestartTerminals),
                Some(Intent::Restart) => {
                    crashes = 0;
                    continue;
                }
                None => {}
            }
            let delay = crash_delay(crashes);
            crashes = crashes.saturating_add(1);
            self.state.send_replace(BackendStatus::Backoff { delay });
            if let Some(exit) = pulse.back_off(delay, shutdown).await {
                return Ok(exit);
            }
        }
    }

    /// Drains a child after a shutdown was observed, and returns the exit its intent asks for.
    async fn drain_for_shutdown(
        &self,
        backend: &mut BackendChild,
        markers: &mut IntentMarker,
        pulse: &mut Pulse<'_>,
    ) -> Result<SupervisorExit> {
        // The lease keeps its heartbeat through the drain, as through the pre-release end.
        pulse
            .through(None, backend.drain(self.config.drain_timeout))
            .await?;
        let now = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs_f64();
        if matches!(
            pulse.through(None, markers.after_exit(now)).await?,
            Some(Intent::StopTerminals | Intent::RestartTerminals)
        ) {
            return Ok(SupervisorExit::StopTerminals);
        }
        Ok(SupervisorExit::Stopped)
    }

    /// The exit for a child that ended with status 78, its own refusal. The refusal is sticky, and
    /// its reason is the child's last line.
    fn refuse_on_exit_78(&self, backend: &BackendChild) -> SupervisorExit {
        let reason = backend.last_line.borrow().clone();
        self.state.send_replace(BackendStatus::Refused {
            reason: if reason.is_empty() {
                "backend exited with status 78".into()
            } else {
                reason
            },
        });
        SupervisorExit::Refused
    }

    /// Logs what a live backend is still waited on; called once per report interval.
    fn report_wait(&self, pid: u32, started_at: Instant, announced: Option<(Intent, Duration)>) {
        let elapsed = started_at.elapsed().as_secs();
        let line = match announced {
            None => format!("backend pid {pid} has run {elapsed}s; waiting on its exit"),
            Some((intent, seen)) => format!(
                "backend pid {pid} has run {elapsed}s; waiting on its exit after announced {intent:?} (drains in {}s)",
                self.config.exit_grace.saturating_sub(seen).as_secs()
            ),
        };
        self.report(&line);
        self.record_progress(pid, "backend wait", started_at);
    }

    /// Adds a progress record to the startup timings file that the readiness bootstrap opened.
    /// Production has no such file, so nothing is written there outside that harness.
    fn record_progress(&self, pid: u32, stage: &str, started_at: Instant) {
        let timings = self.config.home.join("logs").join("startup-timings.jsonl");
        if !timings.is_file() {
            return;
        }
        let record = serde_json::json!({
            "pid": pid,
            "stage": stage,
            "state": "progress",
            "duration_seconds": started_at.elapsed().as_secs_f64(),
        });
        let written = std::fs::OpenOptions::new()
            .append(true)
            .open(&timings)
            .and_then(|mut file| writeln!(file, "{record}"));
        if let Err(error) = written {
            self.report(&format!("startup timings progress not recorded: {error}"));
        }
    }
}

/// Zero-based consecutive crash schedule: 1, 2, 4, 8, 16, 30, 30… seconds.
pub fn crash_delay(crashes: u32) -> Duration {
    Duration::from_secs((1u64 << crashes.min(5)).min(30))
}

fn shutdown_requested(shutdown: &watch::Receiver<bool>) -> bool {
    *shutdown.borrow() || shutdown.has_changed().is_err()
}

async fn shutdown_signal(shutdown: &mut watch::Receiver<bool>) {
    while !shutdown_requested(shutdown) {
        if shutdown.changed().await.is_err() {
            break;
        }
    }
}

/// Longest the supervisor waits for a backend spawn to reach exec.
const SPAWN_DEADLINE: Duration = Duration::from_secs(10);

/// Spawn on a helper thread and wait at most `deadline` for the child to exec, without blocking
/// the runtime.
///
/// `Command::spawn` returns only after the child execs: std reads an exec-status pipe until its
/// write end closes, and a forked child keeps that end open until exec. A child stalled before
/// exec therefore blocks whichever thread calls it for as long as the stall lasts. The helper
/// thread keeps that blocked call, and the async wait gives up at the deadline. A child produced
/// after the deadline is dropped on the helper thread, and `kill_on_drop` ends it.
///
/// The helper owns `read_ends`, the descriptors the child inherits, until spawn returns. A
/// `pre_exec` captures their raw numbers, so those numbers stay occupied when the caller gives up
/// at the deadline.
async fn spawn_bounded(
    read_ends: Vec<OwnedFd>,
    spawn: impl FnOnce() -> io::Result<Child> + Send + 'static,
    deadline: Duration,
) -> Result<Child> {
    let (spawned, outcome) = oneshot::channel();
    let runtime = tokio::runtime::Handle::current();
    std::thread::spawn(move || {
        let _runtime = runtime.enter();
        let _read_ends = read_ends;
        // A failed send means the caller gave up; dropping the returned child kills it.
        let _ = spawned.send(spawn());
    });
    match timeout(deadline, outcome).await {
        Ok(Ok(child)) => child.context("spawn backend"),
        Ok(Err(_)) => bail!("spawn backend thread ended early"),
        Err(_) => bail!("spawn backend did not exec within {deadline:?}"),
    }
}

fn inherited_descriptor_bound() -> io::Result<i32> {
    // Resolve the fallback bound before fork: sysconf and directory iteration
    // may allocate. Existing handles can exceed a subsequently lowered limit.
    // SAFETY: sysconf reads the current process limit and takes no pointers.
    let limit = unsafe { libc::sysconf(libc::_SC_OPEN_MAX) };
    let mut bound = i32::try_from(limit)
        .ok()
        .filter(|limit| *limit > 0)
        .ok_or_else(|| io::Error::other("cannot determine backend descriptor bound"))?;
    for entry in std::fs::read_dir("/dev/fd")? {
        if let Some(fd) = entry?
            .file_name()
            .to_str()
            .and_then(|name| name.parse::<i32>().ok())
        {
            let end = fd
                .checked_add(1)
                .ok_or_else(|| io::Error::other("descriptor bound overflow"))?;
            bound = bound.max(end);
        }
    }
    Ok(bound)
}

/// Mark inherited handles for closure at exec without closing Rust's exec-error pipe early.
///
/// # Safety
/// Call only in the fork child before exec, with a bound resolved in its parent.
unsafe fn seal_inherited_descriptors(bound: i32) -> io::Result<()> {
    #[cfg(target_os = "linux")]
    {
        // SAFETY: this syscall changes only this fork child's descriptor flags.
        if unsafe {
            libc::syscall(
                libc::SYS_close_range,
                3_u32,
                u32::MAX,
                libc::CLOSE_RANGE_CLOEXEC,
            )
        } == 0
        {
            return Ok(());
        }
        let error = io::Error::last_os_error();
        if !matches!(error.raw_os_error(), Some(libc::ENOSYS | libc::EINVAL)) {
            return Err(error);
        }
    }
    for fd in 3..bound {
        // SAFETY: fcntl is async-signal-safe and checks each integer descriptor.
        let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
        if flags < 0 {
            let error = io::Error::last_os_error();
            if error.raw_os_error() == Some(libc::EBADF) {
                continue;
            }
            return Err(error);
        }
        // SAFETY: the descriptor is open in this single-threaded fork child;
        // preserve its flags while making it close on the forthcoming exec.
        if flags & libc::FD_CLOEXEC == 0
            && unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) } < 0
        {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(())
}

struct BackendChild {
    child: Child,
    group: Option<i32>,
    _liveness: std::io::PipeWriter,
    _write_token: TokenLease,
    stderr: JoinHandle<io::Result<()>>,
    last_line: watch::Receiver<String>,
    /// The parent's attachment to the worker this child serves, or None when it serves none.
    worker: Option<WorkerAttachment>,
}

impl BackendChild {
    async fn spawn(config: &BackendConfig, generations: Option<&GenerationSource>) -> Result<Self> {
        // A Rust-mode spawn draws its generation once, here, and the attachment serves that
        // session. A source that yields no session is a spawn failure.
        let worker = match generations {
            Some(source) => {
                let session = WorkerSession::spawn(source)
                    .map_err(|error| anyhow::anyhow!("worker session: {error:?}"))?;
                Some(attach(session).context("attach the worker channel")?)
            }
            None => None,
        };
        let (worker, worker_fds, worker_raw) = match worker {
            Some(WorkerSpawn {
                attachment,
                child_fds,
                channel_fd,
                credential_fd,
            }) => (
                Some(attachment),
                child_fds,
                Some((channel_fd, credential_fd)),
            ),
            None => (None, Vec::new(), None),
        };
        let (reader, writer) = std::io::pipe()?;
        let read_fd = reader.as_raw_fd();
        // The secret rides its own pipe, filled and closed before spawn so the
        // child reads it to EOF. spawn_bounded owns its read end until spawn returns.
        let secret = match config.front_door_secret.as_deref() {
            Some(secret) => {
                let (secret_reader, mut secret_writer) = std::io::pipe()?;
                secret_writer.write_all(secret.as_bytes())?;
                Some(secret_reader)
            }
            None => None,
        };
        let secret_fd = secret.as_ref().map(AsRawFd::as_raw_fd);
        let executable = config.command.first().context("backend command is empty")?;
        let mut command = Command::new(executable);
        command
            .args(&config.command[1..])
            .envs(&config.environment)
            .env("GOBBY_HOME", &config.home)
            .env_remove("GOBBY_SINGLETON_LOCK_FD")
            .env_remove("GOBBY_PARENT_FD")
            .env("GOBBY_SUPERVISOR_FD", read_fd.to_string())
            .stdin(Stdio::null())
            .stdout(Stdio::inherit())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        command.process_group(0);
        if let Some(fd) = secret_fd {
            command.env("GOBBY_FRONT_DOOR_SECRET_FD", fd.to_string());
        }
        // The worker's descriptors and their variables are set in this one step, so a child never
        // sees the marker without the descriptors it names.
        if let Some((channel_fd, credential_fd)) = worker_raw {
            command
                .env(WORKER_MARKER_ENV, "1")
                .env(WORKER_CHANNEL_FD_ENV, channel_fd.to_string())
                .env(WORKER_CREDENTIAL_FD_ENV, credential_fd.to_string());
        }
        // The raw numbers the child must keep, captured as plain integers so that pre_exec
        // allocates nothing.
        let inherited = [
            Some(read_fd),
            secret_fd,
            worker_raw.map(|(channel, _)| channel),
            worker_raw.map(|(_, credential)| credential),
        ];
        let descriptor_bound = inherited_descriptor_bound()?;
        // SAFETY: this closure runs only in the child after fork. It calls only
        // async-signal-safe descriptor operations and reads captured integers.
        // Seal all inherited FDs; only the descriptors captured in `inherited` survive.
        unsafe {
            command.pre_exec(move || {
                seal_inherited_descriptors(descriptor_bound)?;
                for fd in inherited.into_iter().flatten() {
                    let flags = libc::fcntl(fd, libc::F_GETFD);
                    if flags < 0 || libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC) < 0 {
                        return Err(io::Error::last_os_error());
                    }
                }
                Ok(())
            });
        }
        // The helper owns every descriptor the child inherits until command.spawn returns, so the
        // raw numbers that pre_exec captures stay valid even when the caller gives up at the
        // deadline. The parent's copies of the child ends close when the helper drops them.
        let mut read_ends: Vec<OwnedFd> = vec![OwnedFd::from(reader)];
        read_ends.extend(secret.map(OwnedFd::from));
        read_ends.extend(worker_fds);
        let mut child = spawn_bounded(read_ends, move || command.spawn(), SPAWN_DEADLINE).await?;
        let group = i32::try_from(child.id().context("spawned backend has no PID")?)?;
        let mut token = [0u8; 32];
        rand::thread_rng().fill_bytes(&mut token);
        let token = URL_SAFE_NO_PAD.encode(token);
        // Installed before delivery, so the child never presents an unknown token.
        let write_token = config.write_token.install(token.clone());
        match (&writer).write_all(format!("{token}\n").as_bytes()) {
            // A child that already exited is reaped by supervision as a crash.
            Err(error) if error.kind() != io::ErrorKind::BrokenPipe => {
                return Err(error).context("deliver backend write token");
            }
            _ => {}
        }
        let mut stderr = child.stderr.take().context("backend stderr pipe missing")?;
        let (last, last_line) = watch::channel(String::new());
        let stderr = tokio::spawn(async move {
            let mut bytes = [0u8; 1024];
            let mut line = Vec::new();
            loop {
                let count = stderr.read(&mut bytes).await?;
                for byte in &bytes[..count] {
                    if *byte == b'\n' {
                        publish_line(&mut line, &last)
                    } else {
                        // Retain a bounded last line even for malformed output.
                        if line.len() == 4096 {
                            publish_line(&mut line, &last)
                        }
                        line.push(*byte);
                    }
                }
                if count == 0 {
                    publish_line(&mut line, &last);
                    return Ok(());
                }
            }
        });
        Ok(Self {
            child,
            group: Some(group),
            _liveness: writer,
            _write_token: write_token,
            stderr,
            last_line,
            worker,
        })
    }

    /// The generation of the worker session this child serves, or None when it serves none.
    #[cfg(test)]
    fn worker_generation(&self) -> Option<u64> {
        self.worker
            .as_ref()
            .map(|worker| worker.session.generation())
    }

    async fn finish_stderr(&mut self) {
        match timeout(Duration::from_millis(250), &mut self.stderr).await {
            Ok(Ok(Ok(()))) => {}
            Ok(Ok(Err(error))) => {
                tracing::warn!(target: "gobby.lifecycle", "backend stderr read: {error}");
            }
            Ok(Err(error)) => {
                tracing::warn!(target: "gobby.lifecycle", "backend stderr task: {error}");
            }
            Err(_) => self.stderr.abort(), // A descendant may retain the output pipe.
        }
    }

    async fn drain(&mut self, deadline: Duration) -> Result<ExitStatus> {
        if !self.signal_owned_group(libc::SIGTERM)? {
            self.group = None;
            let status = self.child.wait().await?;
            self.finish_stderr().await;
            return Ok(status);
        }
        // Keep the group leader unreaped through the grace window: its PID
        // cannot be reused for an unrelated group even if a launcher exits
        // before its TERM-ignoring descendant. Then kill and reap the command.
        sleep_until(Instant::now() + deadline).await;
        self.kill_group()?;
        let status = timeout(Duration::from_secs(5), self.child.wait()).await??;
        self.finish_stderr().await;
        Ok(status)
    }

    fn signal_owned_group(&mut self, signal: i32) -> io::Result<bool> {
        let Some(group) = self.group else {
            return Ok(false);
        };
        match signal_group(group, signal) {
            // Darwin reports EPERM when every group member is a zombie. Accept
            // that completion only when the leader is reaped or has exited;
            // a live leader keeps its permission error.
            Err(error)
                if cfg!(target_os = "macos")
                    && error.raw_os_error() == Some(libc::EPERM)
                    && (self.child.try_wait()?.is_some() || leader_has_exited(group)) =>
            {
                Ok(false)
            }
            result => result,
        }
    }

    fn kill_group(&mut self) -> io::Result<()> {
        self.signal_owned_group(libc::SIGKILL)?;
        self.group = None;
        Ok(())
    }
}

impl Drop for BackendChild {
    fn drop(&mut self) {
        self.stderr.abort();
        if let Err(error) = self.kill_group() {
            tracing::warn!(target: "gobby.lifecycle", "backend group cleanup: {error}");
        }
    }
}

/// Whether the kernel no longer shows `pid` as a live process while `waitpid` has not reaped it.
/// Darwin hides an exited leader from `proc_pidinfo` (ESRCH) before `waitpid` reports it, and a
/// live leader is always visible to it, so this identifies a leader that has finished exiting.
#[cfg(target_os = "macos")]
fn leader_has_exited(pid: i32) -> bool {
    // SAFETY: proc_bsdinfo is a C POD struct; proc_pidinfo receives its exact size
    // and a writable pointer that lives through the call.
    let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
    let size = std::mem::size_of::<libc::proc_bsdinfo>() as libc::c_int;
    let read =
        unsafe { libc::proc_pidinfo(pid, libc::PROC_PIDTBSDINFO, 0, (&raw mut info).cast(), size) };
    read <= 0 && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
}

#[cfg(not(target_os = "macos"))]
fn leader_has_exited(_pid: i32) -> bool {
    false
}

fn signal_group(group: i32, signal: i32) -> io::Result<bool> {
    // SAFETY: spawn creates a new group whose ID is the directly owned child PID.
    // Negative ID signals that group, never the supervisor's process group.
    if unsafe { libc::kill(-group, signal) } == 0 {
        return Ok(true);
    }
    let error = io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::ESRCH) {
        Ok(false)
    } else {
        Err(error)
    }
}

fn publish_line(line: &mut Vec<u8>, last: &watch::Sender<String>) {
    let text = String::from_utf8_lossy(line).trim().to_string();
    if !text.is_empty() {
        crate::logging::raw_stderr(&text);
        last.send_replace(text);
    }
    line.clear();
}

/// Takes `lock` for the supervisor. While another task holds it, reports through `reporter` at
/// each `interval`, and gives up once `limit` has passed, naming the holder.
async fn lock_reporting<'a, T>(
    lock: &'a NamedLock<T>,
    interval: Duration,
    limit: Duration,
    reporter: &(dyn Fn(&str) + Send + Sync),
) -> Result<NamedGuard<'a, T>> {
    let started = Instant::now();
    loop {
        let remaining = limit.saturating_sub(started.elapsed());
        if let Ok(guard) =
            timeout(interval.min(remaining), lock.lock_as("backend supervisor")).await
        {
            return Ok(guard);
        }
        // The attempt that ran out the remaining budget is the last one.
        if remaining <= interval {
            bail!(
                "backend supervisor gave up after {limit:?} waiting on the lease lock, held by {}",
                lock.holder()
            );
        }
        reporter(&format!(
            "backend supervisor waiting on the lease lock held by {}",
            lock.holder()
        ));
    }
}

#[cfg(test)]
#[path = "backend/tests.rs"]
mod tests;
