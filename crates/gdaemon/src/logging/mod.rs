//! Routed logging: one `tracing` layer that writes each record to its surface's file.
//!
//! Records carry a `gobby.*` target. The target picks the surface (daemon, hooks, llm,
//! mcp, automation, rule allow audit or a CLI's parser log), the surface picks the
//! file, and WARNING and above also reach `errors.log`. Before the configuration is
//! readable, records go to stderr as bare text. Settings follow the `logging.*` keys of
//! each configuration epoch.

use std::path::PathBuf;

pub mod classify;
pub mod epoch;
mod format;
pub mod layer;
pub mod settings;
mod sink;
pub mod writer;

pub use format::{Record, format_json, format_text};
pub use layer::{RouteHandle, RoutedLayer, routed_layer};
pub use settings::resolve_dir;
pub use sink::{install_pre_config, raw_stderr};

/// The level threshold of the routed files, in Python's level names.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Level {
    Debug,
    Info,
    Warning,
    Error,
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

impl LogSettings {
    /// The compiled defaults, which are Python's, writing under `dir`.
    pub fn defaults(dir: PathBuf) -> Self {
        Self {
            level: Level::Debug,
            format: Format::Text,
            dir,
            max_size_mb: 0,
            backup_count: 0,
            llm_max_size_mb: 0,
            llm_backup_count: 0,
            allow_audit_max_size_mb: 0,
            allow_audit_backup_count: 0,
        }
    }
}

/// Installs the routed layer as the process's subscriber, once per process.
pub fn activate(_settings: LogSettings) -> anyhow::Result<()> {
    anyhow::bail!("routed logging is not available")
}

#[cfg(test)]
mod tests;
