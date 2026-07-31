use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};

use crate::WikiError;
use crate::paths::safe_vault_relative_path;
use crate::sources::atomic::sync_parent_dir;

pub(super) fn write_immutable(
    vault_root: &Path,
    relative: &Path,
    bytes: &[u8],
) -> Result<(), WikiError> {
    let path = prepare_immutable_path(vault_root, relative)?;

    if path.exists() {
        return validate_existing_raw_bytes(&path, relative, bytes);
    }
    let mut temp_file = create_raw_temp_file(&path)?;
    if let Err(error) = temp_file.write_all(bytes) {
        return Err(WikiError::Io {
            action: "write raw source temp file",
            path: Some(temp_file.path().to_path_buf()),
            source: error,
        });
    }
    if let Err(error) = temp_file.as_file().sync_all() {
        return Err(WikiError::Io {
            action: "sync raw source temp file",
            path: Some(temp_file.path().to_path_buf()),
            source: error,
        });
    }
    match temp_file.persist_noclobber(&path) {
        Ok(_) => sync_parent_dir(&path),
        Err(error) if error.error.kind() == ErrorKind::AlreadyExists => {
            validate_existing_raw_bytes(&path, relative, bytes)
        }
        Err(error) => Err(WikiError::Io {
            action: "write raw source",
            path: Some(path),
            source: error.error,
        }),
    }
}

pub(super) fn write_immutable_file(
    vault_root: &Path,
    relative: &Path,
    source_path: &Path,
    content_hash: &str,
) -> Result<(), WikiError> {
    let source_hash = validate_source_file_hash(source_path, content_hash)?;
    let path = prepare_immutable_path(vault_root, relative)?;

    if path.exists() {
        return validate_existing_raw_file(&path, relative, &source_hash);
    }
    let mut temp_file = create_raw_temp_file(&path)?;
    let mut source = std::fs::File::open(source_path).map_err(|error| WikiError::Io {
        action: "open raw source",
        path: Some(source_path.to_path_buf()),
        source: error,
    })?;
    if let Err(error) = std::io::copy(&mut source, &mut temp_file) {
        return Err(WikiError::Io {
            action: "write raw source temp file",
            path: Some(temp_file.path().to_path_buf()),
            source: error,
        });
    }
    if let Err(error) = temp_file.as_file().sync_all() {
        return Err(WikiError::Io {
            action: "sync raw source temp file",
            path: Some(temp_file.path().to_path_buf()),
            source: error,
        });
    }
    match temp_file.persist_noclobber(&path) {
        Ok(_) => sync_parent_dir(&path),
        Err(error) if error.error.kind() == ErrorKind::AlreadyExists => {
            validate_existing_raw_file(&path, relative, &source_hash)
        }
        Err(error) => Err(WikiError::Io {
            action: "write raw source",
            path: Some(path),
            source: error.error,
        }),
    }
}

pub(super) fn prepare_immutable_path(
    vault_root: &Path,
    relative: &Path,
) -> Result<PathBuf, WikiError> {
    let relative = safe_vault_relative_path(relative)?;
    let root = vault_root.canonicalize().map_err(|error| WikiError::Io {
        action: "resolve vault root",
        path: Some(vault_root.to_path_buf()),
        source: error,
    })?;
    let candidate = root.join(&relative);
    let parent = candidate
        .parent()
        .ok_or_else(|| raw_path_outside_vault(&relative))?;
    let parent = canonicalize_existing_prefix(parent)?;
    if !parent.starts_with(&root) {
        return Err(raw_path_outside_vault(&relative));
    }
    std::fs::create_dir_all(&parent).map_err(|error| WikiError::Io {
        action: "create raw source directory",
        path: Some(parent.clone()),
        source: error,
    })?;
    let parent = parent.canonicalize().map_err(|error| WikiError::Io {
        action: "resolve raw source directory",
        path: Some(parent),
        source: error,
    })?;
    if !parent.starts_with(&root) {
        return Err(raw_path_outside_vault(&relative));
    }
    let file_name = candidate
        .file_name()
        .ok_or_else(|| raw_path_outside_vault(&relative))?;
    let path = parent.join(file_name);
    match std::fs::symlink_metadata(&path) {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            return Err(WikiError::InvalidInput {
                field: "raw_path",
                message: format!(
                    "immutable raw source path {} must not be a symbolic link",
                    relative.display()
                ),
            });
        }
        Ok(_) => {}
        Err(error) if error.kind() == ErrorKind::NotFound => {}
        Err(error) => {
            return Err(WikiError::Io {
                action: "inspect raw source path",
                path: Some(path),
                source: error,
            });
        }
    }
    Ok(path)
}

fn canonicalize_existing_prefix(path: &Path) -> Result<PathBuf, WikiError> {
    let mut current = path;
    let mut missing_suffix = Vec::new();
    loop {
        match std::fs::symlink_metadata(current) {
            Ok(_) => break,
            Err(error) if error.kind() == ErrorKind::NotFound => {
                let Some(name) = current.file_name() else {
                    break;
                };
                missing_suffix.push(name.to_os_string());
                let Some(parent) = current.parent() else {
                    break;
                };
                current = parent;
            }
            Err(error) => {
                return Err(WikiError::Io {
                    action: "inspect raw source directory",
                    path: Some(current.to_path_buf()),
                    source: error,
                });
            }
        }
    }

    let mut resolved = current.canonicalize().map_err(|error| WikiError::Io {
        action: "resolve raw source directory",
        path: Some(current.to_path_buf()),
        source: error,
    })?;
    for component in missing_suffix.iter().rev() {
        resolved.push(component);
    }
    Ok(resolved)
}

fn raw_path_outside_vault(relative: &Path) -> WikiError {
    WikiError::InvalidInput {
        field: "raw_path",
        message: format!(
            "immutable raw source path {} must stay inside the vault",
            relative.display()
        ),
    }
}

pub(super) fn validate_existing_raw_bytes(
    path: &Path,
    relative: &Path,
    bytes: &[u8],
) -> Result<(), WikiError> {
    let existing_hash =
        gobby_core::indexing::file_content_hash(path).map_err(|error| WikiError::Io {
            action: "hash existing raw source",
            path: Some(path.to_path_buf()),
            source: error,
        })?;
    if existing_hash == gobby_core::indexing::content_hash(bytes) {
        return Ok(());
    }
    Err(immutable_exists_error(relative))
}

fn validate_existing_raw_file(
    path: &Path,
    relative: &Path,
    source_hash: &str,
) -> Result<(), WikiError> {
    let existing_hash =
        gobby_core::indexing::file_content_hash(path).map_err(|error| WikiError::Io {
            action: "hash existing raw source",
            path: Some(path.to_path_buf()),
            source: error,
        })?;
    if existing_hash == source_hash {
        return Ok(());
    }
    Err(immutable_exists_error(relative))
}

fn validate_source_file_hash(source_path: &Path, content_hash: &str) -> Result<String, WikiError> {
    let source_hash =
        gobby_core::indexing::file_content_hash(source_path).map_err(|error| WikiError::Io {
            action: "hash raw source",
            path: Some(source_path.to_path_buf()),
            source: error,
        })?;
    if source_hash == content_hash {
        return Ok(source_hash);
    }
    Err(WikiError::InvalidInput {
        field: "content_hash",
        message: format!(
            "declared content hash does not match source file {}",
            source_path.display()
        ),
    })
}

fn immutable_exists_error(relative: &Path) -> WikiError {
    WikiError::InvalidInput {
        field: "raw_path",
        message: format!(
            "immutable raw source already exists at {}",
            relative.display()
        ),
    }
}

fn create_raw_temp_file(path: &Path) -> Result<tempfile::NamedTempFile, WikiError> {
    let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    else {
        return Err(WikiError::Io {
            action: "create raw source temp file",
            path: Some(path.to_path_buf()),
            source: std::io::Error::new(
                ErrorKind::InvalidInput,
                "raw source target has no parent directory",
            ),
        });
    };
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("source");
    tempfile::Builder::new()
        .prefix(&format!(".{file_name}."))
        .suffix(".tmp")
        .tempfile_in(parent)
        .map_err(|source| WikiError::Io {
            action: "create raw source temp file",
            path: Some(parent.to_path_buf()),
            source,
        })
}
