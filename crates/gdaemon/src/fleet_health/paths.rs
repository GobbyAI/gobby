//! Gobby home and log-directory resolution for the watcher, mirroring the Python daemon
//! (`get_gobby_home` and `resolved_logs_dir`), so both read the same files.

use std::ffi::OsStr;
use std::path::{Path, PathBuf};

/// The Gobby home: `GOBBY_HOME` when set to a non-blank value, else `<user home>/.gobby`.
/// Mirrors `get_gobby_home`, which expands a leading `~` in the override.
pub fn gobby_home(env_home: Option<&OsStr>, user_home: Option<&Path>) -> Option<PathBuf> {
    match env_home.filter(|value| !is_blank(value)) {
        Some(value) => Some(expand_user(value, user_home)),
        None => user_home.map(|home| home.join(".gobby")),
    }
}

/// The daemon log directory: the explicit flag, then `GOBBY_LOGGING_DIR`, then `<home>/logs`.
/// A blank environment value counts as unset. The daemon's `logging.dir` in its config is not
/// read here; operators with a custom directory pass the flag.
pub fn logs_dir(
    explicit: Option<&Path>,
    env_logging_dir: Option<&OsStr>,
    home: &Path,
    user_home: Option<&Path>,
) -> PathBuf {
    if let Some(dir) = explicit {
        return expand_user(dir.as_os_str(), user_home);
    }
    match env_logging_dir.filter(|value| !is_blank(value)) {
        Some(value) => expand_user(value, user_home),
        None => home.join("logs"),
    }
}

fn is_blank(value: &OsStr) -> bool {
    value.to_string_lossy().trim().is_empty()
}

/// Expands a leading `~` the way `expanduser` does: `~` and `~/...` take the user home.
fn expand_user(raw: &OsStr, user_home: Option<&Path>) -> PathBuf {
    let path = Path::new(raw);
    match (path.strip_prefix("~"), user_home) {
        (Ok(rest), Some(home)) if rest.as_os_str().is_empty() => home.to_path_buf(),
        (Ok(rest), Some(home)) => home.join(rest),
        _ => path.to_path_buf(),
    }
}

#[cfg(test)]
#[path = "paths/tests.rs"]
mod tests;
