use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use crate::WikiError;

use super::paths::{ensure_existing_parent_inside_vault, ensure_synthesized_path_inside_vault};
use super::types::{PageWriteKind, PageWriteOutcome, SynthesizedPage, WritePolicy};

/// Advisory preflight for callers that want to fail before expensive synthesis.
///
/// The actual race-free protection lives in `write_synthesized_page`, which
/// uses `create_new` for `RequireMergeIntent` and atomic replacement for
/// overwrite-after-merge writes.
pub fn ensure_page_write_allowed(
    page: &SynthesizedPage,
    policy: WritePolicy,
) -> Result<(), WikiError> {
    if page.path.exists() && policy == WritePolicy::RequireMergeIntent {
        return Err(WikiError::InvalidInput {
            field: "write_intent",
            message: format!(
                "existing page {} requires merge/diff handling before overwrite",
                page.path.display()
            ),
        });
    }
    Ok(())
}

pub fn write_synthesized_page(
    vault_root: &Path,
    page: &SynthesizedPage,
    policy: WritePolicy,
) -> Result<PageWriteOutcome, WikiError> {
    let markdown = crate::markdown::normalize(&page.markdown);
    ensure_synthesized_path_inside_vault(vault_root, &page.path, "synthesized_page")?;
    if let Some(parent) = page.path.parent() {
        ensure_existing_parent_inside_vault(vault_root, parent, "synthesized_page")?;
        fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create synthesized page directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }

    let kind = match policy {
        WritePolicy::RequireMergeIntent => {
            let file = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&page.path)
                .map_err(|error| {
                    if error.kind() == std::io::ErrorKind::AlreadyExists {
                        WikiError::InvalidInput {
                            field: "write_intent",
                            message: format!(
                                "existing page {} requires merge/diff handling before overwrite",
                                page.path.display()
                            ),
                        }
                    } else {
                        WikiError::Io {
                            action: "create synthesized wiki page",
                            path: Some(page.path.clone()),
                            source: error,
                        }
                    }
                })?;
            write_created_synthesized_page(file, &page.path, markdown.as_bytes())?;
            sync_parent_dir(&page.path)?;
            PageWriteKind::Created
        }
        WritePolicy::AllowOverwriteAfterMerge => {
            match fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&page.path)
            {
                Ok(file) => {
                    write_created_synthesized_page(file, &page.path, markdown.as_bytes())?;
                    sync_parent_dir(&page.path)?;
                    PageWriteKind::Created
                }
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                    write_synthesized_page_atomically(&page.path, markdown.as_bytes())?;
                    PageWriteKind::Overwritten
                }
                Err(error) => {
                    return Err(WikiError::Io {
                        action: "create synthesized wiki page",
                        path: Some(page.path.clone()),
                        source: error,
                    });
                }
            }
        }
    };
    Ok(PageWriteOutcome {
        path: page.path.clone(),
        kind,
    })
}

fn write_created_synthesized_page(
    mut file: fs::File,
    path: &Path,
    contents: &[u8],
) -> Result<(), WikiError> {
    if let Err(error) = file.write_all(contents) {
        drop(file);
        let _ = fs::remove_file(path);
        return Err(WikiError::Io {
            action: "write synthesized wiki page",
            path: Some(path.to_path_buf()),
            source: error,
        });
    }
    if let Err(error) = file.sync_all() {
        drop(file);
        let _ = fs::remove_file(path);
        return Err(WikiError::Io {
            action: "write synthesized wiki page",
            path: Some(path.to_path_buf()),
            source: error,
        });
    }
    Ok(())
}

fn write_synthesized_page_atomically(path: &Path, contents: &[u8]) -> Result<(), WikiError> {
    let temp_path = temp_sibling_path(path);
    let mut file = fs::File::create(&temp_path).map_err(|error| WikiError::Io {
        action: "create synthesized page temp file",
        path: Some(temp_path.clone()),
        source: error,
    })?;
    if let Err(error) = file.write_all(contents) {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "write synthesized page temp file",
            path: Some(temp_path),
            source: error,
        });
    }
    if let Err(error) = file.sync_all() {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "sync synthesized page temp file",
            path: Some(temp_path),
            source: error,
        });
    }
    drop(file);
    if let Err(error) = fs::rename(&temp_path, path) {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "replace synthesized wiki page",
            path: Some(path.to_path_buf()),
            source: error,
        });
    }
    sync_parent_dir(path)?;
    Ok(())
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
        fs::File::open(parent)
            .and_then(|dir| dir.sync_all())
            .map_err(|error| WikiError::Io {
                action: "sync synthesized page directory",
                path: Some(parent.to_path_buf()),
                source: error,
            })
    }
}

fn temp_sibling_path(path: &Path) -> PathBuf {
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("page.md");
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    path.with_file_name(format!(".{file_name}.{}.{nanos}.tmp", std::process::id()))
}
