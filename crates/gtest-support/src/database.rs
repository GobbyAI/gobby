//! Per-run databases on the test hub: create, teardown drop WITH FORCE, and the dead-pid sweep
//! (design-v2 section 7.2). Rehearsal mode uses the profile's database as it is and touches
//! nothing.

use std::fmt;

use crate::hub_guard::{HubGuard, HubGuardError};

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

/// One run's database and its URL. An owned run is dropped by `teardown`, or best-effort on drop.
#[derive(Debug)]
pub struct RunDatabase {
    _private: (),
}

impl RunDatabase {
    /// The URL the run connects to.
    pub fn url(&self) -> &str {
        unimplemented!("database::RunDatabase::url (C13)")
    }

    /// The run's database name, or `None` in rehearsal mode.
    pub fn name(&self) -> Option<&str> {
        unimplemented!("database::RunDatabase::name (C13)")
    }

    /// Drop the run's database WITH FORCE. Rehearsal mode drops nothing.
    pub fn teardown(self) -> Result<(), DatabaseError> {
        unimplemented!("database::RunDatabase::teardown (C13)")
    }
}

/// Start a run on the guarded hub: sweep dead-pid harness databases, then create this run's.
pub fn start_run_database(guard: &HubGuard) -> Result<RunDatabase, DatabaseError> {
    let _ = guard;
    unimplemented!("database::start_run_database (C13)")
}

/// `start_run_database` with an explicit pid and run suffix.
pub fn start_run_database_as(
    guard: &HubGuard,
    pid: u32,
    run_suffix: &str,
) -> Result<RunDatabase, DatabaseError> {
    let _ = (guard, pid, run_suffix);
    unimplemented!("database::start_run_database_as (C13)")
}

/// A fresh run suffix: 12 lowercase hex digits.
pub fn new_run_suffix() -> String {
    unimplemented!("database::new_run_suffix (C13)")
}

/// CREATE DATABASE for a harness-named database on the maintenance connection.
pub fn create_database(admin_url: &str, name: &str) -> Result<(), DatabaseError> {
    let _ = (admin_url, name);
    unimplemented!("database::create_database (C13)")
}

/// DROP DATABASE IF EXISTS ... WITH (FORCE) on the maintenance connection.
pub fn drop_database(admin_url: &str, name: &str) -> Result<(), DatabaseError> {
    let _ = (admin_url, name);
    unimplemented!("database::drop_database (C13)")
}

/// Whether a database named `name` exists on the hub.
pub fn database_exists(admin_url: &str, name: &str) -> Result<bool, DatabaseError> {
    let _ = (admin_url, name);
    unimplemented!("database::database_exists (C13)")
}

/// Drop every harness database whose pid is dead; return the names dropped.
pub fn sweep_dead_pid(admin_url: &str) -> Result<Vec<String>, DatabaseError> {
    let _ = admin_url;
    unimplemented!("database::sweep_dead_pid (C13)")
}
