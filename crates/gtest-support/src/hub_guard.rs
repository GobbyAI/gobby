//! DATABASE_URL guard for test code that writes the test hub (design-v2 section 7.1).
//!
//! Verdicts and rule names equal `tests/fixtures/hub_guard.py`; both engines read
//! `tests/fixtures/hub_guard_vectors.json`. First failing rule refuses:
//! R2 redirect variables, R3 URL identity, R4 rehearsal profile, ARG missing or malformed input.

use std::fmt;
use std::path::Path;

/// The isolated test hub's port (ruling H5). Only the test-hub mode checks it.
pub const TEST_HUB_PORT: u16 = 60892;
pub const TEST_HUB_USER: &str = "gobby_test";
pub const TEST_HUB_DBNAME: &str = "gobby_test";
/// Copy of `gobby.cli.hub_backup.rehearsal._SHARED_PORTS`; a test compares it with the vector file.
pub const SHARED_PORTS: &[u16] = &[
    5432, 6333, 6334, 6379, 16379, 60887, 60888, 60889, 60890, 60891, 60892, 60987, 60988, 60990,
    60991, 60992, 60993, 60994, 60995, 60996, 60997, 60998, 60999,
];
pub const HARNESS_DATABASE_PREFIX: &str = "gobby_harness_";
pub const FIXTURE_DATABASE_PREFIX: &str = "gobby_test_";

/// Which identity the accepted base URL names.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GuardMode {
    TestHub,
    Rehearsal,
}

/// An accepted base URL. Per-run URLs derive from `base` only in `TestHub` mode.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HubGuard {
    pub base: String,
    pub mode: GuardMode,
}

/// A refused URL: `rule` is the first failing rule (R2, R3, R4 or ARG).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HubGuardError {
    pub rule: &'static str,
    pub detail: String,
}

impl fmt::Display for HubGuardError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.rule, self.detail)
    }
}

impl std::error::Error for HubGuardError {}

/// Why `database_url_for_tests` returned no URL.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DbUrlError {
    /// `DATABASE_URL` is not set; Rust tests skip with a printed line (ruling H18).
    Unset,
    Refused(HubGuardError),
}

impl fmt::Display for DbUrlError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unset => write!(f, "DATABASE_URL is unset"),
            Self::Refused(error) => write!(f, "{error}"),
        }
    }
}

impl std::error::Error for DbUrlError {}

/// Accept the base `DATABASE_URL` for a worktree daemon, or refuse it.
///
/// `profile` falls back to `GOBBY_HUB_REHEARSAL_PROFILE` from `env` when `None`.
pub fn check_test_hub(
    url: Option<&str>,
    env: &dyn Fn(&str) -> Option<String>,
    profile: Option<&Path>,
) -> Result<HubGuard, HubGuardError> {
    let _ = (url, env, profile);
    unimplemented!("hub_guard::check_test_hub (C11)")
}

/// Re-check a per-run URL against the process environment (R2) and the expected database.
pub fn check_derived_url(
    url: &str,
    expected_dbname: &str,
    expected_schema: Option<&str>,
) -> Result<(), HubGuardError> {
    check_derived_url_with_env(url, expected_dbname, expected_schema, &|name| {
        std::env::var(name).ok()
    })
}

/// `check_derived_url` with an explicit environment, for tests that must not see ambient variables.
pub fn check_derived_url_with_env(
    url: &str,
    expected_dbname: &str,
    expected_schema: Option<&str>,
    env: &dyn Fn(&str) -> Option<String>,
) -> Result<(), HubGuardError> {
    let _ = (url, expected_dbname, expected_schema, env);
    unimplemented!("hub_guard::check_derived_url_with_env (C11)")
}

/// Name a per-run database: `gobby_harness_<pid>_<12 lowercase hex>`.
pub fn harness_database_name(pid: u32, run_suffix: &str) -> Result<String, HubGuardError> {
    let _ = (pid, run_suffix);
    unimplemented!("hub_guard::harness_database_name (C11)")
}

/// The per-run URL: the accepted base with its database path replaced, or the base in rehearsal.
pub fn derive_run_url(
    guard: &HubGuard,
    dbname: &str,
    schema: Option<&str>,
) -> Result<String, HubGuardError> {
    let _ = (guard, dbname, schema);
    unimplemented!("hub_guard::derive_run_url (C11)")
}

/// The same hub on the `postgres` maintenance database, for CREATE and DROP DATABASE.
pub fn maintenance_url(url: &str) -> String {
    let _ = url;
    unimplemented!("hub_guard::maintenance_url (C11)")
}

/// Refuse a sweep connection that is not the maintenance database of a loopback gobby_test hub.
pub fn check_maintenance_url(url: &str) -> Result<(), HubGuardError> {
    let _ = url;
    unimplemented!("hub_guard::check_maintenance_url (C11)")
}

/// The pid in a run database name with `prefix`, when the whole name matches; `None` otherwise.
pub fn run_database_pid(name: &str, prefix: &str) -> Option<u32> {
    let _ = (name, prefix);
    unimplemented!("hub_guard::run_database_pid (C11)")
}

/// The operator's `~/.gobby`, or `None` when no home directory is known.
pub fn operator_home() -> Option<std::path::PathBuf> {
    unimplemented!("hub_guard::operator_home (C11)")
}

/// `DATABASE_URL` from the process environment, accepted for a test that needs the test hub.
pub fn database_url_for_tests() -> Result<String, DbUrlError> {
    database_url_with_env(&|name| std::env::var(name).ok())
}

/// `database_url_for_tests` with an explicit environment.
pub fn database_url_with_env(env: &dyn Fn(&str) -> Option<String>) -> Result<String, DbUrlError> {
    let _ = env;
    unimplemented!("hub_guard::database_url_with_env (C11)")
}
