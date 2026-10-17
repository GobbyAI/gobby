//! Authenticated, single-owner lifecycle for the persistent gterm host.
mod connect;
mod epochs;
mod process;
use std::collections::BTreeSet;
use std::future::Future;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use tokio::net::UnixStream;
use tokio::process::{Child, Command};
use tokio::sync::watch;
use tokio::task::JoinHandle;
use tokio::time::timeout;

use crate::control::{ControlClient, ControlError};
pub(crate) use connect::open_control;
use connect::{adopt_or_spawn, connect, handshake, read_pid, verify_pid};
use process::process_is_host;

pub const READY_DEADLINE: Duration = Duration::from_millis(2500);

#[derive(Debug, Clone)]
pub struct HostOptions {
    pub socket_dir: PathBuf,
    pub binary_path: PathBuf,
    pub machine_id: String,
    pub gobby_home: Option<PathBuf>,
    pub health_interval: Duration,
    pub retry_ceiling: Duration,
    pub shutdown_grace: Duration,
    pub spawn_args: Vec<String>,
}

impl HostOptions {
    /// Load the installed runtime settings without creating an observer owner.
    pub async fn from_pool(
        pool: &gobby_core::postgres_pool::Pool,
        home: &std::path::Path,
        machine_id: String,
    ) -> anyhow::Result<Option<Self>> {
        let connection = pool.get().await?;
        let rows = connection.query(
            "SELECT key, value FROM config_store WHERE key LIKE 'terminal_host.%' OR key = 'tmux.attach_history_lines'", &[],
        ).await?;
        let mut values = std::collections::BTreeMap::<String, serde_json::Value>::new();
        for row in rows {
            let key: String = row.try_get("key")?;
            let value: String = row.try_get("value")?;
            values.insert(key, serde_json::from_str(&value)?);
        }
        let enabled = values
            .get("terminal_host.enabled")
            .map_or(Some(true), serde_json::Value::as_bool)
            .ok_or_else(|| anyhow::anyhow!("terminal_host.enabled must be a boolean"))?;
        if !enabled {
            return Ok(None);
        }
        let seconds = |key: &str, default: f64, allow_zero: bool| -> anyhow::Result<Duration> {
            let value = values
                .get(key)
                .map_or(Some(default), serde_json::Value::as_f64)
                .ok_or_else(|| anyhow::anyhow!("{key} must be a number"))?;
            anyhow::ensure!(
                value.is_finite() && (value > 0.0 || allow_zero && value == 0.0),
                "invalid {key}"
            );
            Ok(Duration::try_from_secs_f64(value)?)
        };
        let path = |key: &str, default: PathBuf| -> anyhow::Result<PathBuf> {
            let Some(value) = values.get(key).filter(|value| !value.is_null()) else {
                return Ok(default);
            };
            let value = value
                .as_str()
                .ok_or_else(|| anyhow::anyhow!("{key} must be a path"))?;
            let value = gobby_core::config::resolve_env_pattern(value)?.ok_or_else(|| {
                anyhow::anyhow!("{key} contains an unresolved environment variable")
            })?;
            if value == "~/.gobby" {
                return Ok(home.to_owned());
            }
            if let Some(rest) = value.strip_prefix("~/") {
                let user_home =
                    std::env::var_os("HOME").ok_or_else(|| anyhow::anyhow!("HOME is missing"))?;
                return Ok(PathBuf::from(user_home).join(rest));
            }
            Ok(PathBuf::from(value))
        };
        let mut spawn_args = Vec::new();
        for (key, flag, default) in [
            (
                "terminal_host.tmux_poll_interval_ms",
                "--tmux-poll-interval-ms",
                150,
            ),
            (
                "terminal_host.tmux_poll_backoff_ceiling_ms",
                "--tmux-poll-backoff-ceiling-ms",
                5000,
            ),
            (
                "terminal_host.max_attached_terminals",
                "--max-attached-terminals",
                64,
            ),
            (
                "terminal_host.max_attachments_total",
                "--max-attachments-total",
                128,
            ),
            (
                "terminal_host.max_attachments_per_terminal",
                "--max-attachments-per-terminal",
                8,
            ),
            (
                "tmux.attach_history_lines",
                "--tmux-attach-history-lines",
                500,
            ),
            (
                "terminal_host.tmux_attach_history_max_bytes",
                "--tmux-attach-history-max-bytes",
                256 * 1024,
            ),
        ] {
            let value = values
                .get(key)
                .map_or(Some(default), serde_json::Value::as_u64)
                .ok_or_else(|| anyhow::anyhow!("{key} must be a nonnegative integer"))?;
            spawn_args.extend([flag.to_owned(), value.to_string()]);
        }
        Ok(Some(Self {
            socket_dir: path("terminal_host.socket_dir", home.to_owned())?,
            binary_path: path("terminal_host.binary_path", home.join("bin/gterm"))?,
            machine_id,
            gobby_home: Some(home.to_owned()),
            health_interval: seconds("terminal_host.health_interval_seconds", 5.0, false)?,
            retry_ceiling: seconds("terminal_host.restart_backoff_ceiling_seconds", 30.0, false)?,
            shutdown_grace: seconds("terminal_host.shutdown_grace_seconds", 10.0, true)?,
            spawn_args,
        }))
    }
}

impl Default for HostOptions {
    fn default() -> Self {
        Self {
            socket_dir: PathBuf::from(".gobby"),
            binary_path: PathBuf::from(".gobby/bin/gterm"),
            machine_id: String::new(),
            gobby_home: None,
            health_interval: Duration::from_secs(5),
            retry_ceiling: Duration::from_secs(30),
            shutdown_grace: Duration::from_secs(10),
            spawn_args: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EpochBinding {
    pub machine_id: String,
    pub project_id: String,
    pub host_epoch: String,
    pub state: String,
}

pub trait EpochAuthority: Send + Sync {
    fn bindings<'a>(
        &'a self,
        epoch: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<EpochBinding>, HostFailure>> + Send + 'a>>;

    fn observe_epoch<'a>(
        &'a self,
        _machine: uuid::Uuid,
        _epoch: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<(), HostFailure>> + Send + 'a>> {
        Box::pin(async { Ok(()) })
    }

    fn observe_exit<'a>(
        &'a self,
        _machine: uuid::Uuid,
        _epoch: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<(), HostFailure>> + Send + 'a>> {
        Box::pin(async { Ok(()) })
    }

    fn epoch_snapshot(
        &self,
        machine: uuid::Uuid,
    ) -> Pin<Box<dyn Future<Output = Result<EpochSnapshot, HostFailure>> + Send + '_>> {
        Box::pin(async move {
            Ok(EpochSnapshot {
                machine_id: machine,
                current_epoch: None,
                exited_epochs: BTreeSet::new(),
            })
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
/// A coherent durable view. Consumers must verify the current host is still live
/// and matches `current_epoch` before reconciling; a snapshot grants no authority
/// to settle an epoch that has not been observed exiting.
pub struct EpochSnapshot {
    pub machine_id: uuid::Uuid,
    pub current_epoch: Option<String>,
    pub exited_epochs: BTreeSet<String>,
}

pub struct PostgresEpochAuthority {
    pool: gobby_core::postgres_pool::Pool,
}

impl PostgresEpochAuthority {
    pub fn new(pool: gobby_core::postgres_pool::Pool) -> Self {
        Self { pool }
    }
}

impl EpochAuthority for PostgresEpochAuthority {
    fn observe_epoch<'a>(
        &'a self,
        machine: uuid::Uuid,
        epoch: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<(), HostFailure>> + Send + 'a>> {
        Box::pin(epochs::observe(&self.pool, machine, epoch, false))
    }

    fn observe_exit<'a>(
        &'a self,
        machine: uuid::Uuid,
        epoch: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<(), HostFailure>> + Send + 'a>> {
        Box::pin(epochs::observe(&self.pool, machine, epoch, true))
    }

    fn epoch_snapshot(
        &self,
        machine: uuid::Uuid,
    ) -> Pin<Box<dyn Future<Output = Result<EpochSnapshot, HostFailure>> + Send + '_>> {
        Box::pin(epochs::snapshot(&self.pool, machine))
    }

    fn bindings<'a>(
        &'a self,
        epoch: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<EpochBinding>, HostFailure>> + Send + 'a>> {
        Box::pin(async move {
            let client = self.pool.get().await.map_err(authority_failure)?;
            let rows = client
                .query(
                    "SELECT machine_id::text, project_id::text, host_epoch, state
                     FROM terminals WHERE backend = 'native' AND host_epoch = $1",
                    &[&epoch],
                )
                .await
                .map_err(authority_failure)?;
            rows.into_iter()
                .map(|row| {
                    Ok(EpochBinding {
                        machine_id: row.try_get(0).map_err(authority_failure)?,
                        project_id: row.try_get(1).map_err(authority_failure)?,
                        host_epoch: row.try_get(2).map_err(authority_failure)?,
                        state: row.try_get(3).map_err(authority_failure)?,
                    })
                })
                .collect()
        })
    }
}

fn authority_failure(error: impl std::fmt::Display) -> HostFailure {
    HostFailure::Authority(error.to_string())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HostInfo {
    pub pid: u32,
    pub epoch: String,
    pub version: String,
    pub adopted: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HostStatus {
    Starting,
    Ready(HostInfo),
    Unavailable(HostFailure),
    Stopped,
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum HostFailure {
    #[error("epoch_refused")]
    EpochRefused,
    #[error("token_mismatch")]
    TokenMismatch,
    #[error("pid_mismatch")]
    PidMismatch,
    #[error("protocol_mismatch")]
    ProtocolMismatch,
    #[error("authentication_failure")]
    Authentication,
    #[error("pre_adoption_io: {0}")]
    PreAdoptionIo(String),
    #[error("post_adoption_io: {0}")]
    PostAdoptionIo(String),
    #[error("gterm_missing")]
    GtermMissing,
    #[error("host_start_timeout")]
    HostStartTimeout,
    #[error("epoch_authority: {0}")]
    Authority(String),
    #[error("host_manager_stopped")]
    Stopped,
}

struct Inner {
    options: HostOptions,
    authority: Arc<dyn EpochAuthority>,
    status: watch::Sender<HostStatus>,
    stopped: AtomicBool,
    retry_armed: AtomicBool,
    task: Mutex<Option<JoinHandle<()>>>,
    last_host: Mutex<Option<HostInfo>>,
}

pub struct HostSupervisor {
    inner: Arc<Inner>,
}

impl HostSupervisor {
    pub fn new(options: HostOptions, authority: Arc<dyn EpochAuthority>) -> Self {
        let (status, _) = watch::channel(HostStatus::Starting);
        Self {
            inner: Arc::new(Inner {
                options,
                authority,
                status,
                stopped: AtomicBool::new(false),
                retry_armed: AtomicBool::new(false),
                task: Mutex::new(None),
                last_host: Mutex::new(None),
            }),
        }
    }

    pub async fn start(&self) -> Result<HostInfo, HostFailure> {
        let mut status = self.subscribe();
        {
            let mut task = self
                .inner
                .task
                .lock()
                .expect("lifecycle task lock poisoned");
            if self.inner.stopped.load(Ordering::Acquire) {
                return Err(HostFailure::Stopped);
            }
            if task.is_none() {
                self.inner.retry_armed.store(true, Ordering::Release);
                *task = Some(tokio::spawn(supervise(self.inner.clone())));
            }
        }
        timeout(READY_DEADLINE, async {
            loop {
                match status.borrow_and_update().clone() {
                    HostStatus::Ready(info) => return Ok(info),
                    HostStatus::Unavailable(error) => return Err(error),
                    HostStatus::Stopped => return Err(HostFailure::Stopped),
                    HostStatus::Starting => {}
                }
                status.changed().await.map_err(|_| HostFailure::Stopped)?;
            }
        })
        .await
        .unwrap_or(Err(HostFailure::HostStartTimeout))
    }

    pub fn retry_armed(&self) -> bool {
        self.inner.retry_armed.load(Ordering::Acquire)
    }

    pub fn subscribe(&self) -> tokio::sync::watch::Receiver<HostStatus> {
        self.inner.status.subscribe()
    }

    pub async fn epoch_snapshot(&self) -> Result<EpochSnapshot, HostFailure> {
        let machine =
            uuid::Uuid::parse_str(&self.inner.options.machine_id).map_err(authority_failure)?;
        self.inner.authority.epoch_snapshot(machine).await
    }

    pub async fn stop(&self) {
        let task = {
            let mut task = self
                .inner
                .task
                .lock()
                .expect("lifecycle task lock poisoned");
            self.inner.stopped.store(true, Ordering::Release);
            self.inner.retry_armed.store(false, Ordering::Release);
            self.inner.status.send_replace(HostStatus::Stopped);
            task.take()
        };
        if let Some(task) = task {
            task.abort();
            let _ = task.await;
        }
        // Dropping the control client and Child never kills the persistent host.
    }

    /// Only the runner's explicit terminal-drain intent calls this boundary.
    pub async fn drain(&self) -> Result<(), HostFailure> {
        // Disarm retries and close the owner before opening the drain connection.
        self.stop().await;
        // Python waits 3 * grace + 5s; reserve one second for daemon teardown.
        let budget = self
            .inner
            .options
            .shutdown_grace
            .saturating_mul(3)
            .saturating_add(Duration::from_secs(4));
        timeout(budget, async {
            if let Some(pid) = self.drain_host().await? {
                epochs::record_confirmed_exit(&self.inner, pid).await?;
            }
            Ok(())
        })
        .await
        .map_err(|_| HostFailure::PostAdoptionIo("drain deadline exceeded".into()))?
    }

    async fn drain_host(&self) -> Result<Option<u32>, HostFailure> {
        let handshake = timeout(READY_DEADLINE, async {
            let Some(stream) = connect(&self.inner.options).await? else {
                return Ok(None);
            };
            handshake(&self.inner.options, stream, true, None)
                .await
                .map(Some)
        })
        .await;
        let grace = self.inner.options.shutdown_grace;
        let grace_ms = u64::try_from(grace.as_millis()).unwrap_or(u64::MAX);
        // Explicit drain must work even when the live host cannot be adopted.
        let pid = if let Ok(Ok(Some((Some(mut client), info)))) = handshake {
            *self
                .inner
                .last_host
                .lock()
                .expect("host observation lock poisoned") = Some(info.clone());
            // A lost reply can mean the host exited. Prove the boundary next.
            let _ = timeout(grace + READY_DEADLINE, client.host_shutdown(grace_ms)).await;
            info.pid
        } else {
            match read_pid(&self.inner.options).await {
                Ok(pid) => pid,
                Err(error) => {
                    return if !tokio::fs::try_exists(
                        self.inner.options.socket_dir.join("gterm.pid"),
                    )
                    .await
                    .map_err(pre_io)?
                    {
                        Ok(None)
                    } else {
                        Err(post_adoption_failure(error))
                    };
                }
            }
        };
        if await_host_exit(pid, grace).await? {
            return Ok(Some(pid));
        }
        for signal in ["-TERM", "-KILL"] {
            // Never signal a PID whose installed host identity no longer matches.
            let identity = timeout(READY_DEADLINE, verify_pid(&self.inner.options, pid))
                .await
                .map_err(|_| {
                    HostFailure::PostAdoptionIo("drain identity deadline exceeded".into())
                })?;
            if let Err(error) = identity {
                // The host unlinks its identity before its final process grace.
                // Prove exit even if the pidfile vanished during verification.
                return if await_host_exit(pid, READY_DEADLINE).await? {
                    Ok(Some(pid))
                } else {
                    Err(post_adoption_failure(error))
                };
            }
            let result = timeout(
                READY_DEADLINE,
                Command::new("/bin/kill")
                    .args([signal, &pid.to_string()])
                    .output(),
            )
            .await
            .map_err(|_| HostFailure::PostAdoptionIo("drain signal deadline exceeded".into()))?
            .map_err(|error| HostFailure::PostAdoptionIo(error.to_string()))?;
            if !result.status.success()
                && !process_is_host(pid).await.map_err(post_adoption_failure)?
            {
                return Ok(Some(pid));
            }
            if await_host_exit(pid, grace.max(Duration::from_millis(100))).await? {
                return Ok(Some(pid));
            }
        }
        Err(HostFailure::PostAdoptionIo(format!(
            "gterm host {} still running after explicit drain",
            pid
        )))
    }
}

async fn await_host_exit(pid: u32, grace: Duration) -> Result<bool, HostFailure> {
    let deadline = tokio::time::Instant::now() + grace;
    loop {
        let alive = timeout(READY_DEADLINE, process_is_host(pid))
            .await
            .map_err(|_| {
                HostFailure::PostAdoptionIo("host exit identity deadline exceeded".into())
            })?
            .map_err(post_adoption_failure)?;
        if !alive {
            return Ok(true);
        }
        if tokio::time::Instant::now() >= deadline {
            return Ok(false);
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

impl Drop for HostSupervisor {
    fn drop(&mut self) {
        let mut task = self
            .inner
            .task
            .lock()
            .expect("lifecycle task lock poisoned");
        self.inner.stopped.store(true, Ordering::Release);
        self.inner.retry_armed.store(false, Ordering::Release);
        self.inner.status.send_replace(HostStatus::Stopped);
        if let Some(task) = task.take() {
            task.abort();
        }
    }
}

async fn supervise(inner: Arc<Inner>) {
    let mut client = None;
    let mut adopted = true;
    let mut child: Option<Child> = None;
    let mut known: Option<epochs::ObservedHost> = None;
    let mut backoff = Duration::from_secs(1);
    loop {
        if inner.stopped.load(Ordering::Acquire) {
            return;
        }
        let observed_exit = epochs::observe_process_exit(&inner, &mut known, &mut child).await;
        if observed_exit.is_ok() && known.is_none() {
            client = None;
        }
        let result = if let Err(error) = observed_exit {
            Err(error)
        } else if let Some(active) = client.as_mut() {
            timeout(READY_DEADLINE, health(active, &inner.options, adopted))
                .await
                .unwrap_or_else(|_| {
                    Err(HostFailure::PostAdoptionIo(
                        "health deadline exceeded".into(),
                    ))
                })
        } else {
            timeout(READY_DEADLINE, adopt_or_spawn(&inner, &mut child))
                .await
                .unwrap_or(Err(HostFailure::HostStartTimeout))
        };
        let result = match result {
            Ok((connected, info)) => {
                known = Some(info.clone().into());
                *inner
                    .last_host
                    .lock()
                    .expect("host observation lock poisoned") = Some(info.clone());
                match uuid::Uuid::parse_str(&inner.options.machine_id) {
                    Ok(machine) => {
                        match inner.authority.observe_epoch(machine, &info.epoch).await {
                            Ok(()) => Ok((connected, info)),
                            Err(error) => Err(error),
                        }
                    }
                    Err(error) => Err(authority_failure(error)),
                }
            }
            Err(error) => Err(error),
        };
        let delay = {
            let _task = inner.task.lock().expect("lifecycle task lock poisoned");
            if inner.stopped.load(Ordering::Acquire) {
                return;
            }
            match result {
                Ok((connected, info)) => {
                    adopted = info.adopted;
                    if let Some(connected) = connected {
                        client = Some(connected);
                    }
                    inner.status.send_replace(HostStatus::Ready(info));
                    backoff = Duration::from_secs(1);
                    inner.options.health_interval
                }
                Err(error) => {
                    client = None;
                    let semantic_refusal = error == HostFailure::EpochRefused;
                    inner.status.send_replace(HostStatus::Unavailable(error));
                    let delay = if semantic_refusal {
                        inner.options.health_interval
                    } else {
                        backoff.min(inner.options.retry_ceiling)
                    };
                    backoff = backoff.saturating_mul(2).min(inner.options.retry_ceiling);
                    delay
                }
            }
        };
        tokio::time::sleep(delay).await;
    }
}

type Adoption = (Option<ControlClient<UnixStream>>, HostInfo);

async fn health(
    client: &mut ControlClient<UnixStream>,
    options: &HostOptions,
    adopted: bool,
) -> Result<Adoption, HostFailure> {
    let ping = client
        .ping()
        .await
        .map_err(|error| control_failure(error, true))?;
    verify_pid(options, ping.host_pid)
        .await
        .map_err(post_adoption_failure)?;
    Ok((
        None,
        HostInfo {
            pid: ping.host_pid,
            epoch: ping.host_epoch,
            version: ping.version,
            adopted,
        },
    ))
}

async fn spawn(inner: &Inner) -> Result<Child, HostFailure> {
    let options = &inner.options;
    if !tokio::fs::try_exists(&options.binary_path)
        .await
        .map_err(pre_io)?
    {
        return Err(HostFailure::GtermMissing);
    }
    tokio::fs::create_dir_all(&options.socket_dir)
        .await
        .map_err(pre_io)?;
    let token_path = options.socket_dir.join("gterm-control.token");
    let mut token = tokio::fs::OpenOptions::new();
    token.write(true).create_new(true).mode(0o600);
    match token.open(token_path).await {
        Ok(mut file) => {
            use tokio::io::AsyncWriteExt;
            let value = format!(
                "{}{}",
                uuid::Uuid::new_v4().simple(),
                uuid::Uuid::new_v4().simple()
            );
            file.write_all(value.as_bytes()).await.map_err(pre_io)?;
        }
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(error) => return Err(pre_io(error)),
    }
    let logs = options.socket_dir.join("logs");
    tokio::fs::create_dir_all(&logs).await.map_err(pre_io)?;
    let log = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(logs.join("gterm.log"))
        .map_err(pre_io)?;
    let stderr = log.try_clone().map_err(pre_io)?;
    let mut command = Command::new(&options.binary_path);
    command
        .arg("host")
        .arg("--socket-dir")
        .arg(&options.socket_dir)
        .args(&options.spawn_args)
        .env_remove("TMUX")
        .env_remove("TMUX_PANE")
        .env("GTERM_LOG_FILE", logs.join("gterm.log"))
        .stdin(std::process::Stdio::null())
        .stdout(log)
        .stderr(stderr)
        .kill_on_drop(false);
    // The persistent host must not receive the daemon terminal's Ctrl-C.
    command.process_group(0);
    if let Some(home) = &options.gobby_home {
        command.env("GOBBY_HOME", home);
    }
    // Serialize the final spawn with shutdown; no guard crosses an await.
    let _task = inner.task.lock().expect("lifecycle task lock poisoned");
    if inner.stopped.load(Ordering::Acquire) {
        return Err(HostFailure::Stopped);
    }
    command.spawn().map_err(|error| {
        if error.kind() == std::io::ErrorKind::NotFound {
            HostFailure::GtermMissing
        } else {
            pre_io(error)
        }
    })
}

fn pre_io(error: std::io::Error) -> HostFailure {
    HostFailure::PreAdoptionIo(error.to_string())
}

fn post_adoption_failure(error: HostFailure) -> HostFailure {
    match error {
        HostFailure::PreAdoptionIo(message) => HostFailure::PostAdoptionIo(message),
        error => error,
    }
}

fn control_failure(error: ControlError, adopted: bool) -> HostFailure {
    match error {
        ControlError::Host { error, .. } if error == "invalid_token" => HostFailure::TokenMismatch,
        ControlError::Host { error, .. } if error == "unauthenticated" => {
            HostFailure::Authentication
        }
        ControlError::Host { error, .. } if error == "unsupported_protocol" => {
            HostFailure::ProtocolMismatch
        }
        ControlError::NotAuthenticated => HostFailure::Authentication,
        ControlError::UnsupportedProtocol { .. } => HostFailure::ProtocolMismatch,
        error if adopted => HostFailure::PostAdoptionIo(error.to_string()),
        error => HostFailure::PreAdoptionIo(error.to_string()),
    }
}
