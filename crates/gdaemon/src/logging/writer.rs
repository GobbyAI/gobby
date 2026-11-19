//! The size-rotating, append-only log file.
//!
//! Every handle opens with append, so two writers on one file never overwrite each other.
//! Before each line the writer compares the path's identity with its open handle and
//! reopens when they differ, so a file another process renamed away is replaced by a new
//! one at the path.

use std::fs::{File, Metadata, OpenOptions};
use std::io::{self, Write as _};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, PoisonError};

/// Who rotates the files: the worker (Python's handler renames; this writer only
/// reopens) or gdaemon (this writer rotates as `RotatingFileHandler` does).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RotationOwner {
    Worker,
    Gdaemon,
}

/// The rotation owner: the worker whenever a backend command is configured.
pub fn owner_for(backend_command: Option<&str>) -> RotationOwner {
    match backend_command {
        Some(_) => RotationOwner::Worker,
        None => RotationOwner::Gdaemon,
    }
}

/// A file's identity: (device, inode) on Unix, creation time elsewhere (Windows).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FileIdentity {
    #[cfg(unix)]
    device: u64,
    #[cfg(unix)]
    inode: u64,
    #[cfg(not(unix))]
    created: Option<std::time::SystemTime>,
}

impl FileIdentity {
    fn of(metadata: &Metadata) -> Self {
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt as _;
            Self {
                device: metadata.dev(),
                inode: metadata.ino(),
            }
        }
        #[cfg(not(unix))]
        {
            Self {
                created: metadata.created().ok(),
            }
        }
    }
}

/// The identity of the file at `path`.
pub fn identity_of(path: &Path) -> io::Result<FileIdentity> {
    std::fs::metadata(path).map(|metadata| FileIdentity::of(&metadata))
}

/// The share mode the Windows writer opens with: read, write and delete, so another
/// process can rename or remove the file while this handle is open.
#[cfg(windows)]
pub const SHARE_MODE: u32 = 0x0000_0001 | 0x0000_0002 | 0x0000_0004;

/// One log file with size rotation.
#[derive(Debug)]
pub struct SizeRotatingFile {
    path: PathBuf,
    max_bytes: u64,
    backups: u32,
    owner: RotationOwner,
    handle: Mutex<Option<OpenFile>>,
}

#[derive(Debug)]
struct OpenFile {
    file: File,
    identity: FileIdentity,
}

impl SizeRotatingFile {
    /// Opens `path` for append, positioned at its end, so the threshold counts the bytes
    /// already in the file. `max_bytes` and `backups` apply only under `Gdaemon`.
    pub fn open(
        path: &Path,
        max_bytes: u64,
        backups: u32,
        owner: RotationOwner,
    ) -> io::Result<Self> {
        let handle = open_append(path)?;
        Ok(Self {
            path: path.to_path_buf(),
            max_bytes,
            backups,
            owner,
            handle: Mutex::new(Some(handle)),
        })
    }

    /// Appends `line` and a newline. Under `Gdaemon`, the file rotates first when its
    /// size plus the line and its newline would reach `max_bytes`, unless it is empty.
    pub fn write_line(&self, line: &[u8]) -> io::Result<()> {
        let mut handle = self.handle.lock().unwrap_or_else(PoisonError::into_inner);
        // One stat of the path serves both the identity check and the size, so bytes
        // another writer appended count toward the threshold.
        let at_path = match std::fs::metadata(&self.path) {
            Ok(metadata) => Some(metadata),
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(error) => return Err(error),
        };
        let current = at_path
            .as_ref()
            .zip(handle.as_ref())
            .is_some_and(|(metadata, open)| FileIdentity::of(metadata) == open.identity);
        let size = if current {
            at_path.as_ref().map_or(0, Metadata::len)
        } else {
            *handle = None;
            let reopened = open_append(&self.path)?;
            let size = reopened.file.metadata()?.len();
            *handle = Some(reopened);
            size
        };
        let line_bytes = u64::try_from(line.len())
            .unwrap_or(u64::MAX)
            .saturating_add(1);
        if self.rotates_before(size, line_bytes) {
            // Close the handle first: Windows cannot rename an open file.
            *handle = None;
            self.rotate()?;
            *handle = Some(open_append(&self.path)?);
        }
        let open = handle
            .as_mut()
            .ok_or_else(|| io::Error::other("log file handle is not open"))?;
        let mut buffer = Vec::with_capacity(line.len() + 1);
        buffer.extend_from_slice(line);
        buffer.push(b'\n');
        open.file.write_all(&buffer)
    }

    /// `RotatingFileHandler.shouldRollover`: a non-empty file whose size plus the next
    /// line would reach the limit. CPython never rolls over an empty file (gh-116263),
    /// even when one line alone reaches the limit. The worker's handler rotates its own
    /// files.
    fn rotates_before(&self, size: u64, line_bytes: u64) -> bool {
        self.owner == RotationOwner::Gdaemon
            && self.max_bytes > 0
            && self.backups > 0
            && size > 0
            && size.saturating_add(line_bytes) >= self.max_bytes
    }

    /// `RotatingFileHandler.doRollover`: `.N-1` becomes `.N` (the oldest is replaced),
    /// down to the active file becoming `.1`.
    fn rotate(&self) -> io::Result<()> {
        for index in (1..self.backups).rev() {
            let source = self.backup_path(index);
            if source.exists() {
                replace(&source, &self.backup_path(index + 1))?;
            }
        }
        if self.path.exists() {
            replace(&self.path, &self.backup_path(1))?;
        }
        Ok(())
    }

    fn backup_path(&self, index: u32) -> PathBuf {
        let mut name = self.path.as_os_str().to_owned();
        name.push(format!(".{index}"));
        PathBuf::from(name)
    }
}

/// Renames `source` over `destination`, removing `destination` first as Python does.
fn replace(source: &Path, destination: &Path) -> io::Result<()> {
    match std::fs::remove_file(destination) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    std::fs::rename(source, destination)
}

fn open_append(path: &Path) -> io::Result<OpenFile> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut options = OpenOptions::new();
    options.create(true).append(true);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt as _;
        options.share_mode(SHARE_MODE);
    }
    let file = options.open(path)?;
    let identity = FileIdentity::of(&file.metadata()?);
    Ok(OpenFile { file, identity })
}
