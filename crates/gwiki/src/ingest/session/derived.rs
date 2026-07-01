use std::io::Write;
use std::path::{Path, PathBuf};

use tempfile::{Builder, NamedTempFile};

use crate::WikiError;
use crate::paths::derived_markdown_path;
use crate::sources::SourceRecord;

pub(crate) fn write_session_derived_markdown(
    vault_root: &Path,
    record: &SourceRecord,
    markdown: &str,
) -> Result<PathBuf, WikiError> {
    let relative_path = derived_markdown_path(record)?;
    let path = vault_root.join(&relative_path);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create session derived markdown directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }
    write_session_markdown_atomically(&path, markdown.as_bytes())?;
    Ok(relative_path)
}

fn write_session_markdown_atomically(path: &Path, contents: &[u8]) -> Result<(), WikiError> {
    let mut temp_file = create_session_temp_file(path)?;
    if let Err(error) = temp_file.write_all(contents) {
        return Err(WikiError::Io {
            action: "write session derived markdown temp file",
            path: Some(temp_file.path().to_path_buf()),
            source: error,
        });
    }
    if let Err(error) = temp_file.as_file().sync_all() {
        return Err(WikiError::Io {
            action: "sync session derived markdown temp file",
            path: Some(temp_file.path().to_path_buf()),
            source: error,
        });
    }
    if let Err(error) = temp_file.persist(path) {
        return Err(WikiError::Io {
            action: "replace session derived markdown",
            path: Some(path.to_path_buf()),
            source: error.error,
        });
    }
    sync_parent_dir(path)
}

fn create_session_temp_file(path: &Path) -> Result<NamedTempFile, WikiError> {
    let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    else {
        return Err(WikiError::Io {
            action: "create session derived markdown temp file",
            path: Some(path.to_path_buf()),
            source: std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "session derived target has no parent directory",
            ),
        });
    };
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("session.md");
    Builder::new()
        .prefix(&format!(".{file_name}."))
        .suffix(".tmp")
        .tempfile_in(parent)
        .map_err(|source| WikiError::Io {
            action: "create session derived markdown temp file",
            path: Some(parent.to_path_buf()),
            source,
        })
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
        let dir = std::fs::File::open(parent).map_err(|error| WikiError::Io {
            action: "open session derived markdown parent directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
        dir.sync_all().map_err(|error| WikiError::Io {
            action: "sync session derived markdown parent directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })
    }
}
