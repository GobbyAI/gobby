//! Per-tree removal lock, shared with the Python deletion path.
//!
//! A removal holds `flock(LOCK_EX)` on `<real parent>/.<tree name>.removal.lock` while it works
//! on the tree. `gobby.worktrees.removal_lock` takes the same lock on the same path, so a removal
//! that finds the lock busy keeps its record instead of reading a half-removed tree as dirty.

use anyhow::{Context, Result, bail};
use std::fs::{self, File, OpenOptions};
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

/// Holds one tree's removal lock until dropped. Dropping it, including when its removal is
/// cancelled, releases the lock.
#[derive(Debug)]
pub struct RemovalLock {
    tree: PathBuf,
    /// The lock file and its descriptor; `None` when the tree's parent does not exist, so no
    /// removal can race for the tree.
    held: Option<(PathBuf, File)>,
}

impl RemovalLock {
    /// Takes the tree's lock, or returns `Ok(None)` when another remover holds it.
    pub fn acquire(tree: &Path) -> Result<Option<Self>> {
        let parent = tree
            .parent()
            .with_context(|| format!("removal target {} has no parent", tree.display()))?;
        let name = tree
            .file_name()
            .and_then(|name| name.to_str())
            .with_context(|| format!("removal target {} has no name", tree.display()))?;
        let parent = match fs::canonicalize(parent) {
            Ok(parent) => parent,
            Err(error) if error.kind() == ErrorKind::NotFound => {
                return Ok(Some(Self {
                    tree: tree.to_owned(),
                    held: None,
                }));
            }
            Err(error) => {
                return Err(error)
                    .with_context(|| format!("resolve removal parent of {}", tree.display()));
            }
        };
        let lock_path = parent.join(format!(".{name}.removal.lock"));
        let Some(file) = lock_current(&lock_path)? else {
            return Ok(None);
        };
        Ok(Some(Self {
            tree: tree.to_owned(),
            held: Some((lock_path, file)),
        }))
    }
}

impl Drop for RemovalLock {
    fn drop(&mut self) {
        let Some((lock_path, file)) = self.held.take() else {
            return;
        };
        // Unlink while still locked, and only once the tree is gone. A waiter that opened the
        // unlinked file sees the name move on in `lock_current` and locks the replacement.
        if matches!(fs::symlink_metadata(&self.tree), Err(error) if error.kind() == ErrorKind::NotFound)
        {
            let _ = fs::remove_file(&lock_path);
        }
        drop(file);
    }
}

/// Opens and locks the lock file, or returns `Ok(None)` when another remover holds it.
fn lock_current(lock_path: &Path) -> Result<Option<File>> {
    loop {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(lock_path)
            .with_context(|| format!("open removal lock {}", lock_path.display()))?;
        if !try_lock(&file).with_context(|| format!("lock {}", lock_path.display()))? {
            return Ok(None);
        }
        match fs::metadata(lock_path) {
            Ok(current) => {
                if same_file(&current, &file)? {
                    return Ok(Some(file));
                }
                // The holder unlinked the name after this open, so lock the file that holds it now.
            }
            // Likewise when the holder removed the name and nothing has replaced it yet.
            Err(error) if error.kind() == ErrorKind::NotFound => {}
            Err(error) => bail!("stat removal lock {}: {error}", lock_path.display()),
        }
    }
}

#[cfg(unix)]
fn try_lock(file: &File) -> std::io::Result<bool> {
    use std::os::fd::AsRawFd;
    // SAFETY: `flock` reads only the descriptor `file` owns, which stays open for the call.
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
        return Ok(true);
    }
    let error = std::io::Error::last_os_error();
    if error.kind() == ErrorKind::WouldBlock {
        Ok(false)
    } else {
        Err(error)
    }
}

// Advisory locks are taken on POSIX only, where node mode runs.
#[cfg(not(unix))]
fn try_lock(_file: &File) -> std::io::Result<bool> {
    Ok(true)
}

#[cfg(unix)]
fn same_file(current: &fs::Metadata, file: &File) -> Result<bool> {
    use std::os::unix::fs::MetadataExt;
    let held = file.metadata().context("stat held removal lock")?;
    Ok((current.dev(), current.ino()) == (held.dev(), held.ino()))
}

#[cfg(not(unix))]
fn same_file(_current: &fs::Metadata, _file: &File) -> Result<bool> {
    Ok(true)
}
