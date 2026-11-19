//! Per-run databases on the test hub: create, teardown drop WITH FORCE, and the dead-pid sweep
//! (design-v2 section 7.2). Rehearsal mode uses the profile's database as it is and touches
//! nothing: no connection, no create, no drop, no sweep.

use std::fmt;
use std::future::Future;

use tokio_postgres::{Client, NoTls};

use crate::hub_guard::{self, GuardMode, HARNESS_DATABASE_PREFIX, HubGuard, HubGuardError};

/// A database operation that failed, or a guard refusal.
#[derive(Debug)]
pub enum DatabaseError {
    Guard(HubGuardError),
    Postgres { context: String, message: String },
    Runtime(String),
}

impl fmt::Display for DatabaseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Guard(error) => write!(f, "{error}"),
            Self::Postgres { context, message } => write!(f, "{context}: {message}"),
            Self::Runtime(message) => write!(f, "tokio runtime: {message}"),
        }
    }
}

impl std::error::Error for DatabaseError {}

impl From<HubGuardError> for DatabaseError {
    fn from(error: HubGuardError) -> Self {
        Self::Guard(error)
    }
}

/// One run's database and its URL. An owned run is dropped by `teardown`, or best-effort on drop.
#[derive(Debug)]
pub struct RunDatabase {
    name: Option<String>,
    url: String,
    /// The maintenance URL used to drop the database; `None` in rehearsal mode.
    admin_url: Option<String>,
    dropped: bool,
}

impl RunDatabase {
    /// The URL the run connects to.
    pub fn url(&self) -> &str {
        &self.url
    }

    /// The run's database name, or `None` in rehearsal mode.
    pub fn name(&self) -> Option<&str> {
        self.name.as_deref()
    }

    /// Drop the run's database WITH FORCE. Rehearsal mode drops nothing.
    pub fn teardown(mut self) -> Result<(), DatabaseError> {
        let result = self.drop_owned();
        self.dropped = true;
        result
    }

    fn drop_owned(&self) -> Result<(), DatabaseError> {
        match (&self.name, &self.admin_url) {
            (Some(name), Some(admin)) => drop_database(admin, name),
            _ => Ok(()),
        }
    }
}

impl Drop for RunDatabase {
    /// A run that was never torn down, such as one left by a panic, is dropped best-effort so the
    /// test hub's tmpfs does not fill. Errors cannot be reported from drop, so they are ignored.
    fn drop(&mut self) {
        if !self.dropped {
            let _ = self.drop_owned();
            self.dropped = true;
        }
    }
}

/// Start a run on the guarded hub: sweep dead-pid harness databases, then create this run's own.
///
/// Rehearsal mode returns the profile's URL with no name and makes no connection.
pub fn start_run_database(guard: &HubGuard) -> Result<RunDatabase, DatabaseError> {
    start_run_database_as(guard, std::process::id(), &new_run_suffix())
}

/// `start_run_database` with an explicit pid and run suffix.
pub fn start_run_database_as(
    guard: &HubGuard,
    pid: u32,
    run_suffix: &str,
) -> Result<RunDatabase, DatabaseError> {
    if guard.mode == GuardMode::Rehearsal {
        return Ok(RunDatabase {
            name: None,
            url: guard.base.clone(),
            admin_url: None,
            dropped: false,
        });
    }
    let name = hub_guard::harness_database_name(pid, run_suffix)?;
    let admin = hub_guard::maintenance_url(&guard.base);
    sweep_dead_pid(&admin)?;
    create_database(&admin, &name)?;
    let url = hub_guard::derive_run_url(guard, &name, None)?;
    hub_guard::check_derived_url(&url, &name, None)?;
    let run = RunDatabase {
        name: Some(name),
        url,
        admin_url: Some(admin),
        dropped: false,
    };
    if let Err(error) = install_search_extension(run.url()) {
        // The run is unusable: drop what was just created and report the install failure.
        let _ = run.teardown();
        return Err(error);
    }
    Ok(run)
}

/// A fresh run suffix: 12 lowercase hex digits (48 random bits).
pub fn new_run_suffix() -> String {
    format!("{:012x}", rand::random::<u64>() & 0xffff_ffff_ffff)
}

/// CREATE DATABASE for a harness-named database on the maintenance connection.
pub fn create_database(admin_url: &str, name: &str) -> Result<(), DatabaseError> {
    hub_guard::check_maintenance_url(admin_url)?;
    require_harness_name(name)?;
    block_on(async {
        let client = connect(admin_url, "connect for CREATE DATABASE").await?;
        client
            .batch_execute(&format!("CREATE DATABASE \"{name}\""))
            .await
            .map_err(|error| postgres("CREATE DATABASE", &error))
    })
}

/// DROP DATABASE IF EXISTS ... WITH (FORCE) on the maintenance connection.
pub fn drop_database(admin_url: &str, name: &str) -> Result<(), DatabaseError> {
    hub_guard::check_maintenance_url(admin_url)?;
    require_harness_name(name)?;
    block_on(async {
        let client = connect(admin_url, "connect for DROP DATABASE").await?;
        client
            .batch_execute(&format!("DROP DATABASE IF EXISTS \"{name}\" WITH (FORCE)"))
            .await
            .map_err(|error| postgres("DROP DATABASE", &error))
    })
}

/// Whether a database named `name` exists on the hub.
pub fn database_exists(admin_url: &str, name: &str) -> Result<bool, DatabaseError> {
    hub_guard::check_maintenance_url(admin_url)?;
    block_on(async {
        let client = connect(admin_url, "connect for the existence query").await?;
        client
            .query_one(
                "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = $1)",
                &[&name],
            )
            .await
            .map(|row| row.get::<_, bool>(0))
            .map_err(|error| postgres("existence query", &error))
    })
}

/// Drop every harness database whose pid is dead; return the names dropped.
///
/// A database of a live pid is kept, and so is one whose pid cannot be signalled for another
/// reason than "no such process" (EPERM counts as alive).
pub fn sweep_dead_pid(admin_url: &str) -> Result<Vec<String>, DatabaseError> {
    hub_guard::check_maintenance_url(admin_url)?;
    block_on(async {
        let client = connect(admin_url, "connect for the sweep").await?;
        let rows = client
            .query("SELECT datname FROM pg_database", &[])
            .await
            .map_err(|error| postgres("list databases", &error))?;
        let mut dropped = Vec::new();
        for row in rows {
            let name: String = row.get(0);
            let Some(pid) = hub_guard::run_database_pid(&name, HARNESS_DATABASE_PREFIX) else {
                continue;
            };
            if process_alive(pid) {
                continue;
            }
            client
                .batch_execute(&format!("DROP DATABASE IF EXISTS \"{name}\" WITH (FORCE)"))
                .await
                .map_err(|error| postgres("DROP DATABASE of a dead run", &error))?;
            dropped.push(name);
        }
        Ok(dropped)
    })
}

fn require_harness_name(name: &str) -> Result<(), DatabaseError> {
    match hub_guard::run_database_pid(name, HARNESS_DATABASE_PREFIX) {
        Some(_) => Ok(()),
        None => Err(DatabaseError::Guard(HubGuardError {
            rule: "ARG",
            detail: "the database name must match the harness pattern".to_owned(),
        })),
    }
}

/// `kill(pid, 0)`: ESRCH means the process is gone; any other outcome means it may be alive.
fn process_alive(pid: u32) -> bool {
    let Ok(raw) = i32::try_from(pid) else {
        return true;
    };
    !matches!(
        nix::sys::signal::kill(nix::unistd::Pid::from_raw(raw), None),
        Err(nix::errno::Errno::ESRCH)
    )
}

fn install_search_extension(run_url: &str) -> Result<(), DatabaseError> {
    block_on(async {
        let client = connect(run_url, "connect to the run database").await?;
        client
            .batch_execute("CREATE EXTENSION pg_search")
            .await
            .map_err(|error| postgres("CREATE EXTENSION pg_search", &error))
    })
}

/// Run one blocking database operation on a fresh current-thread runtime that lives on a scoped
/// worker thread. The caller may be inside a tokio runtime (an async test, or gdaemon's async
/// harness), where building and blocking on a second runtime would panic, and a `Drop` may run
/// while a panic unwinds, where a panic would abort. A worker panic is reported, not propagated.
fn block_on<T: Send>(
    future: impl Future<Output = Result<T, DatabaseError>> + Send,
) -> Result<T, DatabaseError> {
    std::thread::scope(|scope| {
        scope
            .spawn(move || -> Result<T, DatabaseError> {
                let runtime = tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .map_err(|error| DatabaseError::Runtime(error.to_string()))?;
                runtime.block_on(future)
            })
            .join()
            .unwrap_or_else(|_| {
                Err(DatabaseError::Runtime(
                    "the database worker thread panicked".to_owned(),
                ))
            })
    })
}

async fn connect(url: &str, context: &str) -> Result<Client, DatabaseError> {
    let (client, connection) = tokio_postgres::connect(url, NoTls)
        .await
        .map_err(|error| postgres(context, &error))?;
    tokio::spawn(async move {
        let _ = connection.await;
    });
    Ok(client)
}

fn postgres(context: &str, error: &tokio_postgres::Error) -> DatabaseError {
    DatabaseError::Postgres {
        context: context.to_owned(),
        message: error.to_string(),
    }
}
