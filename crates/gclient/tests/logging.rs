//! File-only logging at the gclient process boundary.

use std::process::Command;

#[test]
fn binary_initializes_logging_only_for_the_runtime_path() {
    for flag in ["--version", "--help"] {
        let home = tempfile::tempdir().expect("temporary home");
        let output = Command::new(env!("CARGO_BIN_EXE_gclient"))
            .arg(flag)
            .env("HOME", home.path())
            .output()
            .unwrap_or_else(|error| panic!("run gclient {flag}: {error}"));

        assert!(
            output.status.success(),
            "gclient {flag} failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(
            !home.path().join(".gobby/logs/gclient.log").exists(),
            "gclient {flag} must not create a runtime log"
        );
    }

    let home = tempfile::tempdir().expect("temporary home");
    let missing_token = home.path().join("missing-token");
    let output = Command::new(env!("CARGO_BIN_EXE_gclient"))
        .args([
            "--daemon-url",
            "http://127.0.0.1:1",
            "--token-file",
            missing_token.to_str().expect("UTF-8 token path"),
        ])
        .env("HOME", home.path())
        .output()
        .expect("run gclient runtime path");

    assert!(
        !output.status.success(),
        "missing token should stop startup before the terminal loop"
    );
    assert!(
        home.path().join(".gobby/logs/gclient.log").is_file(),
        "runtime entry point did not initialize file logging"
    );
    assert!(output.stdout.is_empty(), "logging leaked to stdout");
}
