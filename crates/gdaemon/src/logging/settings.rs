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

/// The settings that `values` describe, starting from `previous`. Only the seven live
/// keys change anything. A key with an invalid value keeps its previous setting and adds
/// one warning naming the key.
pub fn from_values(
    values: &Map<String, Value>,
    previous: &LogSettings,
) -> (LogSettings, Vec<String>) {
    let mut next = previous.clone();
    let mut warnings = Vec::new();
    let mut take = |key: &str, apply: &mut dyn FnMut(&Value) -> bool| {
        if let Some(value) = values.get(key)
            && !apply(value)
        {
            warnings.push(format!(
                "{key} has an invalid value {value}; the previous setting is kept"
            ));
        }
    };
    take("logging.level", &mut |value| {
        let level = match value.as_str() {
            Some("debug") => Level::Debug,
            Some("info") => Level::Info,
            Some("warning") => Level::Warning,
            Some("error") => Level::Error,
            _ => return false,
        };
        next.level = level;
        true
    });
    take("logging.format", &mut |value| {
        let format = match value.as_str() {
            Some("text") => Format::Text,
            Some("json") => Format::Json,
            _ => return false,
        };
        next.format = format;
        true
    });
    take("logging.dir", &mut |value| match value.as_str() {
        Some(dir) if !dir.is_empty() => {
            next.dir = PathBuf::from(dir);
            true
        }
        _ => false,
    });
    take("logging.max_size_mb", &mut |value| {
        positive(value).is_some_and(|size| {
            next.max_size_mb = size;
            true
        })
    });
    take("logging.backup_count", &mut |value| {
        positive_count(value).is_some_and(|count| {
            next.backup_count = count;
            true
        })
    });
    take("logging.llm_max_size_mb", &mut |value| {
        positive(value).is_some_and(|size| {
            next.llm_max_size_mb = size;
            true
        })
    });
    take("logging.llm_backup_count", &mut |value| {
        positive_count(value).is_some_and(|count| {
            next.llm_backup_count = count;
            true
        })
    });
    (next, warnings)
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
