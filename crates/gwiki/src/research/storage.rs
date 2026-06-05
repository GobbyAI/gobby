use super::*;
use crate::ingest;
use crate::support::text::slugify_with_options;
use crate::support::time;
use std::sync::atomic::{AtomicU64, Ordering};

static TEMP_FILE_COUNTER: AtomicU64 = AtomicU64::new(0);
// Short polling keeps lock handoff responsive while avoiding a busy spin when
// another process is appending to the raw index.
const RAW_INDEX_LOCK_RETRY_SLEEP: Duration = Duration::from_millis(25);

pub(crate) fn append_raw_index_locked(
    vault_root: &Path,
    title: &str,
    note_path: &Path,
) -> Result<(), WikiError> {
    let raw_dir = vault_root.join("raw");
    fs::create_dir_all(&raw_dir).map_err(|error| WikiError::Io {
        action: "create raw directory",
        path: Some(raw_dir.clone()),
        source: error,
    })?;
    let index_path = raw_dir.join("INDEX.md");
    let relative = note_path
        .strip_prefix(vault_root)
        .unwrap_or(note_path)
        .to_string_lossy();
    let lock_path = raw_index_lock_path(vault_root);
    let lock = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .truncate(false)
        .open(&lock_path)
        .map_err(|error| WikiError::Io {
            action: "open raw index lock",
            path: Some(lock_path.clone()),
            source: error,
        })?;
    // This protects the raw index read-modify-write sequence, not just the final write.
    lock_raw_index(&lock, &lock_path)?;
    let mut contents = match fs::read_to_string(&index_path) {
        Ok(contents) => contents,
        Err(error) if error.kind() == ErrorKind::NotFound => "# Raw Sources\n\n".to_string(),
        Err(error) => {
            return Err(WikiError::Io {
                action: "read raw index",
                path: Some(index_path.clone()),
                source: error,
            });
        }
    };
    if contents.is_empty() {
        contents.push_str("# Raw Sources\n\n");
    }
    contents.push_str(&format!("- [{title}]({relative})\n"));
    // Keep the lock handle alive through the full read-modify-write sequence.
    write_file_atomically(&index_path, contents.as_bytes(), "raw index")
}

pub(crate) fn lock_raw_index(lock: &fs::File, lock_path: &Path) -> Result<(), WikiError> {
    let timeout = index_lock_timeout();
    let started = Instant::now();

    loop {
        match fs4::FileExt::try_lock(lock) {
            Ok(()) => return Ok(()),
            Err(fs4::TryLockError::WouldBlock) => {
                let elapsed = started.elapsed();
                if elapsed >= timeout {
                    return Err(WikiError::Io {
                        action: "lock raw index",
                        path: Some(lock_path.to_path_buf()),
                        source: std::io::Error::new(
                            ErrorKind::TimedOut,
                            format!("timed out after {}ms", timeout.as_millis()),
                        ),
                    });
                }
                thread::sleep(RAW_INDEX_LOCK_RETRY_SLEEP.min(timeout - elapsed));
            }
            Err(error) => {
                return Err(WikiError::Io {
                    action: "lock raw index",
                    path: Some(lock_path.to_path_buf()),
                    source: error.into(),
                });
            }
        }
    }
}

pub(crate) fn raw_index_lock_path(vault_root: &Path) -> PathBuf {
    vault_root.join("raw").join("INDEX.md.lock")
}

pub(crate) fn write_file_atomically(
    path: &Path,
    contents: &[u8],
    label: &'static str,
) -> Result<(), WikiError> {
    let temp_path = temp_sibling_path(path);
    let mut file = fs::File::create(&temp_path).map_err(|error| WikiError::Io {
        action: "create temp file",
        path: Some(temp_path.clone()),
        source: error,
    })?;
    if let Err(error) = file.write_all(contents) {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "write temp file",
            path: Some(temp_path),
            source: error,
        });
    }
    if let Err(error) = file.sync_all() {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "sync temp file",
            path: Some(temp_path),
            source: error,
        });
    }
    drop(file);
    if let Err(error) = fs::rename(&temp_path, path) {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: label,
            path: Some(path.to_path_buf()),
            source: error,
        });
    }
    ingest::sync_parent_dir(path)?;
    Ok(())
}

pub(crate) fn temp_sibling_path(path: &Path) -> PathBuf {
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("INDEX.md");
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    let suffix = TEMP_FILE_COUNTER.fetch_add(1, Ordering::Relaxed);
    path.with_file_name(format!(
        ".{file_name}.{}.{nanos}.{suffix}.tmp",
        std::process::id()
    ))
}

pub(crate) fn slugify(title: &str) -> String {
    slugify_with_options(title, Some("research-note"), None)
}

pub(crate) fn unix_timestamp_ms() -> Result<u64, WikiError> {
    time::unix_timestamp_ms()
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::*;

    #[test]
    fn temp_sibling_path_uses_unique_counter_suffix() {
        let path = Path::new("/tmp/INDEX.md");

        let first = temp_sibling_path(path);
        let second = temp_sibling_path(path);

        assert_ne!(first, second);
        assert_eq!(first.parent(), path.parent());
        assert_eq!(second.parent(), path.parent());
    }
}
