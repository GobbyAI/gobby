//! Native backend supervision for the runtime owner.

use super::{
    pid_file::PidFileClaim,
    shutdown_intent::{Intent, IntentMarker},
};
use crate::lease::ActiveDaemonLease;
use anyhow::{Context, Result, ensure};
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use gobby_terminals::write_api::{TokenLease, TokenSlot};
use rand::RngCore;
use std::{
    collections::BTreeMap,
    ffi::OsString,
    io::{self, Write as _},
    os::fd::AsRawFd,
    path::{Path, PathBuf},
    process::{ExitStatus, Stdio},
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::{
    io::AsyncReadExt,
    process::{Child, Command},
    sync::{Mutex, watch},
    task::JoinHandle,
    time::{Instant, interval, interval_at, sleep_until, timeout},
};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BackendStatus {
    Standby,
    Starting { pid: u32 },
    Backoff { delay: Duration },
    Refused { reason: String },
    Stopped,
}

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
            reporter: Arc::new(|line: &str| eprintln!("{line}")),
        })
    }
}

pub struct BackendSupervisor {
    lease: Option<Arc<Mutex<ActiveDaemonLease>>>,
    pid_claim: Option<PidFileClaim>,
    config: BackendConfig,
    state: watch::Sender<BackendStatus>,
}

impl BackendSupervisor {
    pub fn new(lease: ActiveDaemonLease, pid_claim: PidFileClaim, config: BackendConfig) -> Self {
        Self::with_lease(Some(Arc::new(Mutex::new(lease))), pid_claim, config)
    }

    pub fn with_lease(
        lease: Option<Arc<Mutex<ActiveDaemonLease>>>,
        pid_claim: PidFileClaim,
        config: BackendConfig,
    ) -> Self {
        let (state, _) = watch::channel(BackendStatus::Standby);
        Self {
            lease,
            pid_claim: Some(pid_claim),
            config,
            state,
        }
    }

    pub fn subscribe(&self) -> watch::Receiver<BackendStatus> {
        self.state.subscribe()
    }

    /// Run one ownership lifetime. Refused instances never acquire or spawn again.
    /// Dropping/cancelling this future kills its child and closes the liveness writer.
    pub async fn run(&mut self, mut shutdown: watch::Receiver<bool>) -> Result<SupervisorExit> {
        if matches!(*self.state.borrow(), BackendStatus::Refused { .. }) {
            return Ok(SupervisorExit::Refused);
        }
        if self.pid_claim.is_none() {
            return Ok(SupervisorExit::Stopped);
        }
        if let Some(lease) = &self.lease
            && lock_reporting(lease, self.config.report_interval, &*self.config.reporter)
                .await
                .fence()
                .is_none()
        {
            return Ok(SupervisorExit::Standby);
        }
        let result = self.supervise(&mut shutdown).await;
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
            Some(lease) => {
                lock_reporting(lease, self.config.report_interval, &*self.config.reporter)
                    .await
                    .release()
                    .await
            }
            None => Ok(()),
        };
        match result {
            Err(error) => {
                if let Err(release_error) = release {
                    eprintln!("lease release: {release_error:#}");
                }
                Err(error)
            }
            Ok(outcome) => {
                release?;
                Ok(outcome)
            }
        }
    }

    async fn supervise(&mut self, shutdown: &mut watch::Receiver<bool>) -> Result<SupervisorExit> {
        ensure!(
            !self.config.heartbeat_interval.is_zero(),
            "heartbeat interval must be positive"
        );
        ensure!(
            !self.config.report_interval.is_zero(),
            "report interval must be positive"
        );
        let mut markers = IntentMarker::new(&self.config.home);
        let mut crashes = 0u32;
        let mut heartbeats = interval(self.config.heartbeat_interval);
        heartbeats.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            if shutdown_requested(shutdown) {
                return Ok(SupervisorExit::Stopped);
            }
            if let Err(error) = self.heartbeat().await {
                eprintln!("backend lease lost: {error:#}");
                return Ok(SupervisorExit::LeaseLost);
            }
            markers.before_spawn().await?;
            if shutdown_requested(shutdown) {
                return Ok(SupervisorExit::Stopped);
            }
            let mut backend = BackendChild::spawn(&self.config)?;
            let started_at = Instant::now();
            let pid = backend.child.id().context("spawned backend has no PID")?;
            self.state.send_replace(BackendStatus::Starting { pid });
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
                        backend.drain(self.config.drain_timeout).await?;
                        let now = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs_f64();
                        if matches!(markers.after_exit(now).await?, Some(Intent::StopTerminals | Intent::RestartTerminals)) {
                            return Ok(SupervisorExit::StopTerminals);
                        }
                        return Ok(SupervisorExit::Stopped);
                    }
                    status = backend.child.wait() => break (status?, false),
                    _ = heartbeats.tick() => {
                        if let Err(error) = self.heartbeat().await {
                            eprintln!("backend lease lost: {error:#}");
                            backend.drain(self.config.drain_timeout).await?;
                            self.state.send_replace(BackendStatus::Stopped);
                            return Ok(SupervisorExit::LeaseLost);
                        }
                        if announced.is_none() {
                            let now = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs_f64();
                            if let Some(intent) = markers.pending(now).await? {
                                announced = Some((intent, Instant::now()));
                            }
                        }
                        // A backend that announced its own exit gets the grace to leave on its
                        // own; a lingering one is drained, and its intent is then applied below.
                        if let Some((_, seen)) = announced
                            && seen.elapsed() >= self.config.exit_grace
                        {
                            break (backend.drain(self.config.drain_timeout).await?, true);
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
            if !reaped {
                // The leader may exit while descendants retain sockets or stderr.
                // Retain process-group ownership through reaping, including exit 78.
                backend.kill_group()?;
                backend.finish_stderr().await;
            }
            if started_at.elapsed() >= Duration::from_secs(30) {
                crashes = 0;
            }
            if status.code() == Some(78) {
                let reason = backend.last_line.borrow().clone();
                self.state.send_replace(BackendStatus::Refused {
                    reason: if reason.is_empty() {
                        "backend exited with status 78".into()
                    } else {
                        reason
                    },
                });
                return Ok(SupervisorExit::Refused);
            }
            // The child is gone: report it down until the next spawn or stop, not starting.
            self.state.send_replace(BackendStatus::Stopped);
            let now = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs_f64();
            let intent = markers.after_exit(now).await?;
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
            let deadline = Instant::now() + delay;
            loop {
                tokio::select! { biased;
                    _ = shutdown_signal(shutdown) => return Ok(SupervisorExit::Stopped),
                    _ = sleep_until(deadline) => break,
                    _ = heartbeats.tick() => {
                        if let Err(error) = self.heartbeat().await {
                            eprintln!("backend lease lost: {error:#}");
                            return Ok(SupervisorExit::LeaseLost);
                        }
                    }
                }
            }
        }
    }

    async fn heartbeat(&self) -> Result<()> {
        if let Some(lease) = &self.lease {
            lock_reporting(lease, self.config.report_interval, &*self.config.reporter)
                .await
                .heartbeat()
                .await?;
        }
        Ok(())
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
        (self.config.reporter)(&line);
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

struct BackendChild {
    child: Child,
    group: Option<i32>,
    _liveness: std::io::PipeWriter,
    _write_token: TokenLease,
    stderr: JoinHandle<io::Result<()>>,
    last_line: watch::Receiver<String>,
}

impl BackendChild {
    fn spawn(config: &BackendConfig) -> Result<Self> {
        let (reader, writer) = std::io::pipe()?;
        let read_fd = reader.as_raw_fd();
        // The secret rides its own pipe, filled and closed before spawn so the
        // child reads it to EOF. The parent drops its read end after spawn.
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
        // SAFETY: this closure runs only in the child after fork. It calls only
        // async-signal-safe fcntl and reads captured integers. Every owned FD
        // (PID lock, lease sockets, write ends) stays CLOEXEC; only the liveness
        // read end and the secret read end intentionally survive exec.
        unsafe {
            command.pre_exec(move || {
                for fd in std::iter::once(read_fd).chain(secret_fd) {
                    let flags = libc::fcntl(fd, libc::F_GETFD);
                    if flags < 0 || libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC) < 0 {
                        return Err(io::Error::last_os_error());
                    }
                }
                Ok(())
            });
        }
        let mut child = command.spawn().context("spawn backend")?;
        let group = i32::try_from(child.id().context("spawned backend has no PID")?)?;
        drop(reader);
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
        drop(secret);
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
        })
    }

    async fn finish_stderr(&mut self) {
        match timeout(Duration::from_millis(250), &mut self.stderr).await {
            Ok(Ok(Ok(()))) => {}
            Ok(Ok(Err(error))) => eprintln!("backend stderr read: {error}"),
            Ok(Err(error)) => eprintln!("backend stderr task: {error}"),
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
            // Darwin reports EPERM for an all-zombie group. Only accept that
            // completion after verifying our leader has exited; never suppress
            // a permission error while the owned leader is still running.
            Err(error)
                if cfg!(target_os = "macos")
                    && error.raw_os_error() == Some(libc::EPERM)
                    && self.child.try_wait()?.is_some() =>
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
            eprintln!("backend group cleanup: {error}");
        }
    }
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
        eprintln!("{text}");
        last.send_replace(text);
    }
    line.clear();
}

/// Takes `lock`, reporting through `reporter` at each `interval` it stays held by another task.
async fn lock_reporting<'a, T>(
    lock: &'a Mutex<T>,
    interval: Duration,
    reporter: &(dyn Fn(&str) + Send + Sync),
) -> tokio::sync::MutexGuard<'a, T> {
    loop {
        if let Ok(guard) = timeout(interval, lock.lock()).await {
            return guard;
        }
        reporter("backend supervisor waiting on the lease lock held by another daemon task");
    }
}

#[cfg(test)]
#[path = "backend/tests.rs"]
mod tests;
