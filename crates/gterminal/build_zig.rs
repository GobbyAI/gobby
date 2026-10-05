//! Which Zig builds the vendored libghostty-vt. `build.rs` and
//! `tests/build_env.rs` include this file.

use std::env;
use std::ffi::OsString;
use std::path::Path;
use std::process::Command;

/// Homebrew's keg-only Zig 0.16, for a machine whose PATH zig moved on.
const HOMEBREW_ZIG_016: &str = "/opt/homebrew/opt/zig@0.16/bin/zig";

/// This machine's Zig for libghostty-vt, per `resolve_zig`.
pub fn machine_zig() -> Result<OsString, String> {
    resolve_zig(
        env::var_os("ZIG"),
        path_zig_version,
        Path::new(HOMEBREW_ZIG_016),
    )
}

/// `ZIG` when set; else the PATH `zig` when it reports 0.16.x; else
/// `homebrew_zig` when it exists; else an error naming the fix.
/// `path_zig_version` runs only when `ZIG` is unset.
pub fn resolve_zig(
    env_zig: Option<OsString>,
    path_zig_version: impl FnOnce() -> Option<String>,
    homebrew_zig: &Path,
) -> Result<OsString, String> {
    if let Some(zig) = env_zig {
        return Ok(zig);
    }
    let version = path_zig_version();
    let version = version.as_deref().map(str::trim);
    if version.is_some_and(|version| version.starts_with("0.16.")) {
        return Ok("zig".into());
    }
    if homebrew_zig.exists() {
        return Ok(homebrew_zig.into());
    }
    Err(format!(
        "Zig 0.16 is required, but the PATH zig is {} and {} does not exist. \
         Run `brew install zig@0.16`, or set ZIG to a Zig 0.16 binary.",
        version.unwrap_or("missing"),
        homebrew_zig.display()
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
