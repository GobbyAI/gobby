use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};

use crate::WikiError;

pub(crate) fn write_atomic(
    path: &Path,
    contents: &[u8],
    action: &'static str,
) -> Result<(), WikiError> {
    let temp_path = temp_sibling_path(path)?;
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

fn temp_sibling_path(path: &Path) -> Result<PathBuf, WikiError> {
    let file_name = path
        .file_name()
        .ok_or_else(|| WikiError::Config {
            detail: format!(
                "atomic write path `{}` must include a file name",
                path.display()
            ),
        })?
        .to_str()
        .ok_or_else(|| WikiError::Config {
            detail: format!(
                "atomic write path `{}` must include a UTF-8 file name",
                path.display()
            ),
        })?;
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    Ok(path.with_file_name(format!(".{file_name}.{}.{nanos}.tmp", std::process::id())))
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn temp_sibling_path_rejects_missing_file_name() {
        assert!(matches!(
            temp_sibling_path(Path::new("/")),
            Err(WikiError::Config { .. })
        ));
    }

    #[cfg(unix)]
    #[test]
    fn temp_sibling_path_rejects_non_utf8_file_name() {
        use std::ffi::OsString;
        use std::os::unix::ffi::OsStringExt;

        let path = PathBuf::from(OsString::from_vec(vec![0xff]));
        assert!(matches!(
            temp_sibling_path(&path),
            Err(WikiError::Config { .. })
        ));
    }
}
