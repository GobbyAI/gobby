//! The re-signed `ps` that every SRT launch puts first on PATH (port of
//! `droid_ps_shim.py`). Seatbelt refuses to exec the setuid `/bin/ps`, so a sandboxed
//! `ps` probe fails without a copy. The copy is re-signed ad hoc because AMFI kills an
//! Apple binary executed outside its trusted location.
//!
//! TODO(#22407): Droid's supervisor, the upstream defect that first needed this shim,
//! SIGKILLs its process group when its `ps` probe fails. Drop Droid's dependency once
//! Factory ships a fix. Reviewer seats keep the shim for stall diagnosis (#25069).
//! Upstream issue: https://github.com/Factory-AI/factory/issues/33

use super::wait_bounded;
use anyhow::{Context, Result, bail};
use sha2::{Digest, Sha256};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::Duration,
};

const SYSTEM_PS: &str = "/bin/ps";
const CODESIGN: &str = "/usr/bin/codesign";
const SHIM_DIRNAME: &str = "droid-ps-shim";
const CODESIGN_TIMEOUT: Duration = Duration::from_secs(30);

/// The directory holding the shim, or `None` when it is unavailable. A non-darwin host,
/// a missing `/bin/ps` or `codesign`, or a shim that fails to stage all return `None`:
/// the launch goes on without the shim and a sandboxed `ps` stays unavailable.
pub fn droid_shim_dir(gobby: &Path, platform: &str) -> Option<PathBuf> {
    if platform != "darwin" || !Path::new(SYSTEM_PS).exists() || !Path::new(CODESIGN).exists() {
        return None;
    }
    match ensure_shim(gobby) {
        Ok(directory) => Some(directory),
        Err(error) => {
            eprintln!("gdaemon: ps shim unavailable; sandboxed ps will fail closed: {error:#}");
            None
        }
    }
}

/// Keyed by the digest of `/bin/ps`, so an OS update lands in a new directory rather
/// than serving a stale copy.
fn ensure_shim(gobby: &Path) -> Result<PathBuf> {
    let source = fs::read(SYSTEM_PS).context("read /bin/ps")?;
    let digest = format!("{:x}", Sha256::digest(&source));
    let directory = gobby.join("runtime").join(SHIM_DIRNAME).join(digest);
    let target = directory.join("ps");
    if target.exists() {
        return Ok(directory);
    }
    fs::create_dir_all(&directory).context("create shim directory")?;
    let staged = directory.join(format!(".ps.{}.tmp", std::process::id()));
    let installed = stage(&staged, &source, &target);
    let _ = fs::remove_file(&staged);
    installed.map(|()| directory)
}

fn stage(staged: &Path, source: &[u8], target: &Path) -> Result<()> {
    fs::write(staged, source).context("copy /bin/ps")?;
    fs::set_permissions(staged, fs::Permissions::from_mode(0o755)).context("chmod shim")?;
    sign_adhoc(staged)?;
    fs::rename(staged, target).context("install shim")
}

/// `codesign -f -s -` with the Python oracle's 30-second bound.
fn sign_adhoc(path: &Path) -> Result<()> {
    let mut child = Command::new(CODESIGN)
        .args(["-f", "-s", "-"])
        .arg(path)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .context("start codesign")?;
    match wait_bounded(&mut child, CODESIGN_TIMEOUT).context("wait for codesign")? {
        Some(status) if status.success() => Ok(()),
        Some(status) => bail!("codesign exited with {status}"),
        None => bail!("codesign timed out after {CODESIGN_TIMEOUT:?}"),
    }
}
