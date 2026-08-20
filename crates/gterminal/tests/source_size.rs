//! Hand-maintained keep-set sources stay under the 1,000-line ceiling.

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
            let entry = entry.expect("src dirent");
            let path = entry.path();
            if path.is_dir() {
                stack.push(path);
            } else if path.extension().is_some_and(|ext| ext == "rs") {
                let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
                // Sidecar unit tests and bindgen chunks are not production sources.
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
    assert!(src.is_dir(), "missing crates/gterminal/src");
    assert!(
        src.join("pane/terminal.rs").is_file(),
        "keep-set pane/terminal.rs is missing"
    );

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

    let original_modules = [
        src.join("pane/terminal.rs"),
        src.join("raw_input.rs"),
        src.join("protocol/wire.rs"),
        src.join("protocol/render_ansi.rs"),
    ];
    for path in original_modules {
        let text = fs::read_to_string(&path).unwrap_or_default();
        assert!(
            text.contains("pub ") || text.contains("pub("),
            "public API must remain on {}",
            path.display()
        );
    }
    assert!(src.join("pane/terminal_render.rs").is_file());
    assert!(src.join("pane/terminal_io.rs").is_file());
    assert!(src.join("raw_input_framer.rs").is_file());
    assert!(src.join("protocol/wire_types.rs").is_file());
    assert!(src.join("protocol/wire_codec.rs").is_file());
    assert!(src.join("protocol/render_ansi_blit.rs").is_file());
}
