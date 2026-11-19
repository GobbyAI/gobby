//! Routed logging: one `tracing` layer that writes each record to its surface's file.
//!
//! Records carry a `gobby.*` target. The target picks the surface (daemon, hooks, llm,
//! mcp, automation, rule allow audit or a CLI's parser log), the surface picks the
//! file, and WARNING and above also reach `errors.log`. Before the configuration is
//! readable, records go to stderr as bare text. Settings follow the `logging.*` keys of
//! each configuration epoch.

use std::path::PathBuf;
use std::sync::OnceLock;

use anyhow::Context as _;
use time::{OffsetDateTime, UtcOffset};

pub mod classify;
pub mod epoch;
mod format;
mod layer;
pub mod settings;
mod sink;
pub mod writer;

#[cfg(test)]
pub(crate) use format::{Record, format_json, format_text};
#[cfg(test)]
pub(crate) use layer::{RouteHandle, routed_layer};
pub use settings::resolve_dir;
pub use sink::{install_pre_config, raw_stderr};

/// The level threshold of the routed files, in Python's level names.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Level {
    Debug,
    Info,
    Warning,
    Error,
}

impl Level {
    /// Python's level name.
    pub fn name(self) -> &'static str {
        match self {
            Level::Debug => "DEBUG",
            Level::Info => "INFO",
            Level::Warning => "WARNING",
            Level::Error => "ERROR",
        }
    }

    /// The level of a `tracing` record. TRACE is finer than any Python level and is
    /// never recorded.
    fn from_tracing(level: tracing::Level) -> Option<Self> {
        match level {
            tracing::Level::ERROR => Some(Level::Error),
            tracing::Level::WARN => Some(Level::Warning),
            tracing::Level::INFO => Some(Level::Info),
            tracing::Level::DEBUG => Some(Level::Debug),
            tracing::Level::TRACE => None,
        }
    }
}

/// The layout of a routed line.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format {
    Text,
    Json,
}

/// The routed logging settings: the seven live `logging.*` keys, plus the rule allow
/// audit file's size and backups, which stay at their compiled defaults.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LogSettings {
    pub level: Level,
    pub format: Format,
    pub dir: PathBuf,
    pub max_size_mb: u64,
    pub backup_count: u32,
    pub llm_max_size_mb: u64,
    pub llm_backup_count: u32,
    pub allow_audit_max_size_mb: u64,
    pub allow_audit_backup_count: u32,
}

/// Python's default rule allow audit retention target, in days.
const ALLOW_AUDIT_RETENTION_DAYS: u64 = 14;
/// Python's default rule allow audit file size, in MB.
const ALLOW_AUDIT_MAX_SIZE_MB: u64 = 256;

impl LogSettings {
    /// The compiled defaults, which are Python's (`src/gobby/config/logging.py`),
    /// writing under `dir`.
    pub fn defaults(dir: PathBuf) -> Self {
        Self {
            level: Level::Info,
            format: Format::Text,
            dir,
            max_size_mb: 10,
            backup_count: 5,
            llm_max_size_mb: 50,
            llm_backup_count: 5,
            allow_audit_max_size_mb: ALLOW_AUDIT_MAX_SIZE_MB,
            allow_audit_backup_count: settings::allow_audit_backup_count(
                ALLOW_AUDIT_RETENTION_DAYS,
                ALLOW_AUDIT_MAX_SIZE_MB,
            ),
        }
    }
}

/// Installs the routed layer as the process's subscriber, once per process. The files
/// open under `settings.dir` now, so a directory that cannot be written fails here.
///
/// A backend command always resolves today (`GOBBY_BACKEND_COMMAND`, or the compiled
/// default of the backend configuration), so the worker's handler owns rotation and this
/// writer only reopens a file the worker renamed.
pub fn activate(settings: LogSettings) -> anyhow::Result<()> {
    let routes = layer::RouteHandle::new(settings, writer::RotationOwner::Worker);
    routes
        .open_all()
        .context("routed logging cannot open its files")?;
    sink::activate_routes(routes)?;
    if let Some(Err(error)) = LOCAL_OFFSET.get() {
        tracing::warn!(
            target: "gobby.daemon",
            "the local time offset is unavailable ({error}); log times are UTC"
        );
    }
    Ok(())
}

/// The local UTC offset, read once at process start.
static LOCAL_OFFSET: OnceLock<Result<UtcOffset, String>> = OnceLock::new();

/// Reads the local UTC offset once, for the record times. Call it first in `main`, while
/// the process has one thread: on Unix the read is refused once other threads exist. A
/// refused read leaves the times in UTC, and activation logs that once.
pub fn capture_local_offset() {
    LOCAL_OFFSET
        .get_or_init(|| UtcOffset::current_local_offset().map_err(|error| error.to_string()));
}

/// The current time as `YYYY-MM-DDTHH:MM:SS`, at the captured offset (UTC without one).
fn local_timestamp() -> String {
    let offset = match LOCAL_OFFSET.get() {
        Some(Ok(offset)) => *offset,
        _ => UtcOffset::UTC,
    };
    let now = OffsetDateTime::now_utc().to_offset(offset);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
        now.year(),
        u8::from(now.month()),
        now.day(),
        now.hour(),
        now.minute(),
        now.second()
    )
}

#[cfg(test)]
mod tests;
