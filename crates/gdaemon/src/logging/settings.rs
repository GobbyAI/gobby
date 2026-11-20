//! The `logging.*` keys of a configuration epoch, mapped onto [`LogSettings`].

use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

use super::{Format, Level, LogSettings};

/// Python's configured default directory, which resolves under the Gobby home.
const DEFAULT_DIR: &str = "~/.gobby/logs";

/// Rule allow audit sizing (`src/gobby/config/logging.py`): measured lines per day and
/// a conservative line size.
const ALLOW_AUDIT_EVENTS_PER_DAY: u128 = 368_000;
const ALLOW_AUDIT_BYTES_PER_LINE: u128 = 512;

/// The settings that `values` describe. An epoch is the whole stored configuration, so a
/// live key it does not carry takes its `defaults` setting (the settings routing activated
/// with), and a key it carries with an invalid value keeps its `previous` setting and adds
/// one warning naming the key. Only the seven live keys change anything.
pub fn from_values(
    values: &Map<String, Value>,
    previous: &LogSettings,
    defaults: &LogSettings,
) -> (LogSettings, Vec<String>) {
    let mut warnings = Vec::new();
    let mut key = Key {
        values,
        warnings: &mut warnings,
    };
    let next = LogSettings {
        level: key.pick("logging.level", level, previous.level, defaults.level),
        format: key.pick("logging.format", format, previous.format, defaults.format),
        dir: key.pick(
            "logging.dir",
            dir,
            previous.dir.clone(),
            defaults.dir.clone(),
        ),
        max_size_mb: key.pick(
            "logging.max_size_mb",
            positive,
            previous.max_size_mb,
            defaults.max_size_mb,
        ),
        backup_count: key.pick(
            "logging.backup_count",
            positive_count,
            previous.backup_count,
            defaults.backup_count,
        ),
        llm_max_size_mb: key.pick(
            "logging.llm_max_size_mb",
            positive,
            previous.llm_max_size_mb,
            defaults.llm_max_size_mb,
        ),
        llm_backup_count: key.pick(
            "logging.llm_backup_count",
            positive_count,
            previous.llm_backup_count,
            defaults.llm_backup_count,
        ),
        ..defaults.clone()
    };
    (next, warnings)
}

/// One epoch's values, and the warnings its invalid keys add.
struct Key<'a> {
    values: &'a Map<String, Value>,
    warnings: &'a mut Vec<String>,
}

impl Key<'_> {
    /// The setting `name` describes: `default` when the epoch does not carry it, its parsed
    /// value when it is valid, and `previous` (with a warning) when it is not.
    fn pick<T>(
        &mut self,
        name: &str,
        parse: impl Fn(&Value) -> Option<T>,
        previous: T,
        default: T,
    ) -> T {
        match self.values.get(name) {
            None => default,
            Some(value) => parse(value).unwrap_or_else(|| {
                self.warnings.push(format!(
                    "{name} has an invalid value {value}; the previous setting is kept"
                ));
                previous
            }),
        }
    }
}

fn level(value: &Value) -> Option<Level> {
    match value.as_str()? {
        "debug" => Some(Level::Debug),
        "info" => Some(Level::Info),
        "warning" => Some(Level::Warning),
        "error" => Some(Level::Error),
        _ => None,
    }
}

fn format(value: &Value) -> Option<Format> {
    match value.as_str()? {
        "text" => Some(Format::Text),
        "json" => Some(Format::Json),
        _ => None,
    }
}

fn dir(value: &Value) -> Option<PathBuf> {
    value
        .as_str()
        .filter(|dir| !dir.is_empty())
        .map(PathBuf::from)
}

/// A JSON integer above zero, as Python's positive-value validator accepts.
fn positive(value: &Value) -> Option<u64> {
    value.as_u64().filter(|number| *number > 0)
}

fn positive_count(value: &Value) -> Option<u32> {
    positive(value).and_then(|number| u32::try_from(number).ok())
}

/// The rule allow audit backups that keep `retention_days` of lines at `max_size_mb`
/// per file, as Python sizes them.
pub fn allow_audit_backup_count(retention_days: u64, max_size_mb: u64) -> u32 {
    let required =
        u128::from(retention_days) * ALLOW_AUDIT_EVENTS_PER_DAY * ALLOW_AUDIT_BYTES_PER_LINE;
    let per_file = u128::from(max_size_mb.max(1)) * 1024 * 1024;
    let files = required.div_ceil(per_file);
    u32::try_from(files.saturating_sub(1))
        .unwrap_or(u32::MAX)
        .max(1)
}

/// The logs directory: `GOBBY_LOGGING_DIR`, then the configured directory, then
/// `home/logs`. The configured default `~/.gobby/logs` resolves under `home`.
pub fn resolve_dir(
    env: &dyn Fn(&str) -> Option<String>,
    configured: Option<&Path>,
    home: &Path,
) -> PathBuf {
    if let Some(dir) = env("GOBBY_LOGGING_DIR").filter(|dir| !dir.is_empty()) {
        return expand_user(env, Path::new(&dir));
    }
    match configured {
        Some(dir) if dir != Path::new(DEFAULT_DIR) => expand_user(env, dir),
        _ => home.join("logs"),
    }
}

/// `Path.expanduser`: a leading `~` becomes the user's home directory.
fn expand_user(env: &dyn Fn(&str) -> Option<String>, path: &Path) -> PathBuf {
    let Ok(rest) = path.strip_prefix("~") else {
        return path.to_path_buf();
    };
    match env("HOME").or_else(|| env("USERPROFILE")) {
        Some(user_home) => PathBuf::from(user_home).join(rest),
        None => path.to_path_buf(),
    }
}
