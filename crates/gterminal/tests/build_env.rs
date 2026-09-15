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

fn zig_global_cache_dir() -> PathBuf {
    if let Some(dir) = std::env::var_os("ZIG_GLOBAL_CACHE_DIR") {
        return PathBuf::from(dir);
    }
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/"))
        .join(".cache/zig")
}

fn seed_temp_zig_global_cache(temp_global: &Path, source: &Path) -> Result<(), String> {
    let packages = source.join("p");
    if !packages.is_dir() {
        return Err(format!(
            "global Zig cache lacks package directory {}",
            packages.display()
        ));
    }
    fs::create_dir_all(temp_global).map_err(|err| err.to_string())?;
    let dest = temp_global.join("p");
    std::os::unix::fs::symlink(&packages, &dest).map_err(|err| {
        format!(
            "symlink {} -> {}: {err}",
            packages.display(),
            dest.display()
        )
    })?;
    Ok(())
}

fn zig_missing_package(stderr: &str) -> Option<String> {
    let lowered = stderr.to_ascii_lowercase();
    if !(lowered.contains("403")
        || lowered.contains("unable to fetch")
        || lowered.contains("unable to connect"))
    {
        return None;
    }
    stderr
        .lines()
        .map(str::trim)
        .find(|line| line.contains("http") || line.contains("error:") || line.contains("403"))
        .map(str::to_string)
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

#[test]
#[cfg(target_os = "macos")]
fn darwin_nonsimd_archive_links_every_member() {
    let temp = tempfile::tempdir().expect("tempdir");
    let vendor = workspace_root().join("crates/gterminal/vendor/libghostty-vt");
    let prefix = temp.path().join("install");
    let cache = temp.path().join("cache");
    let global_source = zig_global_cache_dir();
    let temp_global = temp.path().join("global-cache");
    if let Err(reason) = seed_temp_zig_global_cache(&temp_global, &global_source) {
        eprintln!("SKIP: darwin_nonsimd_archive_links_every_member: {reason}");
        return;
    }
    let uucode = temp_global
        .join("p")
        .join("uucode-0.2.0-ZZjBPqZVVABQepOqZHR7vV_NcaN-wats0IB6o-Exj6m9");
    if !uucode.exists() {
        eprintln!(
            "SKIP: darwin_nonsimd_archive_links_every_member: global Zig cache lacks package uucode-0.2.0-ZZjBPqZVVABQepOqZHR7vV_NcaN-wats0IB6o-Exj6m9"
        );
        return;
    }
    let version = fs::read_to_string(vendor.join("VERSION")).expect("vendor version");
    let output = Command::new(std::env::var_os("ZIG").unwrap_or_else(|| "zig".into()))
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
        .arg(&cache)
        .arg("--global-cache-dir")
        .arg(&temp_global)
        .env("ZIG_GLOBAL_CACHE_DIR", &temp_global)
        .output()
        .expect("build non-SIMD archive with Zig");
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        if let Some(missing) = zig_missing_package(&stderr) {
            eprintln!(
                "SKIP: darwin_nonsimd_archive_links_every_member: global Zig cache lacks package: {missing}"
            );
            return;
        }
        panic!("non-SIMD Zig build failed:\n{stderr}");
    }
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
