#[cfg(unix)]
use std::fs::File;
use std::fs::{self, OpenOptions};
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};

use crate::WikiError;

pub(super) fn write_video_markdown_atomic(path: &Path, bytes: &[u8]) -> Result<(), WikiError> {
    let temp_path = temp_sibling_path(path);
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temp_path)
        .map_err(|error| WikiError::Io {
            action: "create video derived markdown temp file",
            path: Some(temp_path.clone()),
            source: error,
        })?;
    if let Err(error) = file.write_all(bytes) {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "write video derived markdown temp file",
            path: Some(temp_path),
            source: error,
        });
    }
    if let Err(error) = file.sync_all() {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "sync video derived markdown temp file",
            path: Some(temp_path),
            source: error,
        });
    }
    drop(file);
    if let Err(error) = fs::rename(&temp_path, path) {
        if error.kind() == ErrorKind::AlreadyExists {
            if let Err(remove_error) = fs::remove_file(path) {
                let _ = fs::remove_file(&temp_path);
                return Err(WikiError::Io {
                    action: "replace existing video derived markdown",
                    path: Some(path.to_path_buf()),
                    source: remove_error,
                });
            }
            if let Err(rename_error) = fs::rename(&temp_path, path) {
                let _ = fs::remove_file(&temp_path);
                return Err(WikiError::Io {
                    action: "write video derived markdown",
                    path: Some(path.to_path_buf()),
                    source: rename_error,
                });
            }
            return sync_parent_dir(path);
        }
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "write video derived markdown",
            path: Some(path.to_path_buf()),
            source: error,
        });
    }
    sync_parent_dir(path)
}

fn temp_sibling_path(path: &Path) -> PathBuf {
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("video.md");
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    path.with_file_name(format!(".{file_name}.{}.{nanos}.tmp", std::process::id()))
}

fn sync_parent_dir(path: &Path) -> Result<(), WikiError> {
    #[cfg(not(unix))]
    {
        let _ = path;
        Ok(())
    }
    #[cfg(unix)]
    {
        let Some(parent) = path.parent() else {
            return Ok(());
        };
        File::open(parent)
            .and_then(|dir| dir.sync_all())
            .map_err(|error| WikiError::Io {
                action: "sync video derived markdown directory",
                path: Some(parent.to_path_buf()),
                source: error,
            })
    }
}
