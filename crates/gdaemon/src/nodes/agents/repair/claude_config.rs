//! Locked read-modify-write of `~/.claude.json`, the port of the Python
//! `update_claude_config` contract: an mkdir lock beside the file with a
//! 10-second stale rule, then an atomic replace. A failed change leaves the
//! original file intact.

use super::write_atomic;
use anyhow::{Context, Result, ensure};
use serde_json::{Map, Value};
use std::{
    fs,
    io::ErrorKind,
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant, SystemTime},
};

const LOCK_WAIT: Duration = Duration::from_secs(30);
const LOCK_RETRY: Duration = Duration::from_millis(50);
const LOCK_STALE: Duration = Duration::from_secs(10);

/// Applies `change` to the object under the lock and persists it. Call from a
/// blocking context: the lock wait sleeps the thread.
pub fn update(
    path: &Path,
    change: impl FnOnce(&mut Map<String, Value>) -> Result<()>,
) -> Result<()> {
    ensure!(
        !path.is_symlink(),
        "{} must not be a symlink",
        path.display()
    );
    let _lock = acquire(path)?;
    let mut data = match fs::read(path) {
        Ok(bytes) => serde_json::from_slice::<Value>(&bytes)
            .with_context(|| format!("parse {}", path.display()))?,
        Err(error) if error.kind() == ErrorKind::NotFound => Value::Object(Map::new()),
        Err(error) => return Err(error).with_context(|| format!("read {}", path.display())),
    };
    let object = data
        .as_object_mut()
        .with_context(|| format!("parse {}: expected a JSON object", path.display()))?;
    change(object)?;
    let mut text = serde_json::to_string_pretty(&data)?;
    text.push('\n');
    write_atomic(path, text.as_bytes())
}

/// The held mkdir lock; dropping it releases the lock directory.
struct Lock(PathBuf);

impl Drop for Lock {
    fn drop(&mut self) {
        let _ = fs::remove_dir(&self.0);
    }
}

fn acquire(path: &Path) -> Result<Lock> {
    let file_name = path
        .file_name()
        .with_context(|| format!("{} has no file name", path.display()))?
        .to_string_lossy();
    let lock = path.with_file_name(format!("{file_name}.lock"));
    let deadline = Instant::now() + LOCK_WAIT;
    loop {
        match fs::create_dir(&lock) {
            Ok(()) => return Ok(Lock(lock)),
            Err(error) if error.kind() == ErrorKind::AlreadyExists => {
                if is_stale(&lock) {
                    let _ = fs::remove_dir(&lock);
                    continue;
                }
                ensure!(
                    Instant::now() < deadline,
                    "timed out waiting for Claude config lock {}",
                    lock.display()
                );
                thread::sleep(LOCK_RETRY);
            }
            Err(error) => {
                return Err(error).with_context(|| format!("create {}", lock.display()));
            }
        }
    }
}

fn is_stale(lock: &Path) -> bool {
    fs::metadata(lock)
        .and_then(|metadata| metadata.modified())
        .ok()
        .and_then(|modified| SystemTime::now().duration_since(modified).ok())
        .is_some_and(|age| age > LOCK_STALE)
}
