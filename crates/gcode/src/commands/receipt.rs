//! Receipt files for guarded maintenance commands. Location checks and atomic writes
//! live here so retire-files and vector reconcile apply share one set of guards.

use std::fs::{self, File};
use std::io::{BufReader, BufWriter, Write};
use std::path::Path;

use anyhow::{Context as _, ensure};
use serde::Serialize;
use serde::de::DeserializeOwned;

/// Refuses a receipt path unless it is canonical, lies outside `root_path`, and sits in a
/// private directory owned by this user. An existing receipt must be a private owner-only file.
pub(crate) fn validate_location(path: &Path, root_path: &Path) -> anyhow::Result<()> {
    no_symlinks(path)?;
    let parent = path
        .parent()
        .context("receipt requires a parent directory")?;
    let filename = path.file_name().context("receipt requires a file name")?;
    ensure!(
        parent.canonicalize()?.join(filename) == path,
        "receipt path must be canonical"
    );
    ensure!(
        !path.starts_with(root_path),
        "receipt must stay outside the repository root"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let metadata = fs::metadata(parent)?;
        // SAFETY: getuid has no arguments and no memory-safety preconditions.
        let uid = unsafe { libc::getuid() };
        ensure!(
            metadata.is_dir() && metadata.uid() == uid && metadata.mode() & 0o077 == 0,
            "receipt parent must be a private owned directory"
        );
    }
    if path.exists() {
        require_private_file(&File::open(path)?)?;
    }
    Ok(())
}

/// Reads an existing receipt. `Ok(None)` means no file exists; a file that cannot be
/// opened or parsed is an error, so callers never mistake it for absence.
pub(crate) fn read_existing<T: DeserializeOwned>(path: &Path) -> anyhow::Result<Option<T>> {
    if !path.exists() {
        return Ok(None);
    }
    let file = File::open(path)?;
    Ok(Some(serde_json::from_reader(BufReader::new(file))?))
}

/// Writes `value` as JSON through a temp file in the same directory, syncs it, renames it
/// over `path`, then syncs the directory. The caller validates `path` first.
pub(crate) fn write_atomic<T: Serialize>(path: &Path, value: &T) -> anyhow::Result<()> {
    let parent = path.parent().context("receipt requires parent")?;
    let mut file = tempfile::NamedTempFile::new_in(parent)?;
    {
        let mut writer = BufWriter::new(file.as_file_mut());
        serde_json::to_writer(&mut writer, value)?;
        writer.write_all(b"\n")?;
        writer.flush()?;
    }
    file.as_file().sync_all()?;
    file.persist(path).map_err(|error| error.error)?;
    File::open(parent)?.sync_all()?;
    Ok(())
}

pub(crate) fn no_symlinks(path: &Path) -> anyhow::Result<()> {
    ensure!(
        path.is_absolute(),
        "path must be absolute: {}",
        path.display()
    );
    for ancestor in path.ancestors() {
        match fs::symlink_metadata(ancestor) {
            Ok(metadata) => ensure!(
                !metadata.file_type().is_symlink(),
                "symlink path refused: {}",
                ancestor.display()
            ),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

pub(crate) fn require_private_file(file: &File) -> anyhow::Result<()> {
    let metadata = file.metadata()?;
    ensure!(metadata.is_file(), "expected a regular private file");
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        // SAFETY: getuid has no arguments and no memory-safety preconditions.
        let uid = unsafe { libc::getuid() };
        ensure!(
            metadata.uid() == uid && metadata.mode() & 0o077 == 0,
            "file must be owner-only and owned by this user"
        );
    }
    #[cfg(not(unix))]
    anyhow::bail!("retirement requires Unix ownership validation");
    Ok(())
}
