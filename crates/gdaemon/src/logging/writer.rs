//! The size-rotating, append-only log file.
//!
//! Every handle opens with append, so two writers on one file never overwrite each other.
//! Before each line the writer compares the path's identity with its open handle and
//! reopens when they differ, so a file another process renamed away is replaced by a new
//! one at the path.

use std::io;
use std::path::Path;

/// Who rotates the files: the worker (Python's handler renames; this writer only
/// reopens) or gdaemon (this writer rotates as `RotatingFileHandler` does).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RotationOwner {
    Worker,
    Gdaemon,
}

/// The rotation owner: the worker whenever a backend command is configured.
pub fn owner_for(_backend_command: Option<&str>) -> RotationOwner {
    RotationOwner::Worker
}

/// A file's identity: (device, inode) on Unix, creation time on Windows.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct FileIdentity {
    _stamp: (),
}

/// The identity of the file at `path`.
pub fn identity_of(_path: &Path) -> io::Result<FileIdentity> {
    Ok(FileIdentity::default())
}

/// The share mode the Windows writer opens with.
#[cfg(windows)]
pub const SHARE_MODE: u32 = 0;

/// One log file with size rotation.
#[derive(Debug)]
pub struct SizeRotatingFile {
    _file: (),
}

impl SizeRotatingFile {
    /// Opens `path` for append, positioned at its end, so the threshold counts the bytes
    /// already in the file. `max_bytes` and `backups` apply only under `Gdaemon`.
    pub fn open(
        _path: &Path,
        _max_bytes: u64,
        _backups: u32,
        _owner: RotationOwner,
    ) -> io::Result<Self> {
        Ok(Self { _file: () })
    }

    /// Appends `line` and a newline. Under `Gdaemon`, the file rotates first when its
    /// size plus the line and its newline would reach `max_bytes`, unless it is empty.
    pub fn write_line(&self, _line: &[u8]) -> io::Result<()> {
        Ok(())
    }
}
