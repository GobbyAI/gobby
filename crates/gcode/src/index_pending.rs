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

/// The work one timed-out run left for the next run. A whole-tree pass has no finite path set,
/// so it persists as a marker that names only its scope.
#[derive(Serialize, Deserialize, Debug, PartialEq, Eq)]
#[serde(tag = "scope", rename_all = "snake_case")]
pub(crate) enum Batch {
    Paths {
        paths: Vec<PathBuf>,
    },
    WholeTree {
        path_filter: Option<PathBuf>,
        full: bool,
    },
}

/// One persisted batch: the file that holds it and the work it queues.
#[derive(Debug)]
pub(crate) struct PendingBatch {
    file: PathBuf,
    pub(crate) batch: Batch,
}

impl PendingBatch {
    /// The explicit paths this batch queues. A whole-tree marker queues none.
    pub(crate) fn paths(&self) -> &[PathBuf] {
        match &self.batch {
            Batch::Paths { paths } => paths,
            Batch::WholeTree { .. } => &[],
        }
    }
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
    write_batch(
        dir,
        &Batch::Paths {
            paths: paths.to_vec(),
        },
    )
}

/// Writes the marker for a whole-tree run (under `path_filter`, if any) that timed out waiting
/// for the project lock. The next run re-runs that scope with the same `full` setting.
pub(crate) fn persist_whole_tree(
    dir: &Path,
    path_filter: Option<&Path>,
    full: bool,
) -> anyhow::Result<()> {
    write_batch(
        dir,
        &Batch::WholeTree {
            path_filter: path_filter.map(Path::to_path_buf),
            full,
        },
    )
}

fn write_batch(dir: &Path, batch: &Batch) -> anyhow::Result<()> {
    fs::create_dir_all(dir).with_context(|| format!("cannot create {}", dir.display()))?;
    let (_placeholder, file) = tempfile::Builder::new()
        .prefix(BATCH_PREFIX)
        .suffix(BATCH_SUFFIX)
        .tempfile_in(dir)?
        .keep()
        .map_err(|error| error.error)?;
    receipt::write_atomic(&file, batch)
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
            Ok(Some(batch)) => batches.push(PendingBatch { file, batch }),
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
