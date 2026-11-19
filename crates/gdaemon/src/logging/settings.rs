//! The `logging.*` keys of a configuration epoch, mapped onto [`LogSettings`].

use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

use super::LogSettings;

/// The settings that `values` describe, starting from `previous`. Only the seven live
/// keys change anything. A key with an invalid value keeps its previous setting and adds
/// one warning naming the key.
pub fn from_values(
    _values: &Map<String, Value>,
    previous: &LogSettings,
) -> (LogSettings, Vec<String>) {
    (previous.clone(), Vec::new())
}

/// The rule allow audit backups that keep `retention_days` of lines at `max_size_mb`
/// per file, as Python sizes them.
pub fn allow_audit_backup_count(_retention_days: u64, _max_size_mb: u64) -> u32 {
    0
}

/// The logs directory: `GOBBY_LOGGING_DIR`, then the configured directory, then
/// `home/logs`. The configured default `~/.gobby/logs` resolves under `home`.
pub fn resolve_dir(
    _env: &dyn Fn(&str) -> Option<String>,
    _configured: Option<&Path>,
    _home: &Path,
) -> PathBuf {
    PathBuf::new()
}
