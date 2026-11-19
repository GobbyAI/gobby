//! Full-daemon harness (design-v2 section 7.3): this checkout's gdaemon and Python worker on a
//! per-run database of the isolated test hub, with a temporary home and free loopback ports.
//!
//! C14 red: the API is declared and every entry point panics until the harness lands in C15.

use std::fmt;
use std::path::{Path, PathBuf};

use crate::database::DatabaseError;
use crate::hub_guard::HubGuardError;

/// The line a database-backed test prints when `DATABASE_URL` is unset (ruling H18, section 7.13).
pub const SKIP_LINE: &str =
    "skipped: DATABASE_URL is unset (P0-HARNESS needs the isolated test hub)";

/// The test hub URL from `lookup`, or the skip line printed and `None` returned (H18).
pub fn test_hub_url_or_skip(_lookup: &dyn Fn(&str) -> Option<String>) -> Option<String> {
    unimplemented!("full_daemon::test_hub_url_or_skip (C15)")
}

/// Whether a process with this pid exists (signal 0; EPERM counts as alive).
pub fn pid_alive(_pid: u32) -> bool {
    unimplemented!("full_daemon::pid_alive (C15)")
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
    /// A child process could not be spawned.
    Spawn(String),
    /// The schema apply step failed.
    Schema(String),
    /// The pre-start seed step failed.
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
            Self::Spawn(detail) => write!(f, "spawn: {detail}"),
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
    /// The per-run database was dropped WITH FORCE. Never before the two stops.
    DatabaseDropped,
    /// The temporary home was removed.
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
/// checkout's interpreter and the repository root.
#[derive(Debug, Clone, Copy)]
pub struct SeedContext<'a> {
    /// The URL the daemon and worker will use.
    pub database_url: &'a str,
    /// The temporary home the daemon reads its bootstrap from.
    pub home: &'a Path,
    /// The checkout's `.venv/bin/python`.
    pub python: &'a Path,
    /// The repository root, which is also the Python source root's parent.
    pub repo_root: &'a Path,
}

/// Builds a [`FullDaemon`]. Defaults: the process environment for lookups, no rehearsal profile,
/// no seed step.
pub struct FullDaemonBuilder<'a> {
    _gdaemon: &'a Path,
    _repo_root: &'a Path,
    _private: (),
}

impl FullDaemonBuilder<'_> {
    /// Read `DATABASE_URL` and the PG* redirect variables through `lookup` instead of the process.
    pub fn environment(self, _lookup: impl Fn(&str) -> Option<String> + 'static) -> Self {
        unimplemented!("FullDaemonBuilder::environment (C15)")
    }

    /// Start from a rehearsal profile (section 7.1.2 R4): the profile's own database is used as
    /// it is, with no create, drop or schema apply.
    pub fn rehearsal_profile(self, _path: &Path) -> Self {
        unimplemented!("FullDaemonBuilder::rehearsal_profile (C15)")
    }

    /// Run `seed` after the schema apply and before launch. It returns the operator bearer key it
    /// minted in the database.
    pub fn seed(
        self,
        _seed: impl FnOnce(&SeedContext<'_>) -> Result<String, String> + 'static,
    ) -> Self {
        unimplemented!("FullDaemonBuilder::seed (C15)")
    }

    /// Run the guard, create the run database, apply the schema, seed, launch and wait for
    /// readiness. The guard runs before any process is spawned.
    pub fn start(self) -> Result<FullDaemon, FullDaemonError> {
        unimplemented!("FullDaemonBuilder::start (C15)")
    }
}

/// A running gdaemon and Python worker. Dropping it stops both process groups before the run
/// database is dropped (H45), so a panicking test leaves nothing behind.
pub struct FullDaemon {
    _private: (),
}

impl FullDaemon {
    /// Start a builder for `gdaemon` (the test's `CARGO_BIN_EXE_gdaemon`) in `repo_root`.
    pub fn builder<'a>(gdaemon: &'a Path, repo_root: &'a Path) -> FullDaemonBuilder<'a> {
        FullDaemonBuilder {
            _gdaemon: gdaemon,
            _repo_root: repo_root,
            _private: (),
        }
    }

    /// The front door's base URL, for example `http://127.0.0.1:<port>`.
    pub fn front_door(&self) -> String {
        unimplemented!("FullDaemon::front_door (C15)")
    }

    /// The operator bearer key the seed step minted.
    pub fn operator_key(&self) -> &str {
        unimplemented!("FullDaemon::operator_key (C15)")
    }

    /// The temporary home.
    pub fn home(&self) -> &Path {
        unimplemented!("FullDaemon::home (C15)")
    }

    /// The gdaemon process id.
    pub fn daemon_pid(&self) -> u32 {
        unimplemented!("FullDaemon::daemon_pid (C15)")
    }

    /// The Python worker's process id, when readiness recorded one.
    pub fn worker_pid(&self) -> Option<u32> {
        unimplemented!("FullDaemon::worker_pid (C15)")
    }

    /// The per-run database's name; `None` in rehearsal mode, which creates none.
    pub fn database_name(&self) -> Option<&str> {
        unimplemented!("FullDaemon::database_name (C15)")
    }

    /// Stop gdaemon, then the worker, then drop the database, then remove the home.
    pub fn teardown(self) -> Result<TeardownReport, FullDaemonError> {
        unimplemented!("FullDaemon::teardown (C15)")
    }
}

/// The checkout's interpreter path, `<repo_root>/.venv/bin/python`.
pub fn checkout_python(repo_root: &Path) -> PathBuf {
    repo_root.join(".venv").join("bin").join("python")
}
