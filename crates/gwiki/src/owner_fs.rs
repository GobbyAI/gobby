use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use crate::WikiError;

#[derive(Debug)]
pub struct OwnerRoot {
    path: PathBuf,
    #[cfg(unix)]
    identity: (u64, u64),
    #[cfg(unix)]
    dir: File,
}

impl OwnerRoot {
    pub fn open(path: &Path) -> Result<Self, WikiError> {
        if path
            .symlink_metadata()
            .map(|meta| meta.file_type().is_symlink())
            .unwrap_or(false)
        {
            return Err(files_home_error(path, "files_home is a symlink"));
        }
        if !path.is_dir() {
            return Err(files_home_error(
                path,
                "files_home is missing or not a directory",
            ));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
            let dir = OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
                .open(path)
                .map_err(|source| WikiError::Io {
                    action: "open files_home",
                    path: Some(path.to_path_buf()),
                    source,
                })?;
            let meta = dir.metadata().map_err(|source| WikiError::Io {
                action: "stat files_home",
                path: Some(path.to_path_buf()),
                source,
            })?;
            Ok(Self {
                path: path.to_path_buf(),
                identity: (meta.dev(), meta.ino()),
                dir,
            })
        }
        #[cfg(not(unix))]
        {
            let _ = File::open(path);
            Ok(Self {
                path: path.to_path_buf(),
            })
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn assert_identity(&self) -> Result<(), WikiError> {
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            let meta = self.dir.metadata().map_err(|source| WikiError::Io {
                action: "restat files_home",
                path: Some(self.path.clone()),
                source,
            })?;
            if (meta.dev(), meta.ino()) != self.identity {
                return Err(files_home_error(&self.path, "files_home was replaced"));
            }
            let current = match std::fs::symlink_metadata(&self.path) {
                Ok(meta) => meta,
                Err(source) => {
                    return Err(WikiError::Io {
                        action: "stat files_home path",
                        path: Some(self.path.clone()),
                        source,
                    });
                }
            };
            if current.file_type().is_symlink() || !current.is_dir() {
                return Err(files_home_error(&self.path, "files_home was replaced"));
            }
            use std::os::unix::fs::OpenOptionsExt;
            let current_file = OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
                .open(&self.path)
                .map_err(|_| files_home_error(&self.path, "files_home was replaced"))?;
            let current_meta = current_file.metadata().map_err(|source| WikiError::Io {
                action: "stat replacement files_home",
                path: Some(self.path.clone()),
                source,
            })?;
            if (current_meta.dev(), current_meta.ino()) != self.identity {
                return Err(files_home_error(&self.path, "files_home was replaced"));
            }
        }
        Ok(())
    }

    pub fn create_dir_all(&self, relative: &Path) -> Result<(), WikiError> {
        self.assert_identity()?;
        #[cfg(unix)]
        {
            create_descendants(&self.dir, relative)?;
        }
        #[cfg(not(unix))]
        {
            std::fs::create_dir_all(self.path.join(relative)).map_err(|source| WikiError::Io {
                action: "create files_home descendant",
                path: Some(self.path.join(relative)),
                source,
            })?;
        }
        self.assert_identity()
    }

    pub fn write_file_if_absent(&self, relative: &Path, contents: &str) -> Result<bool, WikiError> {
        if let Some(parent) = relative.parent()
            && !parent.as_os_str().is_empty()
        {
            self.create_dir_all(parent)?;
        }
        self.assert_identity()?;
        let dest = self.path.join(relative);
        match OpenOptions::new().write(true).create_new(true).open(&dest) {
            Ok(mut file) => {
                file.write_all(contents.as_bytes()).map_err(|source| {
                    let _ = std::fs::remove_file(&dest);
                    WikiError::Io {
                        action: "write files_home descendant",
                        path: Some(dest),
                        source,
                    }
                })?;
                self.assert_identity()?;
                Ok(true)
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => Ok(false),
            Err(source) => Err(WikiError::Io {
                action: "create files_home descendant",
                path: Some(dest),
                source,
            }),
        }
    }

    pub fn replace_file(&self, relative: &Path, contents: &[u8]) -> Result<(), WikiError> {
        if let Some(parent) = relative.parent()
            && !parent.as_os_str().is_empty()
        {
            self.create_dir_all(parent)?;
        }
        self.assert_identity()?;
        let dest = self.path.join(relative);
        let temp = dest.with_file_name(format!(
            ".{}.{}.tmp",
            dest.file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("file"),
            std::process::id()
        ));
        std::fs::write(&temp, contents).map_err(|source| WikiError::Io {
            action: "write files_home temp",
            path: Some(temp.clone()),
            source,
        })?;
        std::fs::rename(&temp, &dest).map_err(|source| {
            let _ = std::fs::remove_file(&temp);
            WikiError::Io {
                action: "replace files_home descendant",
                path: Some(dest),
                source,
            }
        })?;
        self.assert_identity()
    }
}

fn files_home_error(path: &Path, detail: &str) -> WikiError {
    WikiError::Config {
        detail: format!("{detail}: {}", path.display()),
    }
}

#[cfg(unix)]
fn create_descendants(root: &File, relative: &Path) -> Result<(), WikiError> {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::io::{AsRawFd, FromRawFd};

    let mut current_fd = root.as_raw_fd();
    let mut opened: Vec<File> = Vec::new();
    for component in relative.components() {
        let std::path::Component::Normal(name) = component else {
            continue;
        };
        let c_name = CString::new(name.as_bytes()).map_err(|_| WikiError::InvalidScope {
            detail: format!("invalid files_home descendant {}", relative.display()),
        })?;
        let flags = libc::O_DIRECTORY | libc::O_RDONLY | libc::O_CLOEXEC | libc::O_NOFOLLOW;
        let existing = unsafe { libc::openat(current_fd, c_name.as_ptr(), flags) };
        if existing >= 0 {
            let file = unsafe { File::from_raw_fd(existing) };
            current_fd = file.as_raw_fd();
            opened.push(file);
            continue;
        }
        let created = unsafe { libc::mkdirat(current_fd, c_name.as_ptr(), 0o755) };
        if created != 0 {
            let err = std::io::Error::last_os_error();
            if err.raw_os_error() != Some(libc::EEXIST) {
                return Err(WikiError::Io {
                    action: "mkdir files_home descendant",
                    path: Some(relative.to_path_buf()),
                    source: err,
                });
            }
        }
        let opened_fd = unsafe { libc::openat(current_fd, c_name.as_ptr(), flags) };
        if opened_fd < 0 {
            return Err(WikiError::Io {
                action: "open files_home descendant",
                path: Some(relative.to_path_buf()),
                source: std::io::Error::last_os_error(),
            });
        }
        let file = unsafe { File::from_raw_fd(opened_fd) };
        current_fd = file.as_raw_fd();
        opened.push(file);
    }
    let _ = opened;
    Ok(())
}

pub fn owner_for_files_home() -> Result<Option<OwnerRoot>, WikiError> {
    let view =
        gobby_core::bootstrap::read_files_home_view().map_err(|error| WikiError::Config {
            detail: error.to_string(),
        })?;
    let Some(files_home) = view.files_home else {
        return Ok(None);
    };
    Ok(Some(OwnerRoot::open(&files_home)?))
}
