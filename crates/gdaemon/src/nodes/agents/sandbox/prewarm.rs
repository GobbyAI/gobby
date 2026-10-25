//! Pre-commit store prewarm and its spare. Ports `sandbox_policy.py`'s
//! `_prewarm_pre_commit_store` and the spare helpers. A failed prewarm only warns.

use super::paths::expand_user;
use std::{
    collections::BTreeMap,
    fs::{self, DirBuilder, Permissions},
    io,
    os::unix::fs::{DirBuilderExt, PermissionsExt, symlink},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{Mutex, MutexGuard},
    thread::{self, JoinHandle},
};

const SPARE_NAME: &str = ".pre-commit-store-spare";
const SPARE_TEMP_NAME: &str = ".pre-commit-store-spare.tmp";

/// The one background replenisher per process (`_pre_commit_spare_thread`).
static SPARE_WORKER: Mutex<Option<JoinHandle<()>>> = Mutex::new(None);

/// `_prewarm_pre_commit_store`: copy the operator's pre-commit store into a run's cache,
/// consuming the managed spare when one exists. A run that clones starts the replenisher.
pub(super) fn prewarm(
    workspace: &Path,
    destination: &Path,
    managed_root: &Path,
    env: &BTreeMap<String, String>,
    home: &Path,
) {
    if !workspace.join(".pre-commit-config.yaml").is_file() {
        return;
    }
    let source = operator_store(env, home);
    if !source.is_dir() {
        return;
    }
    if let Some(parent) = destination.parent()
        && let Err(error) = create_private_dir(parent)
    {
        eprintln!("gdaemon: failed to prepare the run pre-commit cache: {error}");
        return;
    }
    if consume_spare(managed_root, destination) {
        // A consumed spare ends this run's prewarm. The refill waits for the next run that
        // has to clone, so this call never recreates the spare it just used.
        return;
    }
    if let Err(error) = clone_store(&source, destination) {
        // The store is only a prewarm; pre-commit rebuilds hooks into an empty cache.
        eprintln!("gdaemon: failed to prewarm pre-commit store: {error}");
        let _ = remove_store(destination);
    }
    schedule_spare(source, managed_root.to_path_buf());
}

/// `_operator_pre_commit_store`: PRE_COMMIT_HOME, then XDG_CACHE_HOME, then ~/.cache.
fn operator_store(env: &BTreeMap<String, String>, home: &Path) -> PathBuf {
    if let Some(pre_commit_home) = env.get("PRE_COMMIT_HOME").filter(|value| !value.is_empty()) {
        return expand_user(pre_commit_home, home);
    }
    if let Some(xdg) = env.get("XDG_CACHE_HOME").filter(|value| !value.is_empty()) {
        return expand_user(xdg, home).join("pre-commit");
    }
    home.join(".cache").join("pre-commit")
}

/// `_consume_pre_commit_store_spare`: rename a complete spare into the destination.
fn consume_spare(managed_root: &Path, destination: &Path) -> bool {
    match fs::rename(managed_root.join(SPARE_NAME), destination) {
        Ok(()) => true,
        Err(error) if error.kind() == io::ErrorKind::NotFound => false,
        Err(error) => {
            eprintln!("gdaemon: failed to consume pre-commit store spare: {error}");
            false
        }
    }
}

/// `_clone_pre_commit_store`: an APFS clone on macOS, else a symlink-preserving copy.
fn clone_store(source: &Path, destination: &Path) -> io::Result<()> {
    if !apfs_clone(source, destination) {
        remove_store(destination)?;
        copy_tree(source, destination)?;
    }
    make_owner_writable(destination)
}

fn apfs_clone(source: &Path, destination: &Path) -> bool {
    if !cfg!(target_os = "macos") {
        return false;
    }
    match Command::new("/bin/cp")
        .args(["-c", "-R"])
        .arg(source)
        .arg(destination)
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .output()
    {
        Ok(output) if output.status.success() => true,
        Ok(output) => {
            eprintln!(
                "gdaemon: APFS clone of pre-commit store failed, copying instead: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            );
            false
        }
        Err(error) => {
            eprintln!("gdaemon: APFS clone of pre-commit store failed, copying instead: {error}");
            false
        }
    }
}

/// `shutil.copytree(symlinks=True)`: copy a tree, keeping symlinks as symlinks.
fn copy_tree(source: &Path, destination: &Path) -> io::Result<()> {
    let metadata = fs::symlink_metadata(source)?;
    if metadata.file_type().is_symlink() {
        return symlink(fs::read_link(source)?, destination);
    }
    if metadata.is_dir() {
        fs::create_dir(destination)?;
        for entry in fs::read_dir(source)? {
            let entry = entry?;
            copy_tree(&entry.path(), &destination.join(entry.file_name()))?;
        }
        return fs::set_permissions(destination, metadata.permissions());
    }
    fs::copy(source, destination).map(drop)
}

/// `_make_owner_writable`: owner read/write (and search on directories), never following links.
fn make_owner_writable(root: &Path) -> io::Result<()> {
    let metadata = fs::symlink_metadata(root)?;
    if metadata.file_type().is_symlink() {
        return Ok(());
    }
    let mut mode = metadata.permissions().mode() | 0o600;
    if metadata.is_dir() {
        mode |= 0o100;
    }
    fs::set_permissions(root, Permissions::from_mode(mode & 0o7777))?;
    if metadata.is_dir() {
        for entry in fs::read_dir(root)? {
            make_owner_writable(&entry?.path())?;
        }
    }
    Ok(())
}

/// `_remove_pre_commit_store`: remove a store without following a replacement link.
fn remove_store(path: &Path) -> io::Result<()> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    if metadata.file_type().is_symlink() {
        return fs::remove_file(path);
    }
    match fs::remove_dir_all(path) {
        Err(error) if error.kind() == io::ErrorKind::PermissionDenied => {
            make_owner_writable(path)?;
            fs::remove_dir_all(path)
        }
        other => other,
    }
}

/// Create a directory tree with owner-only permissions.
fn create_private_dir(path: &Path) -> io::Result<()> {
    DirBuilder::new().recursive(true).mode(0o700).create(path)?;
    fs::set_permissions(path, Permissions::from_mode(0o700))
}

fn worker_slot() -> MutexGuard<'static, Option<JoinHandle<()>>> {
    match SPARE_WORKER.lock() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    }
}

/// `_schedule_pre_commit_store_spare`: start the sole replenisher when no spare exists.
fn schedule_spare(source: PathBuf, managed_root: PathBuf) {
    let spare = managed_root.join(SPARE_NAME);
    let mut worker = worker_slot();
    if spare.is_dir() || worker.as_ref().is_some_and(|handle| !handle.is_finished()) {
        return;
    }
    match thread::Builder::new()
        .name("pre-commit-store-spare".to_string())
        .spawn(move || replenish_spare(&source, &managed_root))
    {
        Ok(handle) => *worker = Some(handle),
        Err(error) => {
            eprintln!("gdaemon: failed to start pre-commit store spare replenisher: {error}")
        }
    }
}

/// `_replenish_pre_commit_store_spare`: clone the store aside, then rename it into place.
fn replenish_spare(source: &Path, managed_root: &Path) {
    let spare = managed_root.join(SPARE_NAME);
    let temporary = managed_root.join(SPARE_TEMP_NAME);
    if let Err(error) = try_replenish(source, managed_root, &spare, &temporary) {
        eprintln!("gdaemon: failed to replenish pre-commit store spare: {error}");
    }
    if let Err(error) = remove_store(&temporary) {
        eprintln!("gdaemon: failed to remove partial pre-commit store spare: {error}");
    }
}

fn try_replenish(
    source: &Path,
    managed_root: &Path,
    spare: &Path,
    temporary: &Path,
) -> io::Result<()> {
    create_private_dir(managed_root)?;
    remove_store(temporary)?;
    if spare.is_dir() {
        return Ok(());
    }
    clone_store(source, temporary)?;
    fs::rename(temporary, spare)
}
