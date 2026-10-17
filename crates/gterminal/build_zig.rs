//! Which Zig builds the vendored libghostty-vt. `build.rs` and
//! `tests/build_env.rs` include this file.

use std::env;
use std::ffi::OsString;
use std::fs;
use std::io;
use std::path::Path;
use std::process::Command;

/// Homebrew's keg-only Zig 0.16, for a machine whose PATH zig moved on: the
/// Apple silicon prefix, then the Intel one.
const HOMEBREW_ZIG_016: [&str; 2] = [
    "/opt/homebrew/opt/zig@0.16/bin/zig",
    "/usr/local/opt/zig@0.16/bin/zig",
];

/// Stages `vendored` as a Zig build root at `root`. Zig writes `zig-pkg`
/// beside `build.zig`, so the manifests are copied in and every other entry
/// is linked; the vendored tree is only read. Zig's own outputs there are
/// never staged, and restaging keeps the ones under `root`.
pub fn stage_build_root(vendored: &Path, root: &Path) -> io::Result<()> {
    fs::create_dir_all(root)?;
    for entry in fs::read_dir(vendored)? {
        let entry = entry?;
        let name = entry.file_name();
        let staged = root.join(&name);
        if name == "build.zig" || name == "build.zig.zon" {
            fs::copy(entry.path(), &staged)?;
        } else if !ZIG_OUTPUTS.iter().any(|output| name == *output) {
            relink(&entry.path(), &staged)?;
        }
    }
    Ok(())
}

/// Points `staged` at `original`, replacing any earlier link.
#[cfg(unix)]
fn relink(original: &Path, staged: &Path) -> io::Result<()> {
    unless_missing(fs::remove_file(staged))?;
    std::os::unix::fs::symlink(original, staged)
}

/// Points `staged` at `original`, replacing any earlier link. Windows links
/// a directory and a file with different calls, and removes them that way.
#[cfg(windows)]
fn relink(original: &Path, staged: &Path) -> io::Result<()> {
    use std::os::windows::fs::{symlink_dir, symlink_file};
    if original.is_dir() {
        unless_missing(fs::remove_dir(staged))?;
        symlink_dir(original, staged)
    } else {
        unless_missing(fs::remove_file(staged))?;
        symlink_file(original, staged)
    }
}

fn unless_missing(result: io::Result<()>) -> io::Result<()> {
    match result {
        Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(()),
        other => other,
    }
}

/// What Zig writes under its build root; never staged from the vendored tree.
const ZIG_OUTPUTS: [&str; 3] = [".zig-cache", "zig-out", "zig-pkg"];

/// This machine's Zig for libghostty-vt, per `resolve_zig`.
pub fn machine_zig() -> Result<OsString, String> {
    resolve_zig(
        env::var_os("ZIG"),
        path_zig_version,
        &HOMEBREW_ZIG_016.map(Path::new),
    )
}

/// `ZIG` when set; else the PATH `zig` when it reports 0.16.x; else the
/// first of `homebrew_kegs` that exists; else an error naming the fix.
/// `path_zig_version` runs only when `ZIG` is unset.
pub fn resolve_zig(
    env_zig: Option<OsString>,
    path_zig_version: impl FnOnce() -> Option<String>,
    homebrew_kegs: &[&Path],
) -> Result<OsString, String> {
    if let Some(zig) = env_zig {
        return Ok(zig);
    }
    let version = path_zig_version();
    let version = version.as_deref().map(str::trim);
    if version.is_some_and(|version| version.starts_with("0.16.")) {
        return Ok("zig".into());
    }
    if let Some(keg) = homebrew_kegs.iter().find(|keg| keg.exists()) {
        return Ok(keg.into());
    }
    let kegs: Vec<String> = homebrew_kegs
        .iter()
        .map(|keg| keg.display().to_string())
        .collect();
    Err(format!(
        "Zig 0.16 is required, but the PATH zig is {} and no keg exists at {}. \
         Run `brew install zig@0.16`, or set ZIG to a Zig 0.16 binary.",
        version.unwrap_or("missing"),
        kegs.join(" or ")
    ))
}

/// What the PATH `zig` reports as its version, if it runs.
fn path_zig_version() -> Option<String> {
    let output = Command::new("zig").arg("version").output().ok()?;
    output
        .status
        .success()
        .then(|| String::from_utf8_lossy(&output.stdout).into_owned())
}
