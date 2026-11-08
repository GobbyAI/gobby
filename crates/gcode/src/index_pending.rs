//! Durable queue for index paths whose bounded lock wait ran out.
//!
//! A timed-out explicit run persists its paths as one batch file under
//! `<gobby home>/code-index-pending/<project_id>/`. The next index run takes every batch
//! before its own work and deletes a batch only after none of its paths is still busy, so
//! a queued path is indexed at least once and never dropped.

use crate::commands::receipt;
use anyhow::Context as _;
use serde::{Deserialize, Serialize};
use std::ffi::OsStr;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

const BATCH_PREFIX: &str = "batch-";
const BATCH_SUFFIX: &str = ".json";

#[derive(Serialize, Deserialize)]
struct Batch {
    paths: Vec<PathBuf>,
}

/// One persisted batch: the file that holds it and the project-relative paths it queues.
#[derive(Debug)]
pub(crate) struct PendingBatch {
    file: PathBuf,
    pub(crate) paths: Vec<PathBuf>,
}

/// The queue directory for one project, under the gobby home.
pub(crate) fn pending_dir(project_id: &str) -> anyhow::Result<PathBuf> {
    Ok(gobby_core::gobby_home()?
        .join("code-index-pending")
        .join(project_id))
}

/// Writes `paths` as a new batch in `dir`. Every call creates its own file, so a second
/// writer never overwrites a batch another run has not yet drained.
pub(crate) fn persist(dir: &Path, paths: &[PathBuf]) -> anyhow::Result<()> {
    if paths.is_empty() {
        return Ok(());
    }
    fs::create_dir_all(dir).with_context(|| format!("cannot create {}", dir.display()))?;
    let (_placeholder, file) = tempfile::Builder::new()
        .prefix(BATCH_PREFIX)
        .suffix(BATCH_SUFFIX)
        .tempfile_in(dir)?
        .keep()
        .map_err(|error| error.error)?;
    receipt::write_atomic(
        &file,
        &Batch {
            paths: paths.to_vec(),
        },
    )
}

/// Every batch in `dir`, oldest name first. A missing directory has none. A batch that
/// cannot be read stays on disk and is reported; it is never deleted.
pub(crate) fn take(dir: &Path) -> anyhow::Result<Vec<PendingBatch>> {
    let entries = match fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => {
            return Err(error).with_context(|| format!("cannot read {}", dir.display()));
        }
    };
    let mut files = entries
        .map(|entry| entry.map(|entry| entry.path()))
        .collect::<io::Result<Vec<_>>>()?;
    files.retain(|file| file.extension() == Some(OsStr::new("json")));
    files.sort();

    let mut batches = Vec::new();
    for file in files {
        match receipt::read_existing::<Batch>(&file) {
            Ok(Some(batch)) => batches.push(PendingBatch {
                file,
                paths: batch.paths,
            }),
            // Removed by a concurrent run after the listing: nothing left to take.
            Ok(None) => {}
            Err(error) => eprintln!(
                "warning: skipping unreadable index queue batch {}: {error:#}",
                file.display()
            ),
        }
    }
    Ok(batches)
}

/// Deletes a batch once its paths are indexed. A batch already removed is settled.
pub(crate) fn settle(batch: &PendingBatch) -> anyhow::Result<()> {
    match fs::remove_file(&batch.file) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error)
            .with_context(|| format!("cannot settle index queue batch {}", batch.file.display())),
    }
}

#[cfg(test)]
#[path = "index_pending/tests.rs"]
mod tests;
