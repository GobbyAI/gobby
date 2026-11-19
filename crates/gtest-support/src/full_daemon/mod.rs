//! Full-daemon harness: gdaemon and its Python worker on temporary ports,
//! against an isolated per-run database. Process control lives in `process`; the detached gterm
//! host lives in `terminal_host`.

use std::fmt;
#[cfg(unix)]
use std::fs::Permissions;
use std::fs::{self, File};
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
#[cfg(unix)]
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

use serde_json::json;
use tempfile::TempDir;

use crate::database::{DatabaseError, RunDatabase, start_run_database};
use crate::http_sender::get_status;
use crate::hub_guard::{GuardMode, HubGuardError, check_test_hub};
use crate::loopback::{self, LoopbackError, LoopbackGuard};
use crate::ports::{DaemonPorts, daemon_ports};

mod process;
#[cfg(all(test, unix))]
mod teardown_tests;
mod terminal_host;

pub use process::{GroupWatch, pid_alive};
use process::{Identity, stop_daemon, stop_worker};
pub use terminal_host::processes_in_run;
use terminal_host::{
    ensure_gterm, group_snapshot, host_is_live, install_gterm, live_terminal_hosts, read_host_pid,
    stop_strays, stop_terminal_host, terminal_host_children, worker_of,
};

/// The readiness URL path the daemon must answer 200 on.
const READY_PATH: &str = "/api/auth/status";

/// The line a database-backed test prints when `DATABASE_URL` is unset.
pub const SKIP_LINE: &str =
    "skipped: DATABASE_URL is unset (P0-HARNESS needs the isolated test hub)";

/// The machine the isolated daemon is pinned to (tests/e2e/conftest.py:583).
const MACHINE_ID: &str = "21000000-0000-4000-8000-000000000002";
/// Ready ceiling, the e2e fixture's (tests/e2e/conftest.py:70-71).
const READY_CEILING: Duration = Duration::from_secs(300);
/// Grace for gdaemon after SIGTERM.
const DAEMON_GRACE: Duration = Duration::from_secs(30);
/// Grace for the Python worker after SIGTERM.
const WORKER_GRACE: Duration = Duration::from_secs(10);
const POLL: Duration = Duration::from_millis(100);

/// Why `start` refuses on Windows. The guard runs first, so a refused URL still gets its rule.
pub const WINDOWS_UNBUILT: &str = "the full-daemon harness has no process control on Windows yet: plain std cannot place a daemon in a process group or walk its children (the Windows mechanism is a found line for the post-port smoke test, H47)";

/// The PATH the child tools run under. Windows has no fixed system PATH the harness trusts, so it
/// is empty there, and `start` refuses before any child runs.
#[cfg(unix)]
const SYSTEM_PATH: &str = "/usr/bin:/bin:/usr/sbin:/sbin";
#[cfg(windows)]
const SYSTEM_PATH: &str = "";

/// The test hub URL from `lookup`, or the skip line printed and `None` returned.
pub fn test_hub_url_or_skip(lookup: &dyn Fn(&str) -> Option<String>) -> Option<String> {
    match lookup("DATABASE_URL") {
        Some(url) if !url.trim().is_empty() => Some(url),
        _ => {
            println!("{SKIP_LINE}");
            None
        }
    }
}

/// A failure to start, operate or stop the full daemon.
#[derive(Debug)]
pub enum FullDaemonError {
    /// The DATABASE_URL guard or a rehearsal profile refused the launch.
    Guard(HubGuardError),
    /// The per-run database could not be created, dropped or swept.
    Database(DatabaseError),
    /// A required path is missing: the gdaemon binary, the checkout's interpreter.
    Missing(String),
    /// A filesystem or process operation failed before the daemon was ready.
    Io(String),
    /// The schema apply step failed.
    Schema(String),
    /// The pre-start seed step failed, or none was given.
    Seed(String),
    /// Readiness was not reached, or the child exited first.
    Ready(String),
    /// A start failed after gdaemon was spawned. Teardown ran before this was returned, so the
    /// pids are dead unless `teardown` names the step that failed.
    FailedStart {
        /// Why the start failed.
        reason: String,
        /// gdaemon's pid.
        daemon_pid: u32,
        /// The Python worker's pid, when it was running at the failure.
        worker_pid: Option<u32>,
        /// The teardown error, when teardown itself failed.
        teardown: Option<String>,
    },
    /// A teardown step failed.
    Teardown(String),
    /// The platform has no mechanism for this harness yet (the harness refuses, never skips).
    Unsupported(String),
}

impl fmt::Display for FullDaemonError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Guard(error) => write!(f, "{error}"),
            Self::Database(error) => write!(f, "database: {error}"),
            Self::Missing(detail) => write!(f, "missing: {detail}"),
            Self::Io(detail) => write!(f, "io: {detail}"),
            Self::Schema(detail) => write!(f, "schema: {detail}"),
            Self::Seed(detail) => write!(f, "seed: {detail}"),
            Self::Ready(detail) => write!(f, "ready: {detail}"),
            Self::FailedStart {
                reason,
                daemon_pid,
                worker_pid,
                teardown,
            } => write!(
                f,
                "start failed ({reason}); gdaemon pid {daemon_pid}, worker {worker_pid:?}, teardown {teardown:?}"
            ),
            Self::Teardown(detail) => write!(f, "teardown: {detail}"),
            Self::Unsupported(detail) => write!(f, "unsupported: {detail}"),
        }
    }
}

impl std::error::Error for FullDaemonError {}

impl From<HubGuardError> for FullDaemonError {
    fn from(error: HubGuardError) -> Self {
        Self::Guard(error)
    }
}

impl From<DatabaseError> for FullDaemonError {
    fn from(error: DatabaseError) -> Self {
        Self::Database(error)
    }
}

/// A stop or drop step, in the order teardown performs them.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TeardownStep {
    /// gdaemon's process group was signalled and exited.
    DaemonStopped,
    /// The Python worker's process group was signalled and exited.
    WorkerStopped,
    /// The detached gterm host that gdaemon started was stopped. A host outlives its daemon by
    /// design, so teardown stops it (tests/e2e/conftest.py:949-958 does the same).
    TerminalHostStopped,
    /// The per-run database was dropped WITH FORCE, after every process stop.
    DatabaseDropped,
    /// The temporary root was removed.
    HomeRemoved,
}

/// What teardown did, in order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TeardownReport {
    /// The completed steps, in the order they ran.
    pub steps: Vec<TeardownStep>,
    /// Whether gdaemon or the worker was still alive when the database was dropped.
    pub processes_alive_at_drop: bool,
}

/// What a pre-start seed step receives: the per-run database URL, the temporary home, the
/// project directory, the checkout's interpreter and the repository root.
#[derive(Debug, Clone, Copy)]
pub struct SeedContext<'a> {
    /// The URL the schema apply, the seed and the daemon all use.
    pub database_url: &'a str,
    /// The temporary home the daemon reads its bootstrap and config from.
    pub home: &'a Path,
    /// The project checkout the daemon runs in, empty when the step starts.
    pub project_dir: &'a Path,
    /// The checkout's interpreter (`.venv/bin/python`, or `.venv/Scripts/python.exe` on Windows).
    pub python: &'a Path,
    /// The repository root, whose `src` is the Python source root.
    pub repo_root: &'a Path,
    /// The short terminal-host socket directory the daemon is configured with (an AF_UNIX path
    /// must fit 104 bytes, so it is kept outside the home).
    pub terminal_host_dir: &'a Path,
}

type Lookup = Box<dyn Fn(&str) -> Option<String>>;
type Seed = Box<dyn FnOnce(&SeedContext<'_>) -> Result<String, String>>;

/// Builds a [`FullDaemon`]. Defaults: the process environment for lookups, no rehearsal profile,
/// and no seed step, so `start` fails until one is given.
pub struct FullDaemonBuilder<'a> {
    gdaemon: &'a Path,
    repo_root: &'a Path,
    lookup: Lookup,
    profile: Option<PathBuf>,
    seed: Option<Seed>,
    loopback: bool,
    ready_ceiling: Duration,
    readiness_path: String,
}

impl FullDaemonBuilder<'_> {
    /// Run gdaemon under the loopback-only mode: outbound connections other than
    /// loopback fail. The wrapper is built by `crate::loopback` and refuses where it has no
    /// mechanism.
    pub fn loopback_only(mut self) -> Self {
        self.loopback = true;
        self
    }

    /// Shorten the readiness wait (the e2e fixture's ceiling is the default). Only tests that
    /// need a failed start use this.
    pub fn ready_ceiling(mut self, ceiling: Duration) -> Self {
        self.ready_ceiling = ceiling;
        self
    }

    /// Poll this path instead of `/api/auth/status` for readiness. A path the daemon never
    /// answers with 200 makes a start fail after the ceiling, which the failed-start test needs.
    pub fn readiness_path(mut self, path: &str) -> Self {
        self.readiness_path = path.to_owned();
        self
    }

    /// Read `DATABASE_URL` and the PG* redirect variables through `lookup` instead of the process.
    pub fn environment(mut self, lookup: impl Fn(&str) -> Option<String> + 'static) -> Self {
        self.lookup = Box::new(lookup);
        self
    }

    /// Start from a rehearsal profile: the base URL is used as it is, with no
    /// create, drop or schema apply.
    pub fn rehearsal_profile(mut self, path: &Path) -> Self {
        self.profile = Some(path.to_path_buf());
        self
    }

    /// Run `seed` after the schema apply and before launch. It returns the operator bearer key it
    /// minted in the database.
    pub fn seed(
        mut self,
        seed: impl FnOnce(&SeedContext<'_>) -> Result<String, String> + 'static,
    ) -> Self {
        self.seed = Some(Box::new(seed));
        self
    }

    /// Run the guard, create the run database, apply the schema, seed, launch and wait for
    /// readiness. The guard runs before any process is spawned. On Windows the guard still runs,
    /// and the launch is then refused with [`FullDaemonError::Unsupported`].
    pub fn start(self) -> Result<FullDaemon, FullDaemonError> {
        let Self {
            gdaemon,
            repo_root,
            lookup,
            profile,
            seed,
            loopback,
            ready_ceiling,
            readiness_path,
        } = self;
        let base = lookup("DATABASE_URL");
        let guard = check_test_hub(base.as_deref(), &*lookup, profile.as_deref())?;
        if cfg!(windows) {
            return Err(FullDaemonError::Unsupported(WINDOWS_UNBUILT.into()));
        }
        let python = checkout_python(repo_root);
        require_file(gdaemon, "the gdaemon binary")?;
        require_file(&python, "the checkout's interpreter")?;
        let seed = seed.ok_or_else(|| FullDaemonError::Seed("no seed step was given".into()))?;

        let keep = std::env::var_os("GOBBY_E2E_KEEP_ARTIFACTS").is_some();
        let root = tempfile::Builder::new()
            .prefix("gobby-full-daemon-")
            .tempdir()
            .map_err(io_error("create the temporary root"))?;
        let home = root.path().join("home");
        let project = root.path().join("project");
        for dir in [home.join("logs"), home.join("files"), project.clone()] {
            fs::create_dir_all(&dir).map_err(io_error("create a home directory"))?;
        }
        let ports = daemon_ports(20)
            .map_err(io_error("choose daemon ports"))?
            .ok_or_else(|| {
                FullDaemonError::Io("no free daemon port layout after 20 tries".into())
            })?;

        // The run database exists only after the guard accepted the base URL.
        let database = match guard.mode {
            GuardMode::TestHub => Some(start_run_database(&guard)?),
            GuardMode::Rehearsal => None,
        };
        let database_url = database
            .as_ref()
            .map_or_else(|| guard.base.clone(), |run| run.url().to_owned());
        let database_name = database
            .as_ref()
            .and_then(|run| run.name().map(str::to_owned));
        let host_dir = tempfile::Builder::new()
            .prefix("g")
            .tempdir()
            .map_err(io_error("create the terminal host socket directory"))?;
        let root_canonical =
            fs::canonicalize(root.path()).map_err(io_error("resolve the temporary root"))?;
        let mut daemon = FullDaemon {
            root: Some(root),
            host_dir: Some(host_dir),
            keep,
            database,
            database_name,
            child: None,
            daemon_pid: 0,
            worker: None,
            front_door: format!("http://127.0.0.1:{}", ports.http),
            operator_key: String::new(),
            ready_ceiling,
            readiness_path,
            watch: None,
            relays: None,
            root_canonical,
        };
        let host_dir = daemon.host_dir_path();

        write_home(&home, ports, &database_url)?;
        let gterm = ensure_gterm(gdaemon, repo_root)?;
        install_gterm(&gterm, &home)?;
        if guard.mode == GuardMode::TestHub {
            schema_apply(gdaemon, &project, &database_url)?;
        }
        daemon.operator_key = seed(&SeedContext {
            database_url: &database_url,
            home: &home,
            project_dir: &project,
            python: &python,
            repo_root,
            terminal_host_dir: &host_dir,
        })
        .map_err(FullDaemonError::Seed)?;

        let owner = process_create_time(&python, repo_root)?;
        let backend = json!([
            python.display().to_string(),
            repo_root
                .join("tests/e2e/readiness_bootstrap.py")
                .display()
                .to_string(),
            "gobby.runner",
            "--config",
            home.join("config.yaml").display().to_string(),
        ])
        .to_string();
        // Loopback-only: the harness ports and the database port cross the namespace, and the
        // daemon's own outbound network is limited to loopback.
        let (mut command, relays) = if loopback {
            let inbound = [ports.http, ports.websocket];
            let outbound = [database_port(&database_url)?];
            let (command, relays) =
                loopback::wrap_command(gdaemon, &["serve"], &inbound, &outbound, &host_dir)
                    .map_err(loopback_error)?;
            (command, Some(relays))
        } else {
            let mut command = Command::new(gdaemon);
            command.arg("serve");
            (command, None)
        };
        daemon.relays = relays;
        command
            .env_clear()
            .envs(daemon_environment(repo_root, &python, &home))
            .env("DATABASE_URL", &database_url)
            .env("GOBBY_NATIVE_BIN_DIR", gdaemon.parent().unwrap_or(gdaemon))
            .env("GOBBY_BACKEND_COMMAND", backend)
            .env("GOBBY_E2E_OWNER_PID", std::process::id().to_string())
            .env("GOBBY_E2E_OWNER_CREATE_TIME", owner)
            // Set only here, in the child, after the guard accepted the base URL above.
            .env("GOBBY_ALLOW_WORKTREE_DAEMON", "1")
            .current_dir(&project)
            .stdin(Stdio::null())
            .stdout(open_log(&home.join("logs/daemon.log"))?)
            .stderr(open_log(&home.join("logs/daemon_error.log"))?);
        // gdaemon leads its own process group, so teardown can signal the whole group (Unix).
        #[cfg(unix)]
        command.process_group(0);
        let child = command
            .spawn()
            .map_err(|error| FullDaemonError::Io(format!("spawn gdaemon: {error}")))?;
        daemon.daemon_pid = child.id();
        daemon.child = Some(child);
        // The watchdog starts before readiness, so a test that dies at any later point still
        // stops gdaemon's groups and the gterm host.
        daemon.watch = Some(GroupWatch::start(
            daemon.daemon_pid,
            &host_dir.join("gterm.pid"),
        )?);

        if let Err(reason) = wait_ready(&mut daemon, &home) {
            return Err(daemon.abort_start(reason));
        }
        daemon.worker = worker_of(daemon.daemon_pid).and_then(Identity::observe);
        if daemon.worker.is_none() {
            let reason = FullDaemonError::Ready(
                "readiness passed but no worker process group leader is a child of gdaemon".into(),
            );
            return Err(daemon.abort_start(reason));
        }
        Ok(daemon)
    }
}

/// Maps a loopback refusal or failure to the harness error. A missing mechanism is unsupported
/// on this host, so it is reported as such rather than as a missing file.
fn loopback_error(error: LoopbackError) -> FullDaemonError {
    match &error {
        LoopbackError::Mechanism(_) => FullDaemonError::Unsupported(error.to_string()),
        LoopbackError::Target(_) => FullDaemonError::Missing(error.to_string()),
        LoopbackError::Probe(_) | LoopbackError::Io(_) => FullDaemonError::Io(error.to_string()),
    }
}

/// The TCP port in the database URL, the one the loopback relay must carry. A URL without a port
/// names the PostgreSQL default.
fn database_port(database_url: &str) -> Result<u16, FullDaemonError> {
    let without_scheme = database_url
        .split_once("://")
        .map_or(database_url, |(_, rest)| rest);
    let after_credentials = without_scheme
        .rsplit_once('@')
        .map_or(without_scheme, |(_, host)| host);
    let authority = after_credentials
        .split(['/', '?'])
        .next()
        .unwrap_or(after_credentials);
    match authority.rsplit_once(':') {
        Some((_, port)) => port.parse().map_err(|_| {
            FullDaemonError::Io(format!("the database URL's port is not a number: {port}"))
        }),
        None => Ok(5432),
    }
}

/// A running gdaemon and Python worker. Dropping it stops both process groups before the run
/// database is dropped, so a panicking test leaves no process and no database behind.
pub struct FullDaemon {
    root: Option<TempDir>,
    host_dir: Option<TempDir>,
    keep: bool,
    database: Option<RunDatabase>,
    database_name: Option<String>,
    child: Option<Child>,
    daemon_pid: u32,
    /// The Python worker, recorded at readiness with the start time that identifies it.
    worker: Option<Identity>,
    front_door: String,
    operator_key: String,
    ready_ceiling: Duration,
    readiness_path: String,
    /// The teardown watchdog for gdaemon's group, started with the daemon.
    watch: Option<GroupWatch>,
    /// The socat relays of a loopback-only run, stopped when the daemon is torn down.
    relays: Option<LoopbackGuard>,
    /// The temporary root with its symlinks resolved, the path a process's cwd names.
    root_canonical: PathBuf,
}

impl FullDaemon {
    /// Start a builder for `gdaemon` (the test's `CARGO_BIN_EXE_gdaemon`) in `repo_root`.
    pub fn builder<'a>(gdaemon: &'a Path, repo_root: &'a Path) -> FullDaemonBuilder<'a> {
        FullDaemonBuilder {
            gdaemon,
            repo_root,
            lookup: Box::new(|name: &str| std::env::var(name).ok()),
            profile: None,
            seed: None,
            loopback: false,
            ready_ceiling: READY_CEILING,
            readiness_path: READY_PATH.to_owned(),
        }
    }

    /// The front door's base URL, `http://127.0.0.1:<port>`.
    pub fn front_door(&self) -> String {
        self.front_door.clone()
    }

    /// The operator bearer key the seed step minted.
    pub fn operator_key(&self) -> &str {
        &self.operator_key
    }

    /// The temporary root directory; the daemon's home is its `home` subdirectory.
    pub fn home(&self) -> &Path {
        self.root.as_ref().map_or(Path::new(""), |root| root.path())
    }

    /// The run's temporary root, with symlinks resolved. Every process of the run has its cwd
    /// under it (the daemon starts in its project directory), so a process found there is ours.
    pub fn run_root(&self) -> &Path {
        &self.root_canonical
    }

    fn host_dir_path(&self) -> PathBuf {
        self.host_dir
            .as_ref()
            .map_or_else(PathBuf::new, |dir| dir.path().to_path_buf())
    }

    /// The terminal host pids this run knows of: the one `gterm.pid` names, and any gterm host
    /// that is gdaemon's child. A host can start before it writes its pid file, so the child scan
    /// finds a host whose file does not exist yet. Read while gdaemon still runs.
    fn terminal_host_candidates(&self, daemon_running: bool) -> Vec<Identity> {
        let mut hosts: Vec<u32> = read_host_pid(&self.host_dir_path()).into_iter().collect();
        if daemon_running {
            hosts.extend(terminal_host_children(self.daemon_pid));
        }
        hosts.sort_unstable();
        hosts.dedup();
        live_terminal_hosts(&hosts)
    }

    /// The gdaemon process id.
    pub fn daemon_pid(&self) -> u32 {
        self.daemon_pid
    }

    /// The Python worker's process id, recorded at readiness.
    pub fn worker_pid(&self) -> Option<u32> {
        self.worker.as_ref().map(|worker| worker.pid)
    }

    /// The detached gterm host's process id, from the `gterm.pid` it wrote in its socket directory.
    pub fn terminal_host_pid(&self) -> Option<u32> {
        read_host_pid(&self.host_dir_path())
    }

    /// The per-run database's name; `None` in rehearsal mode, which creates none.
    pub fn database_name(&self) -> Option<&str> {
        self.database_name.as_deref()
    }

    /// Stop gdaemon, then the worker and the terminal host, then any process still in the run's
    /// root or process groups, then drop the database, then remove the temporary root. A process
    /// that survived the orderly stops is stopped here too, and the teardown then fails naming it.
    pub fn teardown(mut self) -> Result<TeardownReport, FullDaemonError> {
        self.shutdown()
    }

    /// Tears down a daemon whose start failed, then reports what it held. Teardown runs here,
    /// before the error is returned, so the reported pids are dead unless teardown failed.
    fn abort_start(mut self, reason: FullDaemonError) -> FullDaemonError {
        let daemon_pid = self.daemon_pid;
        let worker_pid = self.worker_pid().or_else(|| worker_of(daemon_pid));
        let teardown = self.shutdown().err().map(|error| error.to_string());
        FullDaemonError::FailedStart {
            reason: reason.to_string(),
            daemon_pid,
            worker_pid,
            teardown,
        }
    }

    /// Runs every step even after one fails, so the database and home are never left behind by a
    /// stop error. The first error is returned.
    fn shutdown(&mut self) -> Result<TeardownReport, FullDaemonError> {
        let mut steps = Vec::new();
        let mut first_error = None;
        // A Drop after `teardown` finds the run already gone: there is nothing left to stop.
        let live_run = self.root.is_some();
        // gdaemon's pid names this run's process only while its child handle is unreaped. After
        // that the pid can belong to another process, so nothing is looked up through it.
        let daemon_running = self.child.is_some();
        // Read while gdaemon still runs: a worker is found through its parent, and a stopped
        // gdaemon's children are reparented where they cannot be found.
        let worker = self.worker.take().or_else(|| {
            daemon_running
                .then(|| worker_of(self.daemon_pid))
                .flatten()
                .and_then(Identity::observe)
        });
        let hosts = self.terminal_host_candidates(daemon_running);
        // The members of the run's process groups, identified while their leaders still run. A
        // member that outlives the orderly stops below is a stray; a pid that changed hands since
        // is not the member any more and is left alone.
        let group_ids: Vec<u32> = daemon_running
            .then_some(self.daemon_pid)
            .into_iter()
            .chain(
                worker
                    .as_ref()
                    .filter(|worker| worker.is_same_process())
                    .map(|worker| worker.pid),
            )
            .chain(
                hosts
                    .iter()
                    .filter(|host| host_is_live(host))
                    .map(|host| host.pid),
            )
            .collect();
        let members = group_snapshot(&group_ids);
        if let Some(mut child) = self.child.take() {
            match stop_daemon(&mut child, DAEMON_GRACE) {
                Ok(()) => steps.push(TeardownStep::DaemonStopped),
                Err(error) => first_error = first_error.or(Some(error)),
            }
        }
        if let Some(worker) = &worker {
            stop_worker(worker, WORKER_GRACE);
            steps.push(TeardownStep::WorkerStopped);
        }
        // A host that exited with the daemon is not a step: nothing was stopped.
        let mut host_stopped = false;
        for host in &hosts {
            host_stopped |= stop_terminal_host(host);
        }
        if host_stopped {
            steps.push(TeardownStep::TerminalHostStopped);
        }
        // The relays carry traffic only for gdaemon, which has stopped by now.
        self.relays = None;
        let processes_alive_at_drop = (daemon_running && pid_alive(self.daemon_pid))
            || worker.as_ref().is_some_and(Identity::is_same_process)
            || hosts.iter().any(host_is_live);
        // Anything left in the run's root, and any identified member of its process groups that
        // still runs, outlived the orderly stops above. It is stopped here and fails the teardown.
        if live_run {
            let own = std::process::id();
            let mut strays = processes_in_run(&self.root_canonical, &[]);
            strays.extend(
                members
                    .iter()
                    .filter(|member| {
                        member.pid != own && member.is_same_process() && pid_alive(member.pid)
                    })
                    .map(|member| member.pid),
            );
            strays.sort_unstable();
            strays.dedup();
            if !strays.is_empty() {
                stop_strays(&strays);
                first_error = first_error.or(Some(FullDaemonError::Teardown(format!(
                    "processes of the run outlived its orderly teardown and were stopped: {strays:?}"
                ))));
            }
        }
        if let Some(database) = self.database.take() {
            match database.teardown() {
                Ok(()) => steps.push(TeardownStep::DatabaseDropped),
                Err(error) => first_error = first_error.or(Some(error.into())),
            }
        }
        if let Some(root) = self.root.take() {
            if self.keep {
                let _kept = root.keep();
            } else {
                drop(root);
            }
            steps.push(TeardownStep::HomeRemoved);
        }
        if let Some(host_dir) = self.host_dir.take() {
            if self.keep {
                let _kept = host_dir.keep();
            } else {
                drop(host_dir);
            }
        }
        // Released last: until here a test killed mid-teardown still gets its groups stopped.
        if let Some(mut watch) = self.watch.take() {
            watch.stop();
        }
        match first_error {
            Some(error) => Err(error),
            None => Ok(TeardownReport {
                steps,
                processes_alive_at_drop,
            }),
        }
    }
}

impl Drop for FullDaemon {
    fn drop(&mut self) {
        // Best effort: a panicking test still stops both process groups before the database goes.
        let _ = self.shutdown();
    }
}

/// The checkout's interpreter path: `<repo_root>/.venv/bin/python` (Unix) or
/// `<repo_root>/.venv/Scripts/python.exe` (Windows).
#[cfg(unix)]
pub fn checkout_python(repo_root: &Path) -> PathBuf {
    repo_root.join(".venv").join("bin").join("python")
}

/// The checkout's interpreter path on Windows (`.venv\Scripts\python.exe`).
#[cfg(windows)]
pub fn checkout_python(repo_root: &Path) -> PathBuf {
    repo_root.join(".venv").join("Scripts").join("python.exe")
}

fn require_file(path: &Path, what: &str) -> Result<(), FullDaemonError> {
    if path.is_file() {
        Ok(())
    } else {
        Err(FullDaemonError::Missing(format!(
            "{what} is not a file: {}",
            path.display()
        )))
    }
}

fn io_error(context: &'static str) -> impl Fn(std::io::Error) -> FullDaemonError {
    move |error| FullDaemonError::Io(format!("{context}: {error}"))
}

fn open_log(path: &Path) -> Result<File, FullDaemonError> {
    File::create(path).map_err(io_error("create a log file"))
}

/// The child environment: a fixed PATH, the checkout's source root, and no inherited variables
/// beyond LANG, LC_ALL and TMPDIR. Provider keys are blanked as the e2e fixture
/// blanks them.
fn daemon_environment(repo_root: &Path, python: &Path, home: &Path) -> Vec<(String, String)> {
    let mut env = vec![
        (
            "PATH".to_owned(),
            format!(
                "{SYSTEM_PATH}{}{}",
                if SYSTEM_PATH.is_empty() { "" } else { ":" },
                python.parent().unwrap_or(python).display()
            ),
        ),
        ("HOME".to_owned(), home.display().to_string()),
        ("GOBBY_HOME".to_owned(), home.display().to_string()),
        (
            "GOBBY_CONFIG".to_owned(),
            home.join("config.yaml").display().to_string(),
        ),
        ("GOBBY_TEST_PROTECT".to_owned(), "1".to_owned()),
        (
            "PYTHONPATH".to_owned(),
            repo_root.join("src").display().to_string(),
        ),
        ("ANTHROPIC_API_KEY".to_owned(), String::new()),
        ("OPENAI_API_KEY".to_owned(), String::new()),
        ("GEMINI_API_KEY".to_owned(), String::new()),
    ];
    for name in ["LANG", "LC_ALL", "TMPDIR"] {
        if let Ok(value) = std::env::var(name) {
            env.push((name.to_owned(), value));
        }
    }
    env
}

fn write_home(home: &Path, ports: DaemonPorts, database_url: &str) -> Result<(), FullDaemonError> {
    fs::write(home.join("machine_id"), MACHINE_ID).map_err(home_io)?;
    let logs = home.join("logs");
    let config = json!({
        "daemon_port": ports.http,
        "test_mode": true,
        "database_url": home.join("hub-postgres.db").display().to_string(),
        "websocket": {"enabled": true, "port": ports.websocket, "ping_interval": 30, "ping_timeout": 10},
        "logging": {
            "client": logs.join("client.log").display().to_string(),
            "client_error": logs.join("client_error.log").display().to_string(),
        },
        "session_lifecycle": {
            "idle_timeout_minutes": 60,
            "max_sessions_per_machine": 10,
            "cleanup_interval_minutes": 5,
        },
    });
    fs::write(home.join("config.yaml"), config.to_string()).map_err(home_io)?;
    let bootstrap = json!({
        "hub_backend": "postgres",
        "database_url": database_url,
        "daemon_port": ports.http,
        "bind_host": "127.0.0.1",
        "websocket_port": ports.websocket,
        "files_home": home.join("files").display().to_string(),
        "front_door": {"enabled": true},
        "api_key": random_hex(),
    });
    let path = home.join("bootstrap.yaml");
    fs::write(&path, bootstrap.to_string()).map_err(home_io)?;
    // The bootstrap key is secret: owner-only on Unix. Plain std sets no ACL on Windows, so the
    // file keeps the temporary directory's inherited permissions there.
    #[cfg(unix)]
    fs::set_permissions(&path, Permissions::from_mode(0o600)).map_err(home_io)?;
    Ok(())
}

fn home_io(error: std::io::Error) -> FullDaemonError {
    FullDaemonError::Io(format!("write the home: {error}"))
}

/// 32 random bytes as lowercase hex, the bootstrap key the seed step replaces with a minted one.
fn random_hex() -> String {
    rand::random::<[u8; 32]>()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// `gdaemon schema apply` against the run database.
fn schema_apply(gdaemon: &Path, project: &Path, database_url: &str) -> Result<(), FullDaemonError> {
    let output = Command::new(gdaemon)
        .args(["schema", "apply"])
        .env_clear()
        .env("GOBBY_DATABASE_URL", database_url)
        .env("PATH", SYSTEM_PATH)
        .current_dir(project)
        .output()
        .map_err(io_error("run schema apply"))?;
    if output.status.success() {
        Ok(())
    } else {
        Err(FullDaemonError::Schema(format!(
            "{}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        )))
    }
}

fn process_create_time(python: &Path, repo_root: &Path) -> Result<String, FullDaemonError> {
    let script = "import os,sys,psutil;print(psutil.Process(int(sys.argv[1])).create_time())";
    let output = Command::new(python)
        .arg("-c")
        .arg(script)
        .arg(std::process::id().to_string())
        .env_clear()
        .current_dir(repo_root)
        .output()
        .map_err(io_error("read the owner's create time"))?;
    if !output.status.success() {
        return Err(FullDaemonError::Io(format!(
            "psutil could not read this process: {}",
            String::from_utf8_lossy(&output.stderr)
        )));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

fn wait_ready(daemon: &mut FullDaemon, home: &Path) -> Result<(), FullDaemonError> {
    let url = format!("{}{}", daemon.front_door, daemon.readiness_path);
    let ceiling = daemon.ready_ceiling;
    let deadline = Instant::now() + ceiling;
    loop {
        let child = daemon
            .child
            .as_mut()
            .ok_or_else(|| FullDaemonError::Ready("gdaemon was never spawned".into()))?;
        if let Some(status) = child.try_wait().map_err(io_error("poll gdaemon"))? {
            return Err(FullDaemonError::Ready(format!(
                "gdaemon exited with {status}:\n{}",
                log_tail(&home.join("logs/daemon_error.log"))
            )));
        }
        // Record the worker while gdaemon lives: once gdaemon exits, the worker is reparented and
        // no later lookup by parent can find it.
        if daemon.worker.is_none() {
            daemon.worker = worker_of(daemon.daemon_pid).and_then(Identity::observe);
        }
        if get_status(&url).ok() == Some(200) {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err(FullDaemonError::Ready(format!(
                "no 200 from {url} within {ceiling:?}:\n{}",
                log_tail(&home.join("logs/daemon_error.log"))
            )));
        }
        sleep(POLL);
    }
}

fn log_tail(path: &Path) -> String {
    let text = fs::read_to_string(path).unwrap_or_default();
    let lines: Vec<&str> = text.lines().collect();
    lines[lines.len().saturating_sub(40)..].join("\n")
}
