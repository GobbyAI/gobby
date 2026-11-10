//! The state file that Python admission reads. It is rewritten atomically every sample.
//!
//! Contract (`schema_version` 1), one JSON object with these keys:
//! `schema_version`, `state` (`healthy|degraded|unstable`), `sampled_at_unix`,
//! `last_non_healthy_at_unix` (null until the first non-healthy sample),
//! `reasons` (array of strings), `thresholds_source`, `watcher_pid`.

use std::fs::{self, File};
use std::io::{self, Write};
use std::path::Path;

use serde::{Deserialize, Serialize};

use super::classify::State;

pub const SCHEMA_VERSION: u32 = 1;

/// Where the thresholds in force came from. Recorded so an operator can see a stale policy.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ThresholdsSource {
    ProjectPolicy,
    LastGood,
    Defaults,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StateFile {
    pub schema_version: u32,
    pub state: State,
    pub sampled_at_unix: i64,
    pub last_non_healthy_at_unix: Option<i64>,
    pub reasons: Vec<String>,
    pub thresholds_source: ThresholdsSource,
    pub watcher_pid: u32,
}

/// Write `state` to `path` through a temporary sibling, fsync, then rename over `path`.
pub fn write_atomic(path: &Path, state: &StateFile) -> io::Result<()> {
    let invalid = |message: &str| io::Error::new(io::ErrorKind::InvalidInput, message.to_string());
    let dir = path
        .parent()
        .ok_or_else(|| invalid("state path has no parent directory"))?;
    let file_name = path
        .file_name()
        .ok_or_else(|| invalid("state path has no file name"))?
        .to_string_lossy()
        .into_owned();
    fs::create_dir_all(dir)?;

    let temporary = dir.join(format!(".{file_name}.{}.tmp", std::process::id()));
    let written = (|| -> io::Result<()> {
        let mut file = File::create(&temporary)?;
        let bytes = serde_json::to_vec_pretty(state).map_err(io::Error::other)?;
        file.write_all(&bytes)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        fs::rename(&temporary, path)
    })();
    if written.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    written
}

pub fn read(path: &Path) -> io::Result<StateFile> {
    let raw = fs::read(path)?;
    serde_json::from_slice(&raw).map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
}

#[cfg(test)]
#[path = "state_file/tests.rs"]
mod tests;
