//! The vt-engine build script links Cargo-owned copies of libghostty-vt, so the
//! build-script output cached in a checkout target never names its source tree.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

#[test]
fn link_artifacts_live_under_the_cargo_target_dir() {
    let scratch = tempfile::Builder::new()
        .prefix("gterminal-link-")
        .tempdir()
        .expect("cargo scratch dir");
    let target_dir = scratch.path().join("target");
    let workspace = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .expect("crates/gterminal -> workspace");
    let output = Command::new("cargo")
        .args([
            "build",
            "-p",
            "gobby-terminal",
            "--features",
            "vt-engine",
            "--message-format=json",
        ])
        .current_dir(workspace)
        .env("CARGO_TARGET_DIR", &target_dir)
        .env("CARGO_TERM_COLOR", "never")
        .output()
        .expect("spawn cargo");
    assert!(
        output.status.success(),
        "vt-engine build failed:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );

    let package_suffix = format!("#{}@{}", env!("CARGO_PKG_NAME"), env!("CARGO_PKG_VERSION"));
    let script_outputs: Vec<PathBuf> = serde_json::Deserializer::from_slice(&output.stdout)
        .into_iter::<serde_json::Value>()
        .filter_map(|message| {
            let message = message.expect("Cargo JSON message");
            if message["reason"] != "build-script-executed"
                || !message["package_id"]
                    .as_str()
                    .is_some_and(|id| id.ends_with(&package_suffix))
            {
                return None;
            }
            let out_dir = Path::new(message["out_dir"].as_str().expect("build-script out_dir"));
            Some(
                out_dir
                    .parent()
                    .expect("build-script directory")
                    .to_path_buf(),
            )
        })
        // Cargo 1.100 moved the captured directives from output to run/stdout.
        .flat_map(|dir| [dir.join("run/stdout"), dir.join("output")])
        .filter(|path| path.is_file())
        .collect();
    assert_eq!(
        script_outputs.len(),
        1,
        "expected one gobby-terminal build-script output: {script_outputs:?}"
    );
    let directives = fs::read_to_string(&script_outputs[0]).expect("read build-script output");

    let target_root = fs::canonicalize(&target_dir).expect("canonical target dir");
    let under_target = |raw: &str| {
        let path = fs::canonicalize(raw)
            .unwrap_or_else(|err| panic!("link path {raw} does not resolve: {err}"));
        assert!(
            path.starts_with(&target_root),
            "link path {raw} is outside the target dir {}",
            target_root.display()
        );
        path
    };
    let mut search_dirs = Vec::new();
    for line in directives.lines() {
        if let Some(value) = line.strip_prefix("cargo:rustc-link-search=") {
            let raw = value.split_once('=').map_or(value, |(_, path)| path);
            search_dirs.push(under_target(raw));
        } else if let Some(raw) = line.strip_prefix("cargo:rustc-link-arg=") {
            assert!(under_target(raw).is_file(), "link arg {raw} is not a file");
        }
    }

    let archive = if cfg!(target_env = "msvc") {
        "ghostty-vt-static.lib"
    } else {
        "libghostty-vt.a"
    };
    assert!(
        search_dirs.iter().any(|dir| dir.join(archive).is_file()),
        "{archive} missing from link search dirs {search_dirs:?}:\n{directives}"
    );
}
