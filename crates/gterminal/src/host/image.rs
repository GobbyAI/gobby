//! Content-addressed host images: the host runs from a private pin under the
//! socket directory, so promoting the installed `gterm` never replaces the
//! bytes a live host (or its handover fallback) execs.

use std::fs;
use std::io;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

/// Directory holding the pins, under the host socket directory.
pub const IMAGES_DIR: &str = "gterm-images";

const PIN_PREFIX: &str = "gterm-";
const PIN_MODE: u32 = 0o700;

/// A pinned image: `<images_dir>/gterm-<sha256>`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PinnedImage {
    pub path: PathBuf,
    pub sha256: String,
}

impl PinnedImage {
    /// Re-hashes the pin and refuses a mismatch; run before any exec.
    pub fn verify(&self) -> io::Result<()> {
        let actual = sha256_file(&self.path)?;
        if actual != self.sha256 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "pinned image {} hashes to {actual}, expected {}",
                    self.path.display(),
                    self.sha256
                ),
            ));
        }
        Ok(())
    }

    /// A placeholder image for in-process `HostState` tests, which never exec.
    #[cfg(test)]
    pub(crate) fn for_tests() -> Self {
        Self {
            path: PathBuf::from("gterm-test"),
            sha256: "test".to_string(),
        }
    }
}

/// Pins `src`, or the binary it links to, into `images_dir`. A hard link to a
/// symlink links the symlink itself on macOS, so the pin would not be a binary.
pub fn pin_image(images_dir: &Path, src: &Path) -> io::Result<PinnedImage> {
    let src = fs::canonicalize(src)?;
    pin_image_linking(images_dir, &src, |from, to| fs::hard_link(from, to))
}

/// `pin_image` with the hard-link step supplied, so a cross-device link
/// failure can be exercised.
pub fn pin_image_linking(
    images_dir: &Path,
    src: &Path,
    link: impl Fn(&Path, &Path) -> io::Result<()>,
) -> io::Result<PinnedImage> {
    ensure_private_dir(images_dir)?;
    let temp = images_dir.join(format!(".gterm-pin-{}", uuid::Uuid::new_v4()));
    // Any failure after staging starts removes the temp: a partial link or
    // copy is never a `gterm-*` pin, so `prune_images` would not reclaim it.
    let pinned = stage_pin(src, &temp, link).and_then(|()| finish_pin(images_dir, &temp));
    if pinned.is_err() {
        let _ = fs::remove_file(&temp);
    }
    pinned
}

/// Links `src` to `temp`, copying instead when the link is unsupported.
fn stage_pin(
    src: &Path,
    temp: &Path,
    link: impl Fn(&Path, &Path) -> io::Result<()>,
) -> io::Result<()> {
    match link(src, temp) {
        Ok(()) => Ok(()),
        Err(err) if link_unsupported(&err) => fs::copy(src, temp).map(drop),
        Err(err) => Err(err),
    }
}

/// Hashes the staged link or copy and moves it to its content name, reusing
/// an intact pin already there.
fn finish_pin(images_dir: &Path, temp: &Path) -> io::Result<PinnedImage> {
    let sha256 = sha256_file(temp)?;
    let path = images_dir.join(format!("{PIN_PREFIX}{sha256}"));
    let existing = PinnedImage {
        path: path.clone(),
        sha256: sha256.clone(),
    };
    if existing.verify().is_ok() && mode(&path)? == PIN_MODE {
        fs::remove_file(temp)?;
        return Ok(existing);
    }
    // A hard link shares the source inode, so this also narrows the source
    // to owner-only; the source is this user's own installed binary.
    fs::set_permissions(temp, fs::Permissions::from_mode(PIN_MODE))?;
    fs::rename(temp, &path)?;
    Ok(existing)
}

/// The pin `path` is, when it sits directly under `images_dir` and its name
/// matches its content hash.
pub fn pinned_image(images_dir: &Path, path: &Path) -> io::Result<Option<PinnedImage>> {
    let (Some(parent), Some(name)) = (path.parent(), path.file_name()) else {
        return Ok(None);
    };
    let Some(claimed) = name.to_str().and_then(|name| name.strip_prefix(PIN_PREFIX)) else {
        return Ok(None);
    };
    let same_dir = match (fs::canonicalize(parent), fs::canonicalize(images_dir)) {
        (Ok(parent), Ok(images_dir)) => parent == images_dir,
        _ => false,
    };
    if !same_dir || sha256_file(path)? != claimed {
        return Ok(None);
    }
    Ok(Some(PinnedImage {
        path: path.to_path_buf(),
        sha256: claimed.to_string(),
    }))
}

/// Whether `path` is a pin under `images_dir`.
pub fn is_pinned(images_dir: &Path, path: &Path) -> io::Result<bool> {
    Ok(pinned_image(images_dir, path)?.is_some())
}

/// Removes every `gterm-*` pin except `keep`. Only a host that owns the
/// sockets calls this.
pub fn prune_images(images_dir: &Path, keep: &PinnedImage) -> io::Result<()> {
    let keep_name = keep.path.file_name();
    for entry in fs::read_dir(images_dir)? {
        let entry = entry?;
        let name = entry.file_name();
        if name.to_string_lossy().starts_with(PIN_PREFIX) && Some(name.as_os_str()) != keep_name {
            fs::remove_file(entry.path())?;
        }
    }
    Ok(())
}

/// Removes a candidate pin unless it is the running image.
pub fn remove_candidate(candidate: &PinnedImage, running: &PinnedImage) -> io::Result<()> {
    if candidate.sha256 == running.sha256 {
        return Ok(());
    }
    fs::remove_file(&candidate.path)
}

/// Creates `dir` with mode 0700 when missing, and refuses one this user does
/// not own or that others can write.
fn ensure_private_dir(dir: &Path) -> io::Result<()> {
    match fs::DirBuilder::new().mode(PIN_MODE).create(dir) {
        Ok(()) => {}
        Err(err) if err.kind() == io::ErrorKind::AlreadyExists => {}
        Err(err) => return Err(err),
    }
    let metadata = fs::symlink_metadata(dir)?;
    // SAFETY: geteuid has no preconditions and cannot fail.
    let euid = unsafe { libc::geteuid() };
    if !metadata.is_dir() || metadata.uid() != euid || metadata.mode() & 0o022 != 0 {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            format!(
                "unsafe gterm images directory {}: must be a directory owned by uid {euid} and not writable by others",
                dir.display()
            ),
        ));
    }
    Ok(())
}

fn link_unsupported(err: &io::Error) -> bool {
    matches!(
        err.kind(),
        io::ErrorKind::CrossesDevices | io::ErrorKind::Unsupported
    ) || matches!(
        err.raw_os_error(),
        Some(libc::EXDEV | libc::ENOTSUP | libc::EPERM | libc::EMLINK)
    )
}

fn mode(path: &Path) -> io::Result<u32> {
    Ok(fs::metadata(path)?.permissions().mode() & 0o777)
}

fn sha256_file(path: &Path) -> io::Result<String> {
    let mut file = fs::File::open(path)?;
    let mut hasher = Sha256::new();
    io::copy(&mut file, &mut hasher)?;
    Ok(format!("{:x}", hasher.finalize()))
}
