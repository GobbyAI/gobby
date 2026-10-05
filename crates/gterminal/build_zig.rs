//! Which Zig builds the vendored libghostty-vt. `build.rs` and
//! `tests/build_env.rs` include this file.

use std::env;
use std::ffi::OsString;
use std::path::Path;
use std::process::Command;

/// Homebrew's keg-only Zig 0.16, for a machine whose PATH zig moved on: the
/// Apple silicon prefix, then the Intel one.
const HOMEBREW_ZIG_016: [&str; 2] = [
    "/opt/homebrew/opt/zig@0.16/bin/zig",
    "/usr/local/opt/zig@0.16/bin/zig",
];

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
