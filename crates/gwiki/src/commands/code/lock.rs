use std::fs::File;
use std::path::{Path, PathBuf};

/// Writer-lock file path relative to the codewiki out_dir.
const LOCK_FILE: &str = "_meta/codewiki.lock";

/// Exclusive single-writer lock over one codewiki out_dir (#17732).
///
/// Two concurrent `gwiki code` runs against the same out_dir corrupt the
/// vault: each `DocSink` holds the whole `_meta/codewiki.json` in memory and
/// rewrites it on every persist (so the writers alternately clobber each
/// other's accumulated entries), and whichever `finish` runs last prunes
/// every page absent from its own seen set — deleting pages the other run
/// just wrote. The lock is held from `DocSink` open through `finish`, so a
/// second writer fails fast with a clear error instead of interleaving.
///
/// Runs against different out_dirs never conflict: the lock is scoped to the
/// out_dir being written, not the project.
#[derive(Debug)]
pub(crate) struct CodewikiWriterLock {
    /// Held open for the lifetime of the lock. On Unix, closing this file
    /// releases the `flock(2)` — including when the process dies, so a
    /// killed run never leaves a stale lock behind.
    _file: File,
    /// Retained on non-Unix targets so `Drop` can remove the lock file —
    /// there the file's existence is itself the lock.
    #[cfg(not(unix))]
    lock_path: PathBuf,
}

impl CodewikiWriterLock {
    /// Acquire the single-writer lock for `out_dir`, creating `_meta/` if
    /// needed. Fails fast with the current holder's identity when another
    /// codewiki run is already writing this out_dir.
    pub(crate) fn acquire(out_dir: &Path) -> anyhow::Result<Self> {
        let lock_path = out_dir.join(LOCK_FILE);
        if let Some(parent) = lock_path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        Self::acquire_at(out_dir, lock_path)
    }

    /// Unix: advisory `flock(2)` on `_meta/codewiki.lock`. The file is left
    /// in place after release — unlinking it would let a third writer lock a
    /// fresh inode while a second still holds the orphaned one.
    ///
    /// The non-blocking probe retries briefly before refusing: a concurrent
    /// process spawn anywhere in this process (rayon workers shelling out to
    /// git for `--since`, tests spawning CLIs) transiently duplicates the fd
    /// table between fork and exec, so a just-released flock can still look
    /// held for a few milliseconds. A genuine holder keeps the lock for the
    /// whole generation run, far past this bound, and still fails fast.
    #[cfg(unix)]
    fn acquire_at(out_dir: &Path, lock_path: PathBuf) -> anyhow::Result<Self> {
        use std::io::Write as _;
        use std::os::unix::io::AsRawFd as _;

        const TOTAL_WAIT: std::time::Duration = std::time::Duration::from_secs(2);
        const POLL: std::time::Duration = std::time::Duration::from_millis(50);

        let file = File::options()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(&lock_path)?;
        let started = std::time::Instant::now();
        loop {
            let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
            if rc == 0 {
                break;
            }
            let err = std::io::Error::last_os_error();
            let retryable = matches!(
                err.raw_os_error(),
                Some(libc::EWOULDBLOCK) | Some(libc::EINTR)
            );
            if !retryable {
                return Err(anyhow::Error::new(err)
                    .context(format!("failed to lock {}", lock_path.display())));
            }
            if started.elapsed() >= TOTAL_WAIT {
                anyhow::bail!(held_by_message(out_dir, &lock_path));
            }
            std::thread::sleep(POLL);
        }
        // Diagnostics for the "held by" message a refused second run prints;
        // the flock itself is the gate, so stale contents are harmless.
        file.set_len(0)?;
        (&file).write_all(holder_info().as_bytes())?;
        Ok(Self { _file: file })
    }

    /// Non-Unix fallback: the lock file's existence is the lock, taken with
    /// an atomic create-new. A crashed run leaves the file behind, so the
    /// refusal message names it for manual removal.
    #[cfg(not(unix))]
    fn acquire_at(out_dir: &Path, lock_path: PathBuf) -> anyhow::Result<Self> {
        use std::io::Write as _;

        let mut file = match File::create_new(&lock_path) {
            Ok(file) => file,
            Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => {
                anyhow::bail!(
                    "{}; if that run crashed, remove {} and retry",
                    held_by_message(out_dir, &lock_path),
                    lock_path.display()
                );
            }
            Err(err) => {
                return Err(anyhow::Error::new(err)
                    .context(format!("failed to create {}", lock_path.display())));
            }
        };
        file.write_all(holder_info().as_bytes())?;
        Ok(Self {
            _file: file,
            lock_path,
        })
    }
}

#[cfg(not(unix))]
impl Drop for CodewikiWriterLock {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.lock_path);
    }
}

/// Refusal message shown when another run holds the out_dir's writer lock.
fn held_by_message(out_dir: &Path, lock_path: &Path) -> String {
    let holder = std::fs::read_to_string(lock_path)
        .ok()
        .map(|contents| contents.trim().to_string())
        .filter(|contents| !contents.is_empty())
        .map(|contents| format!(" (held by {contents})"))
        .unwrap_or_default();
    format!(
        "another `gwiki code` run is already writing {}{holder}; \
         wait for it to finish or stop it, then retry",
        out_dir.display()
    )
}

/// Holder identity written into the lock file for the refusal message.
fn holder_info() -> String {
    serde_json::json!({
        "pid": std::process::id(),
        "acquired_at": chrono::Utc::now().to_rfc3339(),
    })
    .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn second_acquire_on_same_out_dir_reports_the_holder() {
        let out_dir = tempfile::tempdir().expect("tempdir");
        let _held = CodewikiWriterLock::acquire(out_dir.path()).expect("first acquire");

        let refused = CodewikiWriterLock::acquire(out_dir.path())
            .expect_err("second acquire must be refused");

        let message = refused.to_string();
        assert!(
            message.contains("another `gwiki code` run is already writing"),
            "{message}"
        );
        assert!(
            message.contains(&format!("\"pid\":{}", std::process::id())),
            "{message}"
        );
    }

    #[test]
    fn dropping_the_lock_releases_the_out_dir_for_the_next_writer() {
        let out_dir = tempfile::tempdir().expect("tempdir");
        let held = CodewikiWriterLock::acquire(out_dir.path()).expect("first acquire");
        drop(held);

        let reacquired = CodewikiWriterLock::acquire(out_dir.path());
        assert!(
            reacquired.is_ok(),
            "reacquire after release: {reacquired:?}"
        );
    }

    #[test]
    fn different_out_dirs_do_not_conflict() {
        let first_dir = tempfile::tempdir().expect("tempdir");
        let second_dir = tempfile::tempdir().expect("tempdir");

        let _first = CodewikiWriterLock::acquire(first_dir.path()).expect("first out_dir");
        let second = CodewikiWriterLock::acquire(second_dir.path());
        assert!(
            second.is_ok(),
            "a second out_dir must not conflict: {second:?}"
        );
    }
}
