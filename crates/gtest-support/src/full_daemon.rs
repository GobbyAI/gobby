//! Full-daemon harness (design-v2 section 7.3): this checkout's gdaemon and Python worker on a
//! per-run database of the isolated test hub, with a temporary home and free loopback ports.
//!
//! The guard runs first (section 7.1). The worktree-daemon override reaches only the gdaemon
//! child's environment, and only after the guard accepted. Teardown stops gdaemon, then the
//! worker, then drops the run database (H45); `Drop` runs the same steps when a test panics.

use std::fmt;
use std::fs::{self, File};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

use nix::errno::Errno;
use nix::sys::signal::{Signal, kill, killpg};
use nix::unistd::Pid;
use serde_json::json;
use tempfile::TempDir;

use crate::database::{DatabaseError, RunDatabase, start_run_database};
use crate::http_sender::get_status;
use crate::hub_guard::{GuardMode, HubGuardError, check_test_hub};
use crate::ports::{DaemonPorts, daemon_ports};

/// The line a database-backed test prints when `DATABASE_URL` is unset (ruling H18, section 7.13).
pub const SKIP_LINE: &str =
    "skipped: DATABASE_URL is unset (P0-HARNESS needs the isolated test hub)";

/// The machine the isolated daemon is pinned to (tests/e2e/conftest.py:583).
const MACHINE_ID: &str = "21000000-0000-4000-8000-000000000002";
/// Ready ceiling, the e2e fixture's (tests/e2e/conftest.py:70-71).
const READY_CEILING: Duration = Duration::from_secs(300);
/// Grace for gdaemon after SIGTERM (section 7.3.6 step 2).
const DAEMON_GRACE: Duration = Duration::from_secs(30);
/// Grace for the Python worker after SIGTERM (section 7.3.6 step 3).
const WORKER_GRACE: Duration = Duration::from_secs(10);
const POLL: Duration = Duration::from_millis(100);

/// The test hub URL from `lookup`, or the skip line printed and `None` returned (H18).
pub fn test_hub_url_or_skip(lookup: &dyn Fn(&str) -> Option<String>) -> Option<String> {
    match lookup("DATABASE_URL") {
        Some(url) if !url.trim().is_empty() => Some(url),
        _ => {
            println!("{SKIP_LINE}");
            None
        }
    }
}

/// Whether a process with this pid exists and is not a zombie. EPERM counts as alive.
pub fn pid_alive(pid: u32) -> bool {
    // Pid 0 names the caller's own process group to kill(2), so it is never a process of ours.
    let Ok(raw) = i32::try_from(pid) else {
        return false;
    };
    if raw <= 0 {
        return false;
    }
    match kill(Pid::from_raw(raw), None) {
        Ok(()) => !is_zombie(pid),
        Err(Errno::EPERM) => true,
        Err(_) => false,
    }
}

fn is_zombie(pid: u32) -> bool {
    Command::new("ps")
        .args(["-o", "stat=", "-p", &pid.to_string()])
        .output()
        .map(|output| {
            String::from_utf8_lossy(&output.stdout)
                .trim()
                .starts_with('Z')
        })
        .unwrap_or(false)
}

/// The `(pid, pgid)` pairs of the processes whose parent is `parent`.
fn children_of(parent: u32) -> Vec<(u32, u32)> {
    let Ok(output) = Command::new("ps")
        .args(["-A", "-o", "pid=,ppid=,pgid="])
        .output()
    else {
        return Vec::new();
    };
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(|line| {
            let fields: Vec<u32> = line
                .split_whitespace()
                .map(str::parse)
                .collect::<Result<_, _>>()
                .ok()?;
            match fields[..] {
                [pid, ppid, pgid] if ppid == parent => Some((pid, pgid)),
                _ => None,
            }
        })
        .collect()
}

/// A failure to start, operate or stop the full daemon.
#[derive(Debug)]
pub enum FullDaemonError {
    /// The DATABASE_URL guard or a rehearsal profile refused the launch (design 7.1).
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
    /// A teardown step failed.
    Teardown(String),
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
            Self::Teardown(detail) => write!(f, "teardown: {detail}"),
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

/// A stop or drop step, in the order teardown performs them (section 7.3.6).
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
    /// The checkout's `.venv/bin/python`.
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
}

impl FullDaemonBuilder<'_> {
    /// Read `DATABASE_URL` and the PG* redirect variables through `lookup` instead of the process.
    pub fn environment(mut self, lookup: impl Fn(&str) -> Option<String> + 'static) -> Self {
        self.lookup = Box::new(lookup);
        self
    }

    /// Start from a rehearsal profile (section 7.1.2 R4): the base URL is used as it is, with no
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
    /// readiness. The guard runs before any process is spawned.
    pub fn start(self) -> Result<FullDaemon, FullDaemonError> {
        let Self {
            gdaemon,
            repo_root,
            lookup,
            profile,
            seed,
        } = self;
        let base = lookup("DATABASE_URL");
        let guard = check_test_hub(base.as_deref(), &*lookup, profile.as_deref())?;
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

        // The run database exists only after the guard accepted the base URL (section 7.2).
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
        let mut daemon = FullDaemon {
            root: Some(root),
            host_dir: Some(host_dir),
            keep,
            database,
            database_name,
            child: None,
            daemon_pid: 0,
            worker_pid: None,
            front_door: format!("http://127.0.0.1:{}", ports.http),
            operator_key: String::new(),
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
        let mut command = Command::new(gdaemon);
        command
            .arg("serve")
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
            .stderr(open_log(&home.join("logs/daemon_error.log"))?)
            .process_group(0);
        let child = command
            .spawn()
            .map_err(|error| FullDaemonError::Io(format!("spawn gdaemon: {error}")))?;
        daemon.daemon_pid = child.id();
        daemon.child = Some(child);

        wait_ready(&mut daemon, &home)?;
        daemon.worker_pid = find_worker(daemon.daemon_pid);
        if daemon.worker_pid.is_none() {
            return Err(FullDaemonError::Ready(
                "readiness passed but no worker process group leader is a child of gdaemon".into(),
            ));
        }
        Ok(daemon)
    }
}

/// A running gdaemon and Python worker. Dropping it stops both process groups before the run
/// database is dropped (H45), so a panicking test leaves no process and no database behind.
pub struct FullDaemon {
    root: Option<TempDir>,
    host_dir: Option<TempDir>,
    keep: bool,
    database: Option<RunDatabase>,
    database_name: Option<String>,
    child: Option<Child>,
    daemon_pid: u32,
    worker_pid: Option<u32>,
    front_door: String,
    operator_key: String,
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

    /// The temporary home the daemon reads from.
    pub fn home(&self) -> &Path {
        self.root.as_ref().map_or(Path::new(""), |root| root.path())
    }

    fn host_dir_path(&self) -> PathBuf {
        self.host_dir
            .as_ref()
            .map_or_else(PathBuf::new, |dir| dir.path().to_path_buf())
    }

    /// The gdaemon process id.
    pub fn daemon_pid(&self) -> u32 {
        self.daemon_pid
    }

    /// The Python worker's process id, recorded at readiness.
    pub fn worker_pid(&self) -> Option<u32> {
        self.worker_pid
    }

    /// The detached gterm host's process id, from the `gterm.pid` it wrote in its socket directory.
    pub fn terminal_host_pid(&self) -> Option<u32> {
        read_host_pid(&self.host_dir_path())
    }

    /// The per-run database's name; `None` in rehearsal mode, which creates none.
    pub fn database_name(&self) -> Option<&str> {
        self.database_name.as_deref()
    }

    /// Stop gdaemon, then the worker, then drop the database, then remove the temporary root.
    pub fn teardown(mut self) -> Result<TeardownReport, FullDaemonError> {
        self.shutdown()
    }

    /// Runs every step even after one fails, so the database and home are never left behind by a
    /// stop error. The first error is returned.
    fn shutdown(&mut self) -> Result<TeardownReport, FullDaemonError> {
        let mut steps = Vec::new();
        let mut first_error = None;
        // Read while gdaemon still runs: a worker is found through its parent, and a stopped
        // gdaemon's children are reparented where they cannot be found.
        let worker = self
            .worker_pid
            .take()
            .or_else(|| find_worker(self.daemon_pid));
        let host = read_host_pid(&self.host_dir_path());
        if let Some(mut child) = self.child.take() {
            match stop_daemon(&mut child, DAEMON_GRACE) {
                Ok(()) => steps.push(TeardownStep::DaemonStopped),
                Err(error) => first_error = first_error.or(Some(error)),
            }
        }
        if let Some(worker) = worker {
            stop_worker(worker, WORKER_GRACE);
            steps.push(TeardownStep::WorkerStopped);
        }
        // A host that exited with the daemon is not a step: nothing was stopped.
        if host.is_some_and(stop_terminal_host) {
            steps.push(TeardownStep::TerminalHostStopped);
        }
        let processes_alive_at_drop = pid_alive(self.daemon_pid)
            || worker.is_some_and(pid_alive)
            || host.is_some_and(pid_alive);
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

/// The checkout's interpreter path, `<repo_root>/.venv/bin/python`.
pub fn checkout_python(repo_root: &Path) -> PathBuf {
    repo_root.join(".venv").join("bin").join("python")
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
/// beyond LANG, LC_ALL and TMPDIR (design 7.3.2). Provider keys are blanked as the e2e fixture
/// blanks them.
fn daemon_environment(repo_root: &Path, python: &Path, home: &Path) -> Vec<(String, String)> {
    let mut env = vec![
        (
            "PATH".to_owned(),
            format!(
                "/usr/bin:/bin:/usr/sbin:/sbin:{}",
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

/// The config file and bootstrap of the isolated home (design 7.3.4). JSON is YAML, so the
/// daemon's YAML loader reads both.
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
        "gobby-tasks": {"expansion": {"enabled": false}, "validation": {"enabled": false}},
        "code_index": {"enabled": false},
        "embeddings": {"api_base": "http://127.0.0.1:11434/v1"},
        "memory": {"dream": {"enabled": false}},
        "session_summary": {"summary_file_path": home.join("session_summaries").display().to_string()},
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
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).map_err(home_io)?;
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

/// `gdaemon schema apply` against the run database (section 7.3.3).
fn schema_apply(gdaemon: &Path, project: &Path, database_url: &str) -> Result<(), FullDaemonError> {
    let output = Command::new(gdaemon)
        .args(["schema", "apply"])
        .env_clear()
        .env("GOBBY_DATABASE_URL", database_url)
        .env("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
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

/// The start time psutil reports for this test process, passed through unchanged: the readiness
/// bootstrap compares it for equality (tests/e2e/readiness_bootstrap.py:24-40).
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

/// Poll the front door until `/api/auth/status` answers 200. Fails early when gdaemon exits.
fn wait_ready(daemon: &mut FullDaemon, home: &Path) -> Result<(), FullDaemonError> {
    let url = format!("{}/api/auth/status", daemon.front_door);
    let deadline = Instant::now() + READY_CEILING;
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
        if get_status(&url).ok() == Some(200) {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err(FullDaemonError::Ready(format!(
                "no 200 from {url} within {READY_CEILING:?}:\n{}",
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

/// SIGTERM gdaemon's group, wait for its exit, then SIGKILL the group after the grace period.
fn stop_daemon(child: &mut Child, grace: Duration) -> Result<(), FullDaemonError> {
    let raw = i32::try_from(child.id())
        .map_err(|error| FullDaemonError::Teardown(format!("gdaemon pid: {error}")))?;
    let group = Pid::from_raw(raw);
    let _ = killpg(group, Signal::SIGTERM);
    let deadline = Instant::now() + grace;
    loop {
        if child
            .try_wait()
            .map_err(|error| FullDaemonError::Teardown(format!("poll gdaemon: {error}")))?
            .is_some()
        {
            return Ok(());
        }
        if Instant::now() >= deadline {
            let _ = killpg(group, Signal::SIGKILL);
            child
                .wait()
                .map_err(|error| FullDaemonError::Teardown(format!("reap gdaemon: {error}")))?;
            return Ok(());
        }
        sleep(POLL);
    }
}

/// SIGTERM the worker's group, then SIGKILL survivors after the grace period.
fn stop_worker(pid: u32, grace: Duration) {
    let Ok(raw) = i32::try_from(pid) else {
        return;
    };
    let group = Pid::from_raw(raw);
    let _ = killpg(group, Signal::SIGTERM);
    let deadline = Instant::now() + grace;
    while pid_alive(pid) && Instant::now() < deadline {
        sleep(POLL);
    }
    if pid_alive(pid) {
        let _ = killpg(group, Signal::SIGKILL);
    }
}

/// The gterm host binary beside gdaemon. When it is absent, build it from this checkout into the
/// same target directory, as the e2e fixtures build their binaries (tests/fixtures/gdaemon_binary.py).
fn ensure_gterm(gdaemon: &Path, repo_root: &Path) -> Result<PathBuf, FullDaemonError> {
    let built = gdaemon.with_file_name("gterm");
    if built.is_file() {
        return Ok(built);
    }
    let target = gdaemon.parent().and_then(Path::parent).ok_or_else(|| {
        FullDaemonError::Missing(format!("no target directory above {}", gdaemon.display()))
    })?;
    // gdaemon's profile directory names the build profile, so gterm is built in the same one.
    let release = gdaemon
        .parent()
        .and_then(Path::file_name)
        .is_some_and(|name| name == "release");
    let mut args = vec![
        "build",
        "-p",
        "gobby-terminal",
        "--features",
        "vt-engine",
        "--bin",
        "gterm",
    ];
    if release {
        args.push("--release");
    }
    let output = Command::new("cargo")
        .args(&args)
        .env("CARGO_TARGET_DIR", target)
        .current_dir(repo_root)
        .output()
        .map_err(io_error("run cargo for gterm"))?;
    if !output.status.success() {
        return Err(FullDaemonError::Missing(format!(
            "cargo could not build gterm:\n{}",
            String::from_utf8_lossy(&output.stderr)
        )));
    }
    if built.is_file() {
        Ok(built)
    } else {
        Err(FullDaemonError::Missing(format!(
            "gterm is absent at {}",
            built.display()
        )))
    }
}

/// Copy gterm to `<home>/bin/gterm`, the daemon's default host binary. A copy, not a link: gterm
/// refuses a symlinked executable.
fn install_gterm(gterm: &Path, home: &Path) -> Result<(), FullDaemonError> {
    let bin = home.join("bin");
    fs::create_dir_all(&bin).map_err(io_error("create the home bin directory"))?;
    let target = bin.join("gterm");
    fs::copy(gterm, &target).map_err(io_error("copy gterm into the home"))?;
    fs::set_permissions(&target, fs::Permissions::from_mode(0o755))
        .map_err(io_error("chmod gterm"))?;
    Ok(())
}

/// The process-table column that names the executable (macOS prints full paths in `comm`).
#[cfg(target_os = "macos")]
const NAME_COLUMN: &str = "ucomm";
#[cfg(not(target_os = "macos"))]
const NAME_COLUMN: &str = "comm";

/// Whether `pid` runs a gterm host. The daemon execs the host from a content-addressed pin named
/// `gterm-<sha256>` (crates/gterminal/src/host/image.rs), and the name column truncates that name,
/// so the identity check is the `gterm` prefix of the name column.
fn is_gterm_host(pid: u32) -> bool {
    Command::new("ps")
        .args(["-o", &format!("{NAME_COLUMN}="), "-p", &pid.to_string()])
        .output()
        .map(|output| {
            let name = String::from_utf8_lossy(&output.stdout);
            let name = name.trim();
            name == "gterm" || name.starts_with("gterm-")
        })
        .unwrap_or(false)
}

/// The gterm host pid the host wrote into its socket directory, when it wrote one.
fn read_host_pid(socket_dir: &Path) -> Option<u32> {
    if socket_dir.as_os_str().is_empty() {
        return None;
    }
    fs::read_to_string(socket_dir.join("gterm.pid"))
        .ok()?
        .trim()
        .parse::<u32>()
        .ok()
}

/// The Python worker: the child of gdaemon that leads its own process group.
fn find_worker(daemon_pid: u32) -> Option<u32> {
    children_of(daemon_pid)
        .into_iter()
        .find(|(pid, pgid)| pid == pgid)
        .map(|(pid, _)| pid)
}

/// Stop the gterm host at `pid` when it is still a live gterm, and report whether it was signalled.
/// The pid came from the host's own pidfile in this run's socket directory, and it is checked
/// before any signal, so a recycled pid is never signalled.
fn stop_terminal_host(pid: u32) -> bool {
    if !pid_alive(pid) || !is_gterm_host(pid) {
        return false;
    }
    let Ok(raw) = i32::try_from(pid) else {
        return false;
    };
    let _ = kill(Pid::from_raw(raw), Signal::SIGTERM);
    let deadline = Instant::now() + WORKER_GRACE;
    while pid_alive(pid) && Instant::now() < deadline {
        sleep(POLL);
    }
    if pid_alive(pid) {
        let _ = kill(Pid::from_raw(raw), Signal::SIGKILL);
    }
    true
}
