//! Cargo-level Zig gating for the gobby-terminal package (plan 1.2.8).

use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

// The macOS-only archive test is the one caller of `machine_zig`.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
#[path = "../build_zig.rs"]
mod build_zig;

use build_zig::resolve_zig;

fn workspace_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .expect("crates/gterminal -> workspace")
        .to_path_buf()
}

fn cargo_build(args: &[&str], extra_env: &[(&str, OsString)]) -> (i32, String) {
    let scratch = tempfile::Builder::new()
        .prefix("gterminal-cargo-")
        .tempdir()
        .expect("cargo scratch dir");
    let mut command = Command::new("cargo");
    command
        .args(args)
        .current_dir(workspace_root())
        .env("CARGO_TERM_COLOR", "never");
    for (key, value) in extra_env {
        command.env(key, value);
    }
    command.env("CARGO_TARGET_DIR", scratch.path().join("target"));
    let output = command.output().expect("spawn cargo");
    let mut text = String::new();
    text.push_str(&String::from_utf8_lossy(&output.stdout));
    text.push_str(&String::from_utf8_lossy(&output.stderr));
    let code = output.status.code().unwrap_or(1);
    (code, text)
}

#[test]
#[cfg(unix)]
fn build_env_uses_private_target_dir() {
    use std::os::unix::fs::PermissionsExt;

    let scratch = tempfile::tempdir().expect("tempdir");
    let bin_dir = scratch.path().join("bin");
    let cargo = bin_dir.join("cargo");
    let marker = scratch.path().join("cargo-target-dir");
    fs::create_dir(&bin_dir).expect("create fake cargo bin dir");
    fs::write(
        &cargo,
        "#!/bin/sh\nprintf '%s' \"$CARGO_TARGET_DIR\" > \"$GOBBY_CARGO_TARGET_MARKER\"\n",
    )
    .expect("write fake cargo");
    let mut permissions = fs::metadata(&cargo)
        .expect("fake cargo metadata")
        .permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(&cargo, permissions).expect("chmod fake cargo");

    let (code, text) = cargo_build(
        &["build"],
        &[
            ("PATH", bin_dir.into_os_string()),
            ("GOBBY_CARGO_TARGET_MARKER", marker.clone().into_os_string()),
        ],
    );
    assert_eq!(code, 0, "fake cargo must succeed:\n{text}");

    let target_dir = PathBuf::from(fs::read_to_string(marker).expect("read target marker"));
    assert_ne!(target_dir, workspace_root().join("target"));
    assert!(
        target_dir.starts_with(std::env::temp_dir()),
        "cargo target dir must be private scratch space: {}",
        target_dir.display()
    );
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
        text.contains("Zig 0.16"),
        "missing zig must name Zig 0.16:\n{text}"
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

#[test]
#[cfg(target_os = "macos")]
fn darwin_nonsimd_archive_links_every_member() {
    let temp = tempfile::tempdir().expect("tempdir");
    let vendor = workspace_root().join("crates/gterminal/vendor/libghostty-vt");
    let prefix = temp.path().join("install");
    let cache = temp.path().join("cache");
    let version = fs::read_to_string(vendor.join("VERSION")).expect("vendor version");
    let mut command =
        Command::new(build_zig::machine_zig().expect("Zig 0.16 for the vendored build"));
    command
        .current_dir(&vendor)
        .args([
            "build",
            "-Demit-lib-vt",
            "-Dsimd=false",
            "-Doptimize=ReleaseFast",
            "-Demit-xcframework=false",
        ])
        .arg(format!("-Dtarget={}-macos", std::env::consts::ARCH))
        .arg(format!("-Dversion-string={}", version.trim()))
        .arg("--prefix")
        .arg(&prefix)
        .arg("--cache-dir")
        .arg(&cache);
    if let Some(system_dir) = std::env::var_os("LIBGHOSTTY_VT_ZIG_SYSTEM_DIR") {
        command.arg("--system").arg(system_dir);
    }
    let output = command.output().expect("build non-SIMD archive with Zig");
    assert!(
        output.status.success(),
        "non-SIMD Zig build failed:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let archive = prefix.join("lib/libghostty-vt.a");
    let members = Command::new("ar")
        .arg("t")
        .arg(&archive)
        .output()
        .expect("list archive members");
    assert!(members.status.success());
    assert!(
        String::from_utf8_lossy(&members.stdout)
            .lines()
            .any(|name| name == "compiler_rt.o"),
        "archive normalization must retain the compiler runtime"
    );
    let output = Command::new("cc")
        .arg("-dynamiclib")
        .arg(format!("-Wl,-force_load,{}", archive.display()))
        .arg("-o")
        .arg(temp.path().join("archive-probe.dylib"))
        .output()
        .expect("force-load every archive member with Apple linker");
    assert!(
        output.status.success(),
        "non-SIMD archive must retain linkable members:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn version(text: &str) -> impl FnOnce() -> Option<String> + '_ {
    move || Some(text.to_string())
}

#[test]
fn zig_env_wins_without_probing_path() {
    let zig = resolve_zig(
        Some(OsString::from("/custom/zig")),
        || panic!("ZIG set: the PATH zig is never asked"),
        &[Path::new("/absent/zig")],
    );
    assert_eq!(zig, Ok(OsString::from("/custom/zig")));
}

#[test]
fn a_path_zig_016_is_used_before_the_homebrew_keg() {
    let keg = tempfile::NamedTempFile::new().expect("keg stand-in");
    let zig = resolve_zig(None, version("0.16.0\n"), &[keg.path()]);
    assert_eq!(zig, Ok(OsString::from("zig")));
}

#[test]
fn another_path_zig_falls_back_to_the_homebrew_keg() {
    let keg = tempfile::NamedTempFile::new().expect("keg stand-in");
    let zig = resolve_zig(None, version("0.17.0\n"), &[keg.path()]);
    assert_eq!(zig, Ok(keg.path().as_os_str().to_owned()));
    let zig = resolve_zig(None, || None, &[keg.path()]);
    assert_eq!(
        zig,
        Ok(keg.path().as_os_str().to_owned()),
        "a missing PATH zig falls back too"
    );
}

#[test]
fn an_intel_keg_serves_when_the_apple_silicon_keg_is_absent() {
    let dir = tempfile::tempdir().expect("tempdir");
    let apple_silicon = dir.path().join("opt-homebrew-zig");
    let intel = tempfile::NamedTempFile::new().expect("Intel keg stand-in");
    let zig = resolve_zig(
        None,
        version("0.17.0\n"),
        &[apple_silicon.as_path(), intel.path()],
    );
    assert_eq!(zig, Ok(intel.path().as_os_str().to_owned()));
    let first = tempfile::NamedTempFile::new().expect("Apple silicon keg stand-in");
    let zig = resolve_zig(None, version("0.17.0\n"), &[first.path(), intel.path()]);
    assert_eq!(
        zig,
        Ok(first.path().as_os_str().to_owned()),
        "with both kegs present the first wins"
    );
}

#[test]
fn no_zig_016_anywhere_names_the_brew_fix() {
    let dir = tempfile::tempdir().expect("tempdir");
    let apple_silicon = dir.path().join("opt-homebrew-zig");
    let intel = dir.path().join("usr-local-zig");
    let kegs = [apple_silicon.as_path(), intel.as_path()];
    let message = resolve_zig(None, version("0.17.0\n"), &kegs).expect_err("no Zig 0.16");
    assert!(message.contains("brew install zig@0.16"), "{message}");
    assert!(message.contains("0.17.0"), "names the PATH zig: {message}");
    for keg in kegs {
        let keg = keg.display().to_string();
        assert!(
            message.contains(&keg),
            "names every keg it tried: {message}"
        );
    }
    let message = resolve_zig(None, || None, &kegs).expect_err("no zig at all");
    assert!(message.contains("PATH zig is missing"), "{message}");
}
