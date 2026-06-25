use std::fs;
use std::io::{Error, ErrorKind};
use std::path::{Component, Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use super::{ExportArtifact, ExportRequest};
use crate::WikiError;

#[derive(Debug)]
pub(super) struct StagedExport {
    pub(super) artifact: ExportArtifact,
    pub(super) temp_path: PathBuf,
    pub(super) backup_path: Option<PathBuf>,
    pub(super) committed: bool,
}

pub fn write_export(root: &Path, request: ExportRequest) -> Result<ExportArtifact, WikiError> {
    let mut artifacts = write_export_batch(root, vec![request])?;
    Ok(artifacts.remove(0))
}

pub(super) fn write_export_batch(
    root: &Path,
    requests: Vec<ExportRequest>,
) -> Result<Vec<ExportArtifact>, WikiError> {
    let mut staged = Vec::with_capacity(requests.len());
    for (sequence, request) in requests.into_iter().enumerate() {
        match stage_export(root, request, sequence) {
            Ok(export) => staged.push(export),
            Err(error) => {
                cleanup_staged_exports(&staged);
                return Err(error);
            }
        }
    }
    if let Err(error) = commit_staged_exports(&mut staged) {
        rollback_staged_exports(&staged);
        return Err(error);
    }
    Ok(staged.into_iter().map(|export| export.artifact).collect())
}

fn stage_export(
    root: &Path,
    request: ExportRequest,
    sequence: usize,
) -> Result<StagedExport, WikiError> {
    let relative_path = export_relative_path(&request.filename)?;
    let path = root.join("outputs").join(relative_path);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create export directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }
    if path.is_dir() {
        return Err(WikiError::Io {
            action: "write export",
            path: Some(path),
            source: Error::new(ErrorKind::AlreadyExists, "export target is a directory"),
        });
    }

    let temp_path = export_sidecar_path(&path, "tmp", sequence);
    std::fs::write(&temp_path, &request.contents).map_err(|error| WikiError::Io {
        action: "write export",
        path: Some(temp_path.clone()),
        source: error,
    })?;

    Ok(StagedExport {
        artifact: ExportArtifact {
            path,
            kind: request.kind,
            bytes_written: request.contents.len(),
        },
        temp_path,
        backup_path: None,
        committed: false,
    })
}

pub(super) fn commit_staged_exports(staged: &mut [StagedExport]) -> Result<(), WikiError> {
    for (sequence, export) in staged.iter_mut().enumerate() {
        let target = &export.artifact.path;
        if target.exists() {
            if target.is_dir() {
                return Err(WikiError::Io {
                    action: "write export",
                    path: Some(target.clone()),
                    source: Error::new(ErrorKind::AlreadyExists, "export target is a directory"),
                });
            }
            let backup_path = export_sidecar_path(target, "backup", sequence);
            fs::rename(target, &backup_path).map_err(|error| WikiError::Io {
                action: "backup export",
                path: Some(target.clone()),
                source: error,
            })?;
            export.backup_path = Some(backup_path);
        }
    }

    for export in staged.iter_mut() {
        fs::rename(&export.temp_path, &export.artifact.path).map_err(|error| WikiError::Io {
            action: "commit export",
            path: Some(export.artifact.path.clone()),
            source: error,
        })?;
        export.committed = true;
    }

    for export in staged.iter() {
        if let Some(backup_path) = &export.backup_path {
            let _ = fs::remove_file(backup_path);
        }
    }
    Ok(())
}

fn cleanup_staged_exports(staged: &[StagedExport]) {
    for export in staged {
        let _ = fs::remove_file(&export.temp_path);
    }
}

pub(super) fn rollback_staged_exports(staged: &[StagedExport]) {
    for export in staged {
        let _ = fs::remove_file(&export.temp_path);
        if export.committed && export.backup_path.is_none() {
            let _ = fs::remove_file(&export.artifact.path);
        }
    }
    for export in staged {
        if let Some(backup_path) = &export.backup_path {
            if export.committed {
                let _ = fs::remove_file(&export.artifact.path);
            }
            let _ = fs::rename(backup_path, &export.artifact.path);
        }
    }
}

fn export_sidecar_path(path: &Path, kind: &str, sequence: usize) -> PathBuf {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    let file_name = path
        .file_name()
        .map(|name| name.to_string_lossy())
        .unwrap_or_else(|| "export".into());
    path.with_file_name(format!(".{file_name}.{kind}.{unique}.{sequence}"))
}

pub(super) fn export_relative_path(filename: &str) -> Result<PathBuf, WikiError> {
    let path = Path::new(filename);
    if filename.trim().is_empty() || path.is_absolute() {
        return Err(invalid_export_filename(filename));
    }

    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Normal(part) => normalized.push(part),
            Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => {
                return Err(invalid_export_filename(filename));
            }
        }
    }

    if normalized.as_os_str().is_empty() {
        return Err(invalid_export_filename(filename));
    }

    Ok(normalized)
}

fn invalid_export_filename(filename: &str) -> WikiError {
    WikiError::InvalidInput {
        field: "filename",
        message: format!("export filename must stay under outputs/: {filename}"),
    }
}
