//! Locked read-modify-write of `~/.claude.json`, the port of the Python
//! `update_claude_config` contract: a mkdir lock beside the file, kept fresh by
//! a heartbeat, then an atomic replace. Lock ownership is checked before the
//! temp file is written and again before it is renamed into place, so a lost
//! lock aborts the change and leaves the original file intact.

use super::write_temp;
use anyhow::{Context, Result, bail, ensure};
use serde_json::{Map, Value};
use std::{
    fs::{self, DirBuilder},
    io::{self, ErrorKind},
    os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex, MutexGuard, PoisonError,
        mpsc::{self, RecvTimeoutError},
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant, SystemTime},
};

/// Lock timing. The Python oracle's values are [`PYTHON_TIMING`].
#[derive(Clone, Copy)]
struct Timing {
    stale: Duration,
    heartbeat: Duration,
    timeout: Duration,
    retry: Duration,
}

const PYTHON_TIMING: Timing = Timing {
    stale: Duration::from_secs(10),
    heartbeat: Duration::from_secs(5),
    timeout: Duration::from_secs(30),
    retry: Duration::from_millis(50),
};

/// Applies `change` to the object under the lock and persists it. Call from a
/// blocking context: the lock wait sleeps the thread.
pub fn update(
    path: &Path,
    change: impl FnOnce(&mut Map<String, Value>) -> Result<()>,
) -> Result<()> {
    update_with(path, PYTHON_TIMING, change)
}

fn update_with(
    path: &Path,
    timing: Timing,
    change: impl FnOnce(&mut Map<String, Value>) -> Result<()>,
) -> Result<()> {
    ensure!(
        !path.is_symlink(),
        "{} must not be a symlink",
        path.display()
    );
    let lock = acquire(path, timing)?;
    let mut data = match fs::read(path) {
        Ok(bytes) => serde_json::from_slice::<Value>(&bytes)
            .with_context(|| format!("parse {}", path.display()))?,
        Err(error) if error.kind() == ErrorKind::NotFound => Value::Object(Map::new()),
        Err(error) => return Err(error).with_context(|| format!("read {}", path.display())),
    };
    let mode = config_mode(path)?;
    let object = data
        .as_object_mut()
        .with_context(|| format!("parse {}: expected a JSON object", path.display()))?;
    change(object)?;
    let mut text = serde_json::to_string_pretty(&data)?;
    text.push('\n');
    lock.check()?;
    let temp = write_temp(path, text.as_bytes(), mode)?;
    let published = lock.check().and_then(|()| {
        fs::rename(&temp, path).with_context(|| format!("replace {}", path.display()))
    });
    if published.is_err() {
        let _ = fs::remove_file(&temp);
    }
    published
}

/// The mode the rewritten config keeps: an existing file's, or owner-only for a new one.
fn config_mode(path: &Path) -> Result<u32> {
    match fs::metadata(path) {
        Ok(metadata) => Ok(metadata.permissions().mode() & 0o777),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(0o600),
        Err(error) => Err(error).with_context(|| format!("stat {}", path.display())),
    }
}

/// A held lock directory. A heartbeat thread keeps its mtime fresh, so a slow
/// holder is never mistaken for a stale one.
struct Lock {
    path: PathBuf,
    held: Arc<Mutex<Held>>,
    stop: Option<mpsc::Sender<()>>,
    heartbeat: Option<JoinHandle<()>>,
}

/// The identity this holder created, and why the heartbeat lost it, if it did.
struct Held {
    identity: Identity,
    lost: Option<String>,
}

/// The directory identity the Python oracle compares: device, inode and mtime.
#[derive(Clone, Copy, PartialEq, Eq)]
struct Identity {
    dev: u64,
    ino: u64,
    mtime: (i64, i64),
}

impl Lock {
    fn hold(path: PathBuf, every: Duration) -> Result<Self> {
        let identity = identity(&path).with_context(|| format!("stat {}", path.display()))?;
        let held = Arc::new(Mutex::new(Held {
            identity,
            lost: None,
        }));
        let (stop, stopped) = mpsc::channel();
        let heartbeat = {
            let (path, held) = (path.clone(), Arc::clone(&held));
            thread::spawn(move || beat(&path, &held, every, &stopped))
        };
        Ok(Self {
            path,
            held,
            stop: Some(stop),
            heartbeat: Some(heartbeat),
        })
    }

    /// Fails once the heartbeat has lost the lock or the directory is no longer the one held.
    fn check(&self) -> Result<()> {
        let held = lock_state(&self.held);
        if let Some(reason) = &held.lost {
            bail!(
                "Claude config lock was compromised: {}: {reason}",
                self.path.display()
            );
        }
        let current =
            identity(&self.path).with_context(|| format!("stat {}", self.path.display()))?;
        ensure!(
            current == held.identity,
            "Claude config lock ownership changed: {}",
            self.path.display()
        );
        Ok(())
    }
}

impl Drop for Lock {
    fn drop(&mut self) {
        // Closing the channel wakes the heartbeat so it exits before the release.
        drop(self.stop.take());
        if let Some(handle) = self.heartbeat.take() {
            let _ = handle.join();
        }
        let held = lock_state(&self.held);
        // Release only the directory this holder created; another owner's lock stays.
        let ours = identity(&self.path).is_ok_and(|current| {
            (current.dev, current.ino) == (held.identity.dev, held.identity.ino)
        });
        if ours {
            let _ = fs::remove_dir(&self.path);
        }
    }
}

fn lock_state(shared: &Mutex<Held>) -> MutexGuard<'_, Held> {
    shared.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Refreshes the lock's mtime every `every` until the holder stops or loses the lock.
fn beat(path: &Path, shared: &Mutex<Held>, every: Duration, stopped: &mpsc::Receiver<()>) {
    while let Err(RecvTimeoutError::Timeout) = stopped.recv_timeout(every) {
        let mut held = lock_state(shared);
        match refresh(path, held.identity) {
            Ok(identity) => held.identity = identity,
            Err(error) => {
                held.lost = Some(format!("{error:#}"));
                return;
            }
        }
    }
}

/// Touches the lock directory after confirming it is still the one `expected` names.
fn refresh(path: &Path, expected: Identity) -> Result<Identity> {
    let current = identity(path).with_context(|| format!("stat {}", path.display()))?;
    ensure!(
        current == expected,
        "Claude config lock ownership changed: {}",
        path.display()
    );
    fs::File::open(path)?.set_modified(SystemTime::now())?;
    identity(path).with_context(|| format!("stat {}", path.display()))
}

fn identity(path: &Path) -> io::Result<Identity> {
    let metadata = fs::symlink_metadata(path)?;
    Ok(Identity {
        dev: metadata.dev(),
        ino: metadata.ino(),
        mtime: (metadata.mtime(), metadata.mtime_nsec()),
    })
}

fn acquire(path: &Path, timing: Timing) -> Result<Lock> {
    let file_name = path
        .file_name()
        .with_context(|| format!("{} has no file name", path.display()))?
        .to_string_lossy();
    let lock = path.with_file_name(format!("{file_name}.lock"));
    let deadline = Instant::now() + timing.timeout;
    loop {
        ensure!(
            Instant::now() < deadline,
            "timed out waiting for Claude config lock {}",
            lock.display()
        );
        match DirBuilder::new().mode(0o700).create(&lock) {
            Ok(()) => return Lock::hold(lock, timing.heartbeat),
            Err(error) if error.kind() == ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error).with_context(|| format!("create {}", lock.display())),
        }
        if !clear_stale(&lock, timing.stale)? {
            thread::sleep(timing.retry);
        }
    }
}

/// Clears a lock directory whose mtime is older than `stale`. Returns `true` when
/// the lock is gone and the caller should retry at once; `false` when it is live.
fn clear_stale(lock: &Path, stale: Duration) -> Result<bool> {
    let metadata = match fs::symlink_metadata(lock) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(true),
        Err(error) => return Err(error).with_context(|| format!("stat {}", lock.display())),
    };
    ensure!(
        metadata.is_dir(),
        "Claude config lock is not a directory: {}",
        lock.display()
    );
    let aged = SystemTime::now()
        .duration_since(metadata.modified()?)
        .is_ok_and(|age| age > stale);
    if !aged {
        return Ok(false);
    }
    match fs::remove_dir(lock) {
        Ok(()) => Ok(true),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(true),
        Err(error) => Err(error)
            .with_context(|| format!("remove stale Claude config lock {}", lock.display())),
    }
}

#[cfg(test)]
mod tests {
    use super::{PYTHON_TIMING, Timing, update, update_with};
    use anyhow::{Context, Result};
    use serde_json::Value;
    use std::{
        fs,
        os::unix::fs::PermissionsExt,
        sync::mpsc,
        thread,
        time::{Duration, SystemTime},
    };

    #[test]
    fn concurrent_updates_keep_every_entry() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("claude.json");
        let writers: Vec<_> = (0..8u64)
            .map(|index| {
                let path = path.clone();
                thread::spawn(move || {
                    update(&path, |data| {
                        data.insert(format!("entry-{index}"), Value::from(index));
                        Ok(())
                    })
                })
            })
            .collect();
        for writer in writers {
            writer.join().expect("writer thread panicked")?;
        }
        let data: Value = serde_json::from_slice(&fs::read(&path)?)?;
        assert_eq!(
            data.as_object().map(|object| object.len()),
            Some(8),
            "{data}"
        );
        Ok(())
    }

    #[test]
    fn heartbeat_keeps_a_slow_holder_from_being_stolen() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("claude.json");
        let timing = Timing {
            stale: Duration::from_millis(300),
            heartbeat: Duration::from_millis(50),
            timeout: Duration::from_secs(5),
            retry: Duration::from_millis(10),
        };
        let (held_tx, held_rx) = mpsc::channel();
        let slow = {
            let path = path.clone();
            thread::spawn(move || {
                update_with(&path, timing, |data| {
                    let _ = held_tx.send(());
                    thread::sleep(Duration::from_millis(900));
                    data.insert("slow".into(), Value::Bool(true));
                    Ok(())
                })
            })
        };
        held_rx.recv_timeout(Duration::from_secs(5))?;
        update_with(&path, timing, |data| {
            data.insert("fast".into(), Value::Bool(true));
            Ok(())
        })?;
        slow.join().expect("slow updater panicked")?;
        let data: Value = serde_json::from_slice(&fs::read(&path)?)?;
        assert_eq!(data["slow"], Value::Bool(true), "{data}");
        assert_eq!(data["fast"], Value::Bool(true), "{data}");
        Ok(())
    }

    #[test]
    fn lock_lost_during_change_aborts_the_write() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("claude.json");
        let lock = dir.path().join("claude.json.lock");
        fs::write(&path, "{\"kept\":true}\n")?;
        let result = update_with(&path, PYTHON_TIMING, |data| {
            // Another holder takes the lock directory while this change runs.
            fs::remove_dir(&lock)?;
            fs::create_dir(&lock)?;
            data.insert("ours".into(), Value::Bool(true));
            Ok(())
        });
        assert!(result.is_err(), "a lost lock must abort the write");
        assert_eq!(fs::read_to_string(&path)?, "{\"kept\":true}\n");
        assert!(lock.is_dir(), "a lock this update no longer owns must stay");
        Ok(())
    }

    #[test]
    fn stale_lock_that_cannot_be_removed_fails_within_the_timeout() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("claude.json");
        let lock = dir.path().join("claude.json.lock");
        fs::create_dir(&lock)?;
        // A non-empty lock directory refuses removal, so the stale lock cannot be cleared.
        fs::write(lock.join("owner"), "stuck")?;
        fs::File::open(&lock)?.set_modified(SystemTime::now() - Duration::from_secs(60))?;
        let timing = Timing {
            timeout: Duration::from_secs(1),
            retry: Duration::from_millis(10),
            ..PYTHON_TIMING
        };
        let (done_tx, done_rx) = mpsc::channel();
        thread::spawn(move || {
            let _ = done_tx.send(update_with(&path, timing, |_| Ok(())));
        });
        let result = done_rx
            .recv_timeout(Duration::from_secs(5))
            .context("update spun on the stale lock instead of failing")?;
        assert!(
            result.is_err(),
            "an unremovable stale lock must fail the update"
        );
        Ok(())
    }

    #[test]
    fn rewritten_config_keeps_its_private_mode() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let existing = dir.path().join("claude.json");
        fs::write(&existing, "{}\n")?;
        fs::set_permissions(&existing, fs::Permissions::from_mode(0o600))?;
        update(&existing, |data| {
            data.insert("rewritten".into(), Value::Bool(true));
            Ok(())
        })?;
        assert_eq!(fs::metadata(&existing)?.permissions().mode() & 0o777, 0o600);

        let fresh = dir.path().join("fresh.json");
        update(&fresh, |data| {
            data.insert("created".into(), Value::Bool(true));
            Ok(())
        })?;
        assert_eq!(fs::metadata(&fresh)?.permissions().mode() & 0o777, 0o600);
        Ok(())
    }
}
