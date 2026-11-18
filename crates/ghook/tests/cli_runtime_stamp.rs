//! `ghook --version` and `ghook --runtime-stamp` contracts.
//!
//! The runtime stamp is the daemon's compatibility record for the installed
//! `$GOBBY_HOME/bin/ghook`. Only that installed file may write it: a staged
//! candidate or a build artifact that the installer probes must leave it alone.
//! Every test points `GOBBY_HOME` and `HOME` at temporary directories.

use std::collections::BTreeSet;
use std::error::Error;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::{Value, json};

type TestResult = Result<(), Box<dyn Error>>;

const STAMP_FILE: &str = ".ghook-runtime.json";
const SENTINEL: &[u8] = b"{\"sentinel\": true}";

fn build_artifact() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_ghook"))
}

fn version_stdout() -> Vec<u8> {
    format!("ghook {}\n", env!("CARGO_PKG_VERSION")).into_bytes()
}

fn run(exe: &Path, gobby_home: &Path, home: &Path, args: &[&str]) -> std::io::Result<Output> {
    Command::new(exe)
        .args(args)
        .env("HOME", home)
        .env("GOBBY_HOME", gobby_home)
        .output()
}

/// Copy the built binary to `<gobby_home>/bin/<name>` and return that path.
fn place_copy(gobby_home: &Path, name: &str) -> std::io::Result<PathBuf> {
    let bin = gobby_home.join("bin");
    fs::create_dir_all(&bin)?;
    let dest = bin.join(name);
    fs::copy(build_artifact(), &dest)?;
    Ok(dest)
}

fn stamp_path(gobby_home: &Path) -> PathBuf {
    gobby_home.join("bin").join(STAMP_FILE)
}

fn assert_success(output: &Output, expected_stdout: &[u8]) {
    assert!(
        output.status.success(),
        "ghook exited unsuccessfully\nstderr:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        output.stdout,
        expected_stdout,
        "stdout:\n{}",
        String::from_utf8_lossy(&output.stdout)
    );
    assert!(
        output.stderr.is_empty(),
        "stderr should be empty:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn assert_stamp_document(bytes: &[u8]) -> TestResult {
    let stamp: Value = serde_json::from_slice(bytes)?;
    let keys = stamp
        .as_object()
        .ok_or("stamp must be a JSON object")?
        .keys()
        .map(String::as_str)
        .collect::<BTreeSet<_>>();
    assert_eq!(
        keys,
        BTreeSet::from(["ghook_version", "response_capability", "schema_version"])
    );
    assert_eq!(stamp["schema_version"], json!(1));
    assert_eq!(stamp["ghook_version"], json!(env!("CARGO_PKG_VERSION")));
    assert_eq!(stamp["response_capability"], json!("hook-response.v1"));
    Ok(())
}

#[test]
fn build_artifact_version_prints_version_and_writes_no_stamp() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;

    let output = run(
        &build_artifact(),
        gobby_home.path(),
        home.path(),
        &["--version"],
    )?;

    assert_success(&output, &version_stdout());
    assert!(
        !gobby_home.path().join("bin").exists(),
        "build-artifact --version must not create $GOBBY_HOME/bin"
    );
    Ok(())
}

#[test]
fn build_artifact_version_leaves_existing_stamp_alone() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    place_copy(gobby_home.path(), "ghook")?;
    fs::write(stamp_path(gobby_home.path()), SENTINEL)?;

    let output = run(
        &build_artifact(),
        gobby_home.path(),
        home.path(),
        &["--version"],
    )?;

    assert_success(&output, &version_stdout());
    assert_eq!(fs::read(stamp_path(gobby_home.path()))?, SENTINEL);
    Ok(())
}

#[test]
fn installed_copy_version_writes_runtime_stamp() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let installed = place_copy(gobby_home.path(), "ghook")?;

    let output = run(&installed, gobby_home.path(), home.path(), &["--version"])?;

    assert_success(&output, &version_stdout());
    assert_stamp_document(&fs::read(stamp_path(gobby_home.path()))?)?;
    Ok(())
}

#[test]
fn staged_candidate_version_writes_no_stamp() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    place_copy(gobby_home.path(), "ghook")?;
    let staged = place_copy(gobby_home.path(), ".ghook.staged")?;
    fs::write(stamp_path(gobby_home.path()), SENTINEL)?;

    let output = run(&staged, gobby_home.path(), home.path(), &["--version"])?;

    assert_success(&output, &version_stdout());
    assert_eq!(fs::read(stamp_path(gobby_home.path()))?, SENTINEL);
    Ok(())
}

#[cfg(unix)]
#[test]
fn symlink_in_bin_to_build_artifact_is_the_installed_binary() -> TestResult {
    // Canonical-path rule: the link and the running build artifact resolve to
    // the same file, so that file is the installed binary and writes the stamp.
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let bin = gobby_home.path().join("bin");
    fs::create_dir_all(&bin)?;
    let link = bin.join("ghook");
    std::os::unix::fs::symlink(build_artifact(), &link)?;

    let via_link = run(&link, gobby_home.path(), home.path(), &["--version"])?;
    assert_success(&via_link, &version_stdout());
    assert_stamp_document(&fs::read(stamp_path(gobby_home.path()))?)?;

    fs::remove_file(stamp_path(gobby_home.path()))?;
    let direct = run(
        &build_artifact(),
        gobby_home.path(),
        home.path(),
        &["--version"],
    )?;
    assert_success(&direct, &version_stdout());
    assert_stamp_document(&fs::read(stamp_path(gobby_home.path()))?)?;
    Ok(())
}

#[test]
fn runtime_stamp_flag_prints_stamp_bytes_and_writes_nothing() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let installed = place_copy(gobby_home.path(), "ghook")?;
    let version = run(&installed, gobby_home.path(), home.path(), &["--version"])?;
    assert_success(&version, &version_stdout());

    let written = fs::read(stamp_path(gobby_home.path()))?;
    fs::remove_file(stamp_path(gobby_home.path()))?;
    let mut expected = written.clone();
    expected.push(b'\n');

    let output = run(
        &installed,
        gobby_home.path(),
        home.path(),
        &["--runtime-stamp"],
    )?;
    assert_success(&output, &expected);
    assert!(
        !stamp_path(gobby_home.path()).exists(),
        "--runtime-stamp must not write the stamp"
    );

    let fresh = tempfile::tempdir()?;
    let from_build = run(
        &build_artifact(),
        fresh.path(),
        home.path(),
        &["--runtime-stamp"],
    )?;
    assert_success(&from_build, &expected);
    assert!(
        !fresh.path().join("bin").exists(),
        "--runtime-stamp must not create $GOBBY_HOME/bin"
    );
    Ok(())
}
