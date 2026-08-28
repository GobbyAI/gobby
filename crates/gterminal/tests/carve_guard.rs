//! Source-scan guard: the keep-set must not mention herdr agent/plugin/persist
//! concepts, and the named grapheme-mode unit test must live on pane/terminal.

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
                files.push(path);
            }
        }
    }
    files.sort();
    files
}

const REQUIRED: &[&str] = &[
    "lib.rs",
    "ghostty/bindings.rs",
    "pane/terminal.rs",
    "pane/cursor.rs",
    "pane/input.rs",
    "pane/kitty_keyboard.rs",
    "pane/xtgettcap.rs",
    "pane/state.rs",
    "pane/osc.rs",
    "pty/mod.rs",
    "pty/actor.rs",
    "input/mod.rs",
    "raw_input.rs",
    "protocol/wire.rs",
    "protocol/render_ansi.rs",
    "ipc.rs",
    "platform/mod.rs",
    "layout.rs",
    "selection.rs",
    "terminal_theme.rs",
    "terminal_modes.rs",
    "runtime/mod.rs",
    "runtime/runtime.rs",
];

const FORBIDDEN: &[&str] = &[
    "agent_detection",
    "crate::detect",
    "crate::integration",
    "crate::persist",
    "crate::plugin",
    "plugin_command",
    "plugin_paths",
    "HERDR_AGENT",
    "spawn_basic_detection_task",
    "full_lifecycle_authority",
    "begin_graceful_release",
    "detection_content_seq",
    "detect_reset_notify",
    "detect_handle",
    "fn detection_text(",
    "fn agent_osc_title",
    "fn agent_osc_progress",
    "reset_agent_detection",
    "set_full_lifecycle_authority_active",
    "pub fn foreground_job",
];

#[test]
fn no_agent_concepts_in_terminal_core() {
    let src = src_root();
    assert!(src.is_dir(), "missing crates/gterminal/src");

    let mut missing = Vec::new();
    for relative in REQUIRED {
        if !src.join(relative).is_file() {
            missing.push(*relative);
        }
    }
    assert!(
        missing.is_empty(),
        "keep-set files missing under src/: {missing:?}"
    );

    let mut hits = Vec::new();
    for path in rust_sources(&src) {
        let text = fs::read_to_string(&path).expect("read rust source");
        for needle in FORBIDDEN {
            if text.contains(needle) {
                hits.push(format!("{}: {needle}", path.display()));
            }
        }
        for (index, line) in text.lines().enumerate() {
            let lower = line.to_ascii_lowercase();
            if lower.contains("herdr") {
                hits.push(format!(
                    "{}:{}: leftover herdr token",
                    path.display(),
                    index + 1
                ));
            }
        }
    }
    assert!(
        hits.is_empty(),
        "forbidden keep-set concepts:\n{}",
        hits.join("\n")
    );

    let wire = fs::read_to_string(src.join("protocol/wire.rs")).expect("wire.rs");
    assert!(
        wire.contains("pub const PROTOCOL_VERSION: u32 = 1;"),
        "protocol::wire::PROTOCOL_VERSION must be 1"
    );
    assert!(
        !src.join("pane/agent_detection.rs").exists(),
        "pane/agent_detection.rs must not be imported"
    );

    let terminal = fs::read_to_string(src.join("pane/terminal.rs")).expect("pane/terminal.rs");
    assert!(
        terminal.contains("fn grapheme_cluster_mode_is_default_and_survives_full_reset"),
        "named grapheme-mode test must live in pane/terminal.rs"
    );

    let pane = fs::read_to_string(src.join("pane.rs")).expect("pane.rs");
    assert!(
        pane.contains("struct PaneRuntime") || pane.contains("pub use self::runtime::PaneRuntime"),
        "PaneRuntime must live on the pane module"
    );
    let runtime = fs::read_to_string(src.join("runtime/runtime.rs")).expect("runtime.rs");
    for required_api in [
        "fn spawn(",
        "fn frame_data(",
        "fn dirty_patch(",
        "fn osc_title(",
        "fn osc_progress(",
        "fn resize(",
        "fn encode_terminal_key(",
    ] {
        assert!(
            runtime.contains(required_api),
            "runtime API missing {required_api}"
        );
    }
    assert!(
        runtime.contains("fn shutdown(") || runtime.contains("fn kill("),
        "runtime API must expose shutdown/kill"
    );
}
