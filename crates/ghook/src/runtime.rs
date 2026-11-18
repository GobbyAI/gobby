use crate::{diagnose, envelope, transport};
use anyhow::Result;
use std::fs;

/// The runtime stamp document. `ghook --version` writes it to
/// `bin/.ghook-runtime.json`, and `ghook --runtime-stamp` prints it, so both
/// come from this one builder. Keys are alphabetical in the source so the bytes
/// match whether or not serde_json keeps insertion order.
pub(crate) fn stamp_bytes() -> Result<Vec<u8>> {
    let stamp = serde_json::json!({
        "ghook_version": diagnose::GHOOK_VERSION,
        "response_capability": envelope::RESPONSE_CAPABILITY,
        "schema_version": envelope::SCHEMA_VERSION,
    });
    Ok(serde_json::to_vec_pretty(&stamp)?)
}

/// Whether the running executable is the installed `$GOBBY_HOME/bin/ghook`.
///
/// Both sides are canonicalized, so a symlink in the bin dir counts as the
/// installed binary when it resolves to the running file. Any resolution
/// failure answers false: the stamp is written only when identity is proven.
fn is_installed_executable() -> bool {
    let Ok(running) = std::env::current_exe().and_then(fs::canonicalize) else {
        return false;
    };
    let Ok(home) = gobby_core::gobby_home() else {
        return false;
    };
    let installed = home
        .join("bin")
        .join(format!("ghook{}", std::env::consts::EXE_SUFFIX));
    fs::canonicalize(installed).is_ok_and(|installed| installed == running)
}

/// Write the stamp only when the running executable is the installed binary.
/// Any other copy (a staged candidate, a build artifact, a temp copy) writes
/// nothing, and the check runs before the bin dir is touched.
pub(crate) fn write_runtime_stamp_if_installed() -> Result<()> {
    if !is_installed_executable() {
        return Ok(());
    }
    let bin_dir = gobby_core::gobby_home()?.join("bin");
    fs::create_dir_all(&bin_dir)?;
    transport::atomic_write(&bin_dir.join(".ghook-runtime.json"), &stamp_bytes()?)?;
    Ok(())
}
