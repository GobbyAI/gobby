//! 3.3.6 / 3.3.7 / 3.3.10 / 3.3.16 crate shape and source-size ceiling.

use std::fs;
use std::path::{Path, PathBuf};

fn src_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src")
}

fn rust_sources(dir: &Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    let mut stack = vec![dir.to_path_buf()];
    while let Some(current) = stack.pop() {
        for entry in fs::read_dir(&current).expect("read src dir") {
            let path = entry.expect("src dirent").path();
            if path.is_dir() {
                stack.push(path);
            } else if path.extension().is_some_and(|ext| ext == "rs") {
                let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
                if name == "tests.rs"
                    || name.starts_with("tests_")
                    || name.starts_with("generated_")
                {
                    continue;
                }
                files.push(path);
            }
        }
    }
    files.sort();
    files
}

#[test]
fn no_src_file_at_or_above_1000_lines() {
    let src = src_root();
    assert!(src.is_dir(), "missing crates/gclient/src");
    let mut oversized = Vec::new();
    for path in rust_sources(&src) {
        let text = fs::read_to_string(&path).expect("read rust source");
        let lines = text.lines().count();
        if lines >= 1000 {
            oversized.push(format!("{}: {lines}", path.display()));
        }
    }
    assert!(
        oversized.is_empty(),
        "src files at or above 1000 lines:\n{}",
        oversized.join("\n")
    );
}

#[test]
fn license_notice_workspace_and_frame_source() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let notice = fs::read_to_string(root.join("NOTICE.md")).unwrap();
    assert!(notice.contains("## Upstream"));
    assert!(notice.contains("## Modifications"));
    assert!(notice.contains("herdr"));
    let cargo = fs::read_to_string(root.join("Cargo.toml")).unwrap();
    assert!(cargo.contains("license = \"Apache-2.0\""));
    assert!(root.join("LICENSE").is_file());
    let upstream = fs::read_to_string(root.join("UPSTREAM.md")).unwrap();
    assert!(upstream.contains("sidebar.rs"));
    assert!(upstream.contains("reject") || upstream.contains("drop"));

    let workspace = fs::read_to_string(root.join("../../Cargo.toml")).unwrap();
    assert!(workspace.contains("crates/gclient"));
    let pins = fs::read_to_string(root.join("../../src/gobby/install/version_pins.py")).unwrap();
    assert!(pins.contains("\"gclient\""));

    let frame = fs::read_to_string(src_root().join("frame_source.rs")).unwrap();
    assert!(frame.contains("trait FrameSource"));
    let mut dials = Vec::new();
    for path in rust_sources(&src_root()) {
        if path.ends_with("frame_source.rs") {
            continue;
        }
        let text = fs::read_to_string(&path).unwrap();
        if text.contains("UnixStream::connect") || text.contains("UnixStream::connect_addr") {
            dials.push(path.display().to_string());
        }
    }
    assert!(
        dials.is_empty(),
        "socket dials outside frame_source: {dials:?}"
    );
}
