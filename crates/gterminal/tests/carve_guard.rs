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

fn relative_src_path(path: &Path) -> String {
    path.strip_prefix(src_root())
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/")
}

fn is_generated_binding(relative: &str) -> bool {
    Path::new(relative)
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.starts_with("generated_"))
}

fn is_ghostty_path(relative: &str) -> bool {
    relative == "ghostty" || relative.starts_with("ghostty/")
}

/// First line of `attr` must be a `#[allow` / `#![allow` / `cfg_attr(..., allow` form.
fn allow_has_reason(attr: &str, previous_line: &str) -> bool {
    if previous_line.trim_start().starts_with("// reason:") {
        return true;
    }
    attr.lines().any(|line| {
        line.split("//")
            .nth(1)
            .is_some_and(|comment| comment.trim_start().starts_with("reason:"))
    })
}

fn inner_lints(inner: &str) -> Vec<String> {
    inner
        .split(',')
        .map(|part| {
            part.split("//")
                .next()
                .unwrap_or(part)
                .trim()
                .trim_end_matches(')')
                .trim()
                .to_string()
        })
        .filter(|lint| !lint.is_empty())
        .collect()
}

struct AllowAttr {
    start_line: usize,
    text: String,
    inner: String,
}

fn find_allow_attrs(source: &str) -> Vec<AllowAttr> {
    let bytes = source.as_bytes();
    let mut attrs = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] != b'#' {
            i += 1;
            continue;
        }
        let hash = i;
        let mut j = i + 1;
        if j < bytes.len() && bytes[j] == b'!' {
            j += 1;
        }
        if j >= bytes.len() || bytes[j] != b'[' {
            i += 1;
            continue;
        }
        j += 1;
        while j < bytes.len() && bytes[j].is_ascii_whitespace() {
            j += 1;
        }
        let rest = &source[j..];
        let (kind, after_kind) = if rest.starts_with("allow") {
            ("allow", j + 5)
        } else if rest.starts_with("cfg_attr") {
            ("cfg_attr", j + 8)
        } else {
            i += 1;
            continue;
        };
        let mut k = after_kind;
        while k < bytes.len() && bytes[k].is_ascii_whitespace() {
            k += 1;
        }
        if k >= bytes.len() || bytes[k] != b'(' {
            i += 1;
            continue;
        }
        let Some(end) = close_bracket_after_attr(bytes, hash) else {
            i += 1;
            continue;
        };
        let text = &source[hash..=end];
        let Some(inner) = allow_inner(text, kind) else {
            i = end + 1;
            continue;
        };
        let start_line = source[..hash].bytes().filter(|b| *b == b'\n').count() + 1;
        attrs.push(AllowAttr {
            start_line,
            text: text.to_string(),
            inner,
        });
        i = end + 1;
    }
    attrs
}

fn close_bracket_after_attr(bytes: &[u8], hash: usize) -> Option<usize> {
    let mut depth = 0usize;
    let mut in_string = false;
    let mut i = hash;
    while i < bytes.len() {
        let b = bytes[i];
        if in_string {
            if b == b'\\' {
                i += 2;
                continue;
            }
            if b == b'"' {
                in_string = false;
            }
            i += 1;
            continue;
        }
        match b {
            b'"' => in_string = true,
            b'[' => depth += 1,
            b']' => {
                depth = depth.saturating_sub(1);
                if depth == 0 {
                    return Some(i);
                }
            }
            _ => {}
        }
        i += 1;
    }
    None
}

fn allow_inner(attr: &str, kind: &str) -> Option<String> {
    match kind {
        "allow" => {
            let start = attr.find("allow")?;
            let open = attr[start..].find('(')? + start;
            balanced_parens(&attr[open..]).map(|(inner, _)| inner)
        }
        "cfg_attr" => {
            let start = attr.find("cfg_attr")?;
            let open = attr[start..].find('(')? + start;
            let (cfg_inner, _) = balanced_parens(&attr[open..])?;
            let allow_at = cfg_inner.find("allow")?;
            let allow_open = cfg_inner[allow_at..].find('(')? + allow_at;
            balanced_parens(&cfg_inner[allow_open..]).map(|(inner, _)| inner)
        }
        _ => None,
    }
}

fn balanced_parens(src: &str) -> Option<(String, usize)> {
    let bytes = src.as_bytes();
    if bytes.first() != Some(&b'(') {
        return None;
    }
    let mut depth = 0usize;
    let mut in_string = false;
    for (i, &b) in bytes.iter().enumerate() {
        if in_string {
            if b == b'\\' {
                continue;
            }
            if b == b'"' {
                in_string = false;
            }
            continue;
        }
        match b {
            b'"' => in_string = true,
            b'(' => depth += 1,
            b')' => {
                depth -= 1;
                if depth == 0 {
                    return Some((src[1..i].to_string(), i));
                }
            }
            _ => {}
        }
    }
    None
}

fn previous_source_line(source: &str, start_line: usize) -> String {
    source
        .lines()
        .nth(start_line.saturating_sub(2))
        .unwrap_or("")
        .to_string()
}

#[test]
fn no_blanket_lint_allows() {
    let src = src_root();
    assert!(src.is_dir(), "missing crates/gterminal/src");

    let mut violations = Vec::new();
    for path in rust_sources(&src) {
        let relative = relative_src_path(&path);
        if is_generated_binding(&relative) {
            continue;
        }
        let text = fs::read_to_string(&path).expect("read rust source");
        let in_ghostty = is_ghostty_path(&relative);
        for attr in find_allow_attrs(&text) {
            let location = format!("{relative}:{}", attr.start_line);
            if !allow_has_reason(&attr.text, &previous_source_line(&text, attr.start_line)) {
                violations.push(format!("{location}: allow without `// reason:` line"));
            }
            let lints = inner_lints(&attr.inner);
            if lints.iter().any(|lint| lint == "clippy::all") && !in_ghostty {
                violations.push(format!("{location}: clippy::all allow outside ghostty/"));
            }
            if lints.iter().any(|lint| lint == "dead_code") && !in_ghostty {
                violations.push(format!("{location}: dead_code allow outside ghostty/"));
            }
        }
    }

    assert!(
        violations.is_empty(),
        "blanket or undocumented lint allows:\n{}",
        violations.join("\n")
    );
}
