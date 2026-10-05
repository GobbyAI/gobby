//! Native backend ownership; deliberately not connected to production serve.

use super::{
    pid_file::PidFileClaim,
    shutdown_intent::{Intent, IntentMarker},
};
use crate::lease::ActiveDaemonLease;
use anyhow::{Context, Result, ensure};
use std::{
    collections::BTreeMap,
    ffi::OsString,
    io,
    os::fd::AsRawFd,
    path::{Path, PathBuf},
    process::{ExitStatus, Stdio},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::{
    io::AsyncReadExt,
    process::{Child, Command},
    sync::watch,
    task::JoinHandle,
    time::{Instant, interval, sleep_until, timeout},
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
}

pub struct BackendConfig {
    pub home: PathBuf,
    pub command: Vec<OsString>,
    pub environment: BTreeMap<OsString, OsString>,
    pub drain_timeout: Duration,
    pub heartbeat_interval: Duration,
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
            // Python runner_lifecycle_shutdown's overall settlement budget.
            drain_timeout: Duration::from_secs(17),
            heartbeat_interval: Duration::from_secs(1),
        })
    }
}

pub struct BackendSupervisor {
    lease: ActiveDaemonLease,
    pid_claim: Option<PidFileClaim>,
    config: BackendConfig,
    state: watch::Sender<BackendStatus>,
}

impl BackendSupervisor {
    pub fn new(lease: ActiveDaemonLease, pid_claim: PidFileClaim, config: BackendConfig) -> Self {
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
        if self.lease.fence().is_none() {
            return Ok(SupervisorExit::Standby);
        }
        let result = self.supervise(&mut shutdown).await;
        // release takes the dedicated connection before awaiting, so failures and
        // cancellation cannot retain the advisory lock. Always release the PID on
        // stop/loss/error, but retain it in the sticky refused state.
        if !matches!(result, Ok(SupervisorExit::Refused)) {
            self.pid_claim.take();
            self.state.send_replace(BackendStatus::Stopped);
        }
        let release = self.lease.release().await;
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
        let mut markers = IntentMarker::new(&self.config.home);
        let mut crashes = 0u32;
        let mut heartbeats = interval(self.config.heartbeat_interval);
        heartbeats.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            if shutdown_requested(shutdown) {
                return Ok(SupervisorExit::Stopped);
            }
            if let Err(error) = self.lease.heartbeat().await {
                eprintln!("backend lease lost: {error:#}");
                return Ok(SupervisorExit::LeaseLost);
            }
            markers.before_spawn().await?;
            if shutdown_requested(shutdown) {
                return Ok(SupervisorExit::Stopped);
            }
            let mut backend = BackendChild::spawn(&self.config)?;
            let pid = backend.child.id().context("spawned backend has no PID")?;
            self.state.send_replace(BackendStatus::Starting { pid });
            let status: ExitStatus = loop {
                tokio::select! { biased;
                    _ = shutdown_signal(shutdown) => {
                        backend.drain(self.config.drain_timeout).await?;
                        return Ok(SupervisorExit::Stopped);
                    }
                    status = backend.child.wait() => break status?,
                    _ = heartbeats.tick() => {
                        if let Err(error) = self.lease.heartbeat().await {
                            eprintln!("backend lease lost: {error:#}");
                            backend.drain(self.config.drain_timeout).await?;
                            return Ok(SupervisorExit::LeaseLost);
                        }
                    }
                }
            };
            backend.finish_stderr().await;
            if shutdown_requested(shutdown) {
                return Ok(SupervisorExit::Stopped);
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
            let now = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs_f64();
            match markers.after_exit(now).await? {
                Some(Intent::Stop) => return Ok(SupervisorExit::Stopped),
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
                        if let Err(error) = self.lease.heartbeat().await {
                            eprintln!("backend lease lost: {error:#}");
                            return Ok(SupervisorExit::LeaseLost);
                        }
                    }
                }
            }
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

struct BackendChild {
    child: Child,
    group: i32,
    _liveness: std::io::PipeWriter,
    stderr: JoinHandle<io::Result<()>>,
    last_line: watch::Receiver<String>,
}

impl BackendChild {
    fn spawn(config: &BackendConfig) -> Result<Self> {
        let (reader, writer) = std::io::pipe()?;
        let read_fd = reader.as_raw_fd();
        let executable = config.command.first().context("backend command is empty")?;
        let mut command = Command::new(executable);
        command
            .args(&config.command[1..])
            .envs(&config.environment)
            .env("GOBBY_HOME", &config.home)
            .env("HOME", &config.home)
            .env_remove("GOBBY_SINGLETON_LOCK_FD")
            .env_remove("GOBBY_PARENT_FD")
            .env("GOBBY_SUPERVISOR_FD", read_fd.to_string())
            .stdin(Stdio::null())
            .stdout(Stdio::inherit())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        command.process_group(0);
        // SAFETY: this closure runs only in the child after fork. It calls only
        // async-signal-safe fcntl and reads a captured integer. Every owned FD
        // (PID lock, lease sockets, write end) stays CLOEXEC; only this read end
        // intentionally survives exec, never made inheritable in the parent.
        unsafe {
            command.pre_exec(move || {
                let flags = libc::fcntl(read_fd, libc::F_GETFD);
                if flags < 0 || libc::fcntl(read_fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC) < 0 {
                    return Err(io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut child = command.spawn().context("spawn backend")?;
        let group = i32::try_from(child.id().context("spawned backend has no PID")?)?;
        drop(reader);
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
            group,
            _liveness: writer,
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

    async fn drain(&mut self, deadline: Duration) -> Result<()> {
        if !self.signal_owned_group(libc::SIGTERM)? {
            self.child.wait().await?;
            self.finish_stderr().await;
            return Ok(());
        }
        // Keep the group leader unreaped through the grace window: its PID
        // cannot be reused for an unrelated group even if a launcher exits
        // before its TERM-ignoring descendant. Then kill and reap the command.
        sleep_until(Instant::now() + deadline).await;
        self.signal_owned_group(libc::SIGKILL)?;
        timeout(Duration::from_secs(5), self.child.wait()).await??;
        self.finish_stderr().await;
        Ok(())
    }

    fn signal_owned_group(&mut self, signal: i32) -> io::Result<bool> {
        match signal_group(self.group, signal) {
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
}

impl Drop for BackendChild {
    fn drop(&mut self) {
        self.stderr.abort();
        if self.child.id().is_some()
            && let Err(error) = self.signal_owned_group(libc::SIGKILL)
        {
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
