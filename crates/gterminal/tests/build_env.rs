//! Cargo-level Zig gating for the gobby-terminal package (plan 1.2.8).

use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

fn workspace_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .expect("crates/gterminal -> workspace")
        .to_path_buf()
}

fn cargo_build(args: &[&str], extra_env: &[(&str, OsString)]) -> (i32, String) {
    let mut command = Command::new("cargo");
    command
        .args(args)
        .current_dir(workspace_root())
        .env("CARGO_TERM_COLOR", "never");
    for (key, value) in extra_env {
        command.env(key, value);
    }
    let output = command.output().expect("spawn cargo");
    let mut text = String::new();
    text.push_str(&String::from_utf8_lossy(&output.stdout));
    text.push_str(&String::from_utf8_lossy(&output.stderr));
    let code = output.status.code().unwrap_or(1);
    (code, text)
}

#[test]
fn missing_zig_reports_requirement() {
    let temp = tempfile::tempdir().expect("tempdir");
    let missing_zig = temp.path().join("missing-zig");
    let (code, text) = cargo_build(
        &["build", "-p", "gobby-terminal", "--features", "vt-engine"],
        &[("ZIG", missing_zig.into_os_string())],
    );
    assert_ne!(code, 0, "vt-engine build must fail without zig:\n{text}");
    assert!(
        text.contains("Zig 0.15") || text.contains("0.15"),
        "missing zig must name Zig 0.15:\n{text}"
    );
}

#[test]
fn default_features_build_invokes_no_zig() {
    let temp = tempfile::tempdir().expect("tempdir");
    let probe = temp.path().join("zig-probe");
    let marker = temp.path().join("zig-invoked");
    fs::write(
        &probe,
        format!(
            "#!/bin/sh\necho invoked > '{}'\nexit 42\n",
            marker.display()
        ),
    )
    .expect("write zig probe");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut permissions = fs::metadata(&probe).expect("probe metadata").permissions();
        permissions.set_mode(0o755);
        fs::set_permissions(&probe, permissions).expect("chmod probe");
    }

    let (code, text) = cargo_build(
        &["build", "-p", "gobby-terminal"],
        &[("ZIG", probe.into_os_string())],
    );
    assert_eq!(
        code, 0,
        "default-features build must succeed without zig:\n{text}"
    );
    assert!(!marker.exists(), "default-features build invoked Zig");
}
