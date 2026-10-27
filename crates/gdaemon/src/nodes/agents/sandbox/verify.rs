//! Managed SRT install verification (plan 2.5; Python `verify_srt_installation_locked`).
//!
//! The daemon verifies the pinned install that `gobby install` placed and never
//! installs it. Every check refuses with the Python lockout text, so a drifted
//! install stops every managed launch rather than running unverified bytes.

use super::runtime::{SRT_VERSION, SandboxError, SrtInstallation};
use crate::nodes::agents::sandbox::paths;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::{self, OpenOptions},
    io::{self, Read},
    os::unix::{
        fs::{OpenOptionsExt, PermissionsExt},
        io::AsRawFd,
    },
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

/// Upper bound on the `node --version` probe. The probe runs under the shared install lock.
const NODE_PROBE_TIMEOUT: Duration = Duration::from_secs(10);
const NODE_VERSION_OUTPUT_LIMIT: usize = 4096;

/// `SRT_RELEASE` in `src/gobby/utils/dependency_requirements.py`, the integrity pin.
pub(super) struct SrtRelease {
    pub package: &'static str,
    pub version: &'static str,
    pub tarball_url: &'static str,
    pub tarball_sha256: &'static str,
    pub npm_integrity: &'static str,
    pub lockfile_sha256: &'static str,
    pub runner_sha256: &'static str,
    pub http_proxy_sha256: &'static str,
    pub mux_proxy_sha256: &'static str,
    pub macos_profile_sha256: &'static str,
    pub which_sha256: &'static str,
}

pub(super) const SRT_RELEASE: SrtRelease = SrtRelease {
    package: "@anthropic-ai/sandbox-runtime",
    version: "0.0.79",
    tarball_url: "https://registry.npmjs.org/@anthropic-ai/sandbox-runtime/-/sandbox-runtime-0.0.79.tgz",
    tarball_sha256: "5a730e4367c264ccc4b592af01dab038a6c39db7c184efbd132841688fa854f1",
    npm_integrity: "sha512-WjBmS9fbTpnQwQkM9SU+JjhM2CcXo9eWEELktY1100RMvTy/jPhP44yZ3NXhh+GHB350OgPbM3+FubE5WVRtSA==",
    lockfile_sha256: "efd7a3220b4dcf538f4cca60cbdae040969a11047f144e7640fdbdff8fdeb175",
    runner_sha256: "44658a5b22bd801b08a60f63568dc6142e7aae038d2bc4f8b84a893e44b739c3",
    http_proxy_sha256: "5ee6a6e26a95f063ffd38e5222b001c0ad888aab597b92a3a3eabd903fe0cfb6",
    mux_proxy_sha256: "0a47d7841cd0c934734a2d65ad9094eec967481f59ac64381636a4a420b41755",
    macos_profile_sha256: "72155d55576e39c1a6dc6bd19e8c786adfc548a468567c665c2bff6e9164f716",
    which_sha256: "91c69caf6e828eccfcbd82e0c9bfb51cf33456725e1fb32ca43e5fff83d15b20",
};

/// `NODE_MIN_VERSION` in `dependency_requirements.py`.
const NODE_MIN_VERSION: (u64, u64, u64) = (22, 12, 0);

/// The bundled runner this daemon was built with; the install must match it too.
const BUNDLED_RUNNER: &[u8] = include_bytes!("../../../../../../src/gobby/agents/srt_runner.mjs");

const CONTENT_MANIFEST_NAME: &str = "content-manifest.json";

/// Patched upstream files the install must carry, keyed by path under the package root.
/// Verification reads only the pinned digests; the patch itself is applied by install.
pub(super) const PROXY_PATCHES: &[(&str, &str)] = &[
    (
        "node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/http-proxy.js",
        SRT_RELEASE.http_proxy_sha256,
    ),
    (
        "node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/mux-proxy.js",
        SRT_RELEASE.mux_proxy_sha256,
    ),
    (
        "node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/macos-sandbox-utils.js",
        SRT_RELEASE.macos_profile_sha256,
    ),
    (
        "node_modules/@anthropic-ai/sandbox-runtime/dist/utils/which.js",
        SRT_RELEASE.which_sha256,
    ),
];

/// `srt_install_root`: `<gobby_home>/tools/srt/<pinned version>`.
pub(super) fn install_root(gobby_home: &Path) -> PathBuf {
    gobby_home.join("tools").join("srt").join(SRT_VERSION)
}

/// `srt_install_lock`: the blocking install lock, `<tools/srt>/.<version>.lock`.
/// A read-only sandboxed reader still serializes through a read-only descriptor.
fn with_install_lock<T>(
    root: &Path,
    body: impl FnOnce() -> Result<T, SandboxError>,
) -> Result<T, SandboxError> {
    let parent = root
        .parent()
        .ok_or_else(|| lockout("managed SRT install root is invalid"))?;
    fs::create_dir_all(parent).map_err(|error| lockout(format!("managed SRT lock: {error}")))?;
    let name = root
        .file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .into_owned();
    let lock_path = parent.join(format!(".{name}.lock"));
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(&lock_path)
        .or_else(|_| OpenOptions::new().read(true).open(&lock_path))
        .map_err(|error| lockout(format!("managed SRT lock: {error}")))?;
    // SAFETY: `file` owns a valid descriptor for the whole call; flock only reads it.
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) } != 0 {
        return Err(lockout("managed SRT install lock could not be acquired"));
    }
    let result = body();
    // SAFETY: same descriptor; releasing the lock held above.
    unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_UN) };
    result
}

/// Verifies the managed install under `gobby_home` and returns the node that runs it.
pub fn verify_srt_installation(gobby_home: &Path) -> Result<SrtInstallation, SandboxError> {
    let root = install_root(gobby_home);
    with_install_lock(&root, || verify_locked(&root))
}

fn verify_locked(root: &Path) -> Result<SrtInstallation, SandboxError> {
    let version = SRT_RELEASE.version;
    let root = fs::canonicalize(root).unwrap_or_else(|_| root.to_path_buf());
    let runner = root.join("runner.mjs");
    let package_json = root.join("node_modules/@anthropic-ai/sandbox-runtime/package.json");
    let missing = || {
        lockout(format!(
            "managed SRT {version} is missing or invalid; rerun `gobby install`"
        ))
    };
    let receipt: Value = read_json(&root.join("receipt.json")).ok_or_else(missing)?;
    let package: Value = read_json(&package_json).ok_or_else(missing)?;
    let runner_bytes = fs::read(&runner).map_err(|_| missing())?;
    let lockfile_bytes = fs::read(root.join("package-lock.json")).map_err(|_| missing())?;
    let manifest_value: Value = read_json(&root.join(CONTENT_MANIFEST_NAME)).ok_or_else(|| {
        lockout("managed SRT content manifest is missing or invalid; rerun `gobby install`")
    })?;
    if !receipt.is_object() || !package.is_object() {
        return Err(lockout(format!(
            "managed SRT {version} receipt or package metadata is invalid"
        )));
    }
    let manifest = string_map(&manifest_value)
        .ok_or_else(|| lockout("managed SRT content manifest is invalid"))?;
    let expected_receipt = [
        ("package", SRT_RELEASE.package),
        ("version", SRT_RELEASE.version),
        ("tarball_url", SRT_RELEASE.tarball_url),
        ("tarball_sha256", SRT_RELEASE.tarball_sha256),
        ("npm_integrity", SRT_RELEASE.npm_integrity),
        ("lockfile_sha256", SRT_RELEASE.lockfile_sha256),
        ("runner_sha256", SRT_RELEASE.runner_sha256),
        ("http_proxy_sha256", SRT_RELEASE.http_proxy_sha256),
        ("mux_proxy_sha256", SRT_RELEASE.mux_proxy_sha256),
        ("macos_profile_sha256", SRT_RELEASE.macos_profile_sha256),
        ("which_sha256", SRT_RELEASE.which_sha256),
    ];
    if expected_receipt
        .iter()
        .any(|(key, value)| receipt.get(*key).and_then(Value::as_str) != Some(*value))
    {
        return Err(lockout(
            "managed SRT receipt does not match Gobby's pinned runtime",
        ));
    }
    if package.get("name").and_then(Value::as_str) != Some(SRT_RELEASE.package)
        || package.get("version").and_then(Value::as_str) != Some(SRT_RELEASE.version)
    {
        return Err(lockout(
            "managed SRT package identity does not match its receipt",
        ));
    }
    if sha256_hex(&runner_bytes) != SRT_RELEASE.runner_sha256 {
        return Err(lockout(
            "managed SRT runner checksum mismatch; run `gobby install srt`",
        ));
    }
    if sha256_hex(BUNDLED_RUNNER) != SRT_RELEASE.runner_sha256 {
        return Err(lockout(
            "managed SRT runner does not match this Gobby installation",
        ));
    }
    if sha256_hex(&lockfile_bytes) != SRT_RELEASE.lockfile_sha256 {
        return Err(lockout("managed SRT lockfile checksum mismatch"));
    }
    verify_proxy_patch_with(&root, PROXY_PATCHES)?;
    verify_srt_content(&root, &manifest)?;
    verify_protected_content(&root, &manifest)?;
    let node = verify_node_health()?;
    Ok(SrtInstallation {
        root: root.clone(),
        node,
        runner: fs::canonicalize(&runner).unwrap_or(runner),
    })
}

/// `verify_srt_proxy_patch`: every patched upstream file has its pinned digest.
pub(super) fn verify_proxy_patch_with(
    root: &Path,
    patches: &[(&str, &str)],
) -> Result<(), SandboxError> {
    for (relative, expected) in patches {
        let path = root.join(relative);
        let bytes = fs::read(&path).map_err(|error| {
            lockout(format!(
                "managed SRT {} is unreadable: {error}",
                file_name(&path)
            ))
        })?;
        if sha256_hex(&bytes) != *expected {
            return Err(lockout(format!(
                "managed SRT {} checksum mismatch; run `gobby install srt`",
                file_name(&path)
            )));
        }
    }
    Ok(())
}

/// `_verify_srt_content`'s manifest half: the install matches its content manifest.
pub fn verify_srt_content(
    root: &Path,
    manifest: &BTreeMap<String, String>,
) -> Result<(), SandboxError> {
    if &build_srt_content_manifest(root) != manifest {
        return Err(lockout(
            "managed SRT content manifest mismatch; rerun `gobby install`",
        ));
    }
    Ok(())
}

/// `_verify_srt_content`'s protection half: no manifest path is writable, and the
/// seccomp helper stays executable.
fn verify_protected_content(
    root: &Path,
    manifest: &BTreeMap<String, String>,
) -> Result<(), SandboxError> {
    let mut protected = vec![
        root.to_path_buf(),
        root.join("receipt.json"),
        root.join(CONTENT_MANIFEST_NAME),
    ];
    protected.extend(manifest.keys().map(|relative| root.join(relative)));
    protected.extend(walk(&root.join("node_modules")));
    for path in protected {
        let Ok(info) = fs::symlink_metadata(&path) else {
            return Err(lockout("managed SRT content manifest mismatch"));
        };
        if info.file_type().is_symlink() {
            continue;
        }
        let mode = info.permissions().mode();
        if mode & 0o222 != 0 {
            let relative = path.strip_prefix(root).unwrap_or(&path).display();
            return Err(lockout(format!(
                "managed SRT content is writable: {relative}"
            )));
        }
        if path.file_name().is_some_and(|name| name == "apply-seccomp") && mode & 0o111 == 0 {
            return Err(lockout(
                "managed SRT seccomp helper is not executable; rerun `gobby install`",
            ));
        }
    }
    Ok(())
}

/// `build_srt_content_manifest`: SHA-256 of every file under `runner.mjs`,
/// `package-lock.json` and `node_modules/`, or `link:<target>` for a symlink.
pub fn build_srt_content_manifest(root: &Path) -> BTreeMap<String, String> {
    let mut candidates = vec![root.join("runner.mjs"), root.join("package-lock.json")];
    candidates.extend(walk(&root.join("node_modules")));
    let mut manifest = BTreeMap::new();
    for path in candidates {
        let Ok(relative) = path
            .strip_prefix(root)
            .map(|rel| rel.to_string_lossy().into_owned())
        else {
            continue;
        };
        let Ok(info) = fs::symlink_metadata(&path) else {
            continue;
        };
        if info.file_type().is_symlink() {
            if let Ok(target) = fs::read_link(&path) {
                manifest.insert(relative, format!("link:{}", target.display()));
            }
        } else if info.is_file()
            && let Ok(bytes) = fs::read(&path)
        {
            manifest.insert(relative, sha256_hex(&bytes));
        }
    }
    manifest
}

/// Every entry under `dir`, without following symlinks; empty when `dir` is absent.
fn walk(dir: &Path) -> Vec<PathBuf> {
    let mut found = Vec::new();
    let Ok(entries) = fs::read_dir(dir) else {
        return found;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let is_dir = entry.file_type().is_ok_and(|kind| kind.is_dir());
        found.push(path.clone());
        if is_dir {
            found.extend(walk(&path));
        }
    }
    found
}

/// `node_dependency_status` plus the launch-side lookup: the node on `PATH` must
/// satisfy `NODE_MIN_VERSION`.
fn verify_node_health() -> Result<PathBuf, SandboxError> {
    let search = std::env::var("PATH").ok();
    let node = paths::which("node", search.as_deref())
        .ok_or_else(|| lockout("Node.js executable could not be found"))?;
    if !node.is_absolute() {
        return Err(lockout(
            "Node.js executable must have an absolute PATH spelling",
        ));
    }
    check_node_version(&node, NODE_PROBE_TIMEOUT)?;
    Ok(node)
}

/// Runs `node --version` with a deadline. A timed-out probe is killed and reaped; a
/// nonzero exit is refused whatever it printed.
fn check_node_version(node: &Path, timeout: Duration) -> Result<(), SandboxError> {
    let unverified = || lockout("Node.js version could not be verified");
    let mut child = Command::new(node)
        .arg("--version")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| unverified())?;
    let result = (|| {
        let mut pipe = child.stdout.take().ok_or_else(unverified)?;
        // SAFETY: pipe owns a live descriptor. F_GETFL has no third argument and F_SETFL
        // preserves its existing flags while making reads nonblocking.
        let flags = unsafe { libc::fcntl(pipe.as_raw_fd(), libc::F_GETFL) };
        if flags < 0
            || unsafe { libc::fcntl(pipe.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0
        {
            return Err(unverified());
        }
        let deadline = Instant::now() + timeout;
        let mut stdout = Vec::new();
        let mut status = None;
        let mut eof = false;
        loop {
            if status.is_none() {
                status = child.try_wait().map_err(|_| unverified())?;
            }
            if status.is_some_and(|status| !status.success()) {
                return Err(unverified());
            }
            let mut bytes = [0; 256];
            if !eof {
                match pipe.read(&mut bytes) {
                    Ok(0) => eof = true,
                    Ok(count) => {
                        stdout.extend_from_slice(&bytes[..count]);
                        if stdout.len() > NODE_VERSION_OUTPUT_LIMIT {
                            return Err(unverified());
                        }
                    }
                    Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
                    Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
                    Err(_) => return Err(unverified()),
                }
            }
            if status.is_some() && eof {
                return Ok(stdout);
            }
            if Instant::now() >= deadline {
                return Err(lockout("Node.js version check timed out"));
            }
            thread::sleep(Duration::from_millis(10));
        }
    })();
    if result.is_err() {
        let _ = child.kill();
        let _ = child.wait();
    }
    let stdout = result?;
    let text = String::from_utf8_lossy(&stdout);
    match parse_version(text.trim()) {
        Some(version) if version >= NODE_MIN_VERSION => Ok(()),
        _ => Err(lockout(format!(
            "Node.js {} or newer is required; install it and retry",
            format_version(NODE_MIN_VERSION)
        ))),
    }
}

/// `v22.12.0` or `22.12.0` to a comparable triple.
fn parse_version(text: &str) -> Option<(u64, u64, u64)> {
    let mut parts = text.trim_start_matches('v').split('.').map(|part| {
        part.chars()
            .take_while(char::is_ascii_digit)
            .collect::<String>()
            .parse::<u64>()
            .ok()
    });
    Some((parts.next()??, parts.next()??, parts.next()??))
}

fn format_version((major, minor, patch): (u64, u64, u64)) -> String {
    format!("{major}.{minor}.{patch}")
}

fn read_json(path: &Path) -> Option<Value> {
    serde_json::from_slice(&fs::read(path).ok()?).ok()
}

fn string_map(value: &Value) -> Option<BTreeMap<String, String>> {
    value
        .as_object()?
        .iter()
        .map(|(key, value)| Some((key.clone(), value.as_str()?.to_string())))
        .collect()
}

fn sha256_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn file_name(path: &Path) -> String {
    path.file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .into_owned()
}

fn lockout(message: impl Into<String>) -> SandboxError {
    SandboxError::new(message)
}

#[cfg(test)]
#[path = "verify/tests.rs"]
mod tests;
