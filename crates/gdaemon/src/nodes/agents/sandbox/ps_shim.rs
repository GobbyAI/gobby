//! The `ps` that every SRT launch puts first on PATH (port of `droid_ps_shim.py`). Seatbelt
//! refuses to exec the setuid `/bin/ps`, so a sandboxed `ps` probe fails without a copy. The
//! copy is re-signed ad hoc because AMFI kills an Apple binary executed outside its trusted
//! location. The `ps` the PATH resolves is a wrapper: it checks argv against the
//! non-argument allow-list (#25368) and runs the re-signed copy at `bin/ps`, off PATH.
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
/// The argv-checking `ps`. Its allow-list is mirrored in `droid_ps_shim.py` (parity-tested).
const WRAPPER: &str = include_str!("ps_shim_wrapper.sh");
const REAL_PS_DIR: &str = "bin";
const REAL_PS: &str = "ps";

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

/// Keyed by the digest of `/bin/ps` and the wrapper, so an OS update or a wrapper change
/// lands in a new directory rather than serving a stale copy.
fn ensure_shim(gobby: &Path) -> Result<PathBuf> {
    let source = fs::read(SYSTEM_PS).context("read /bin/ps")?;
    let mut hasher = Sha256::new();
    hasher.update(WRAPPER.as_bytes());
    hasher.update(&source);
    let digest = format!("{:x}", hasher.finalize());
    let directory = gobby.join("runtime").join(SHIM_DIRNAME).join(digest);
    let target = directory.join("ps");
    if target.exists() {
        return Ok(directory);
    }
    let real_dir = directory.join(REAL_PS_DIR);
    fs::create_dir_all(&real_dir).context("create shim directory")?;
    let staged = real_dir.join(format!(".{REAL_PS}.{}.tmp", std::process::id()));
    let installed = stage_signed(&staged, &source, &real_dir.join(REAL_PS));
    let _ = fs::remove_file(&staged);
    installed?;
    // The wrapper lands last: its presence means bin/ps is in place.
    let staged = directory.join(format!(".ps.{}.tmp", std::process::id()));
    let installed = stage_script(&staged, &target);
    let _ = fs::remove_file(&staged);
    installed.map(|()| directory)
}

/// The re-signed copy of `/bin/ps`, installed at `bin/ps`, off PATH.
fn stage_signed(staged: &Path, source: &[u8], target: &Path) -> Result<()> {
    fs::write(staged, source).context("copy /bin/ps")?;
    fs::set_permissions(staged, fs::Permissions::from_mode(0o755)).context("chmod shim")?;
    sign_adhoc(staged)?;
    fs::rename(staged, target).context("install shim")
}

/// The argv-checking `ps` wrapper. It runs `bin/ps` only when every argument is allowed.
fn stage_script(staged: &Path, target: &Path) -> Result<()> {
    fs::write(staged, WRAPPER).context("write shim wrapper")?;
    fs::set_permissions(staged, fs::Permissions::from_mode(0o755)).context("chmod shim wrapper")?;
    fs::rename(staged, target).context("install shim wrapper")
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
