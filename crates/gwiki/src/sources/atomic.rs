use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};

use crate::WikiError;

pub(crate) fn write_atomic(
    path: &Path,
    contents: &[u8],
    action: &'static str,
) -> Result<(), WikiError> {
    let temp_path = temp_sibling_path(path);
    let mut file = File::create(&temp_path).map_err(|error| WikiError::Io {
        action: "create raw source index temp file",
        path: Some(temp_path.clone()),
        source: error,
    })?;
    if let Err(error) = file.write_all(contents) {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action,
            path: Some(temp_path),
            source: error,
        });
    }
    if let Err(error) = file.sync_all() {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "sync raw source index temp file",
            path: Some(temp_path),
            source: error,
        });
    }
    drop(file);
    if let Err(error) = replace_atomic(&temp_path, path) {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action,
            path: Some(path.to_path_buf()),
            source: error,
        });
    }
    sync_parent_dir(path)
}

fn replace_atomic(temp_path: &Path, path: &Path) -> std::io::Result<()> {
    #[cfg(windows)]
    {
        match fs::remove_file(path) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
    }
    fs::rename(temp_path, path)
}

fn temp_sibling_path(path: &Path) -> PathBuf {
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("INDEX.md");
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
                action: "sync raw source index directory",
                path: Some(parent.to_path_buf()),
                source: error,
            })
    }
}
