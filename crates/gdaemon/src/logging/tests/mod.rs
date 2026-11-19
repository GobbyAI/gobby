//! Routed logging tests. Each test writes under its own temporary directory and installs
//! its subscriber for its own thread only, so nothing reaches the real logs.

use std::path::{Path, PathBuf};

use tracing::Dispatch;
use tracing_subscriber::Registry;
use tracing_subscriber::layer::SubscriberExt;

use super::*;

mod epoch;
mod routing;
mod settings;
mod writer;

/// The rotation unit of the size keys.
const MIB: usize = 1024 * 1024;

/// The files of the six primary surfaces.
const PRIMARY_FILES: [&str; 6] = [
    "daemon.log",
    "hooks.log",
    "llm.log",
    "mcp.log",
    "automation.log",
    "rule-allow-audit.jsonl",
];

/// Settings with every field spelled out, so a test never depends on the defaults.
fn settings_in(dir: &Path) -> LogSettings {
    LogSettings {
        level: Level::Info,
        format: Format::Text,
        dir: dir.to_path_buf(),
        max_size_mb: 10,
        backup_count: 5,
        llm_max_size_mb: 50,
        llm_backup_count: 5,
        allow_audit_max_size_mb: 256,
        allow_audit_backup_count: 9,
    }
}

/// A subscriber with only the routed layer, and the layer's handle.
fn routed(settings: LogSettings) -> (Dispatch, RouteHandle) {
    let layer = routed_layer(settings);
    let handle = layer.handle();
    (Dispatch::new(Registry::default().with(layer)), handle)
}

/// The text of `dir/name`, or empty when the file does not exist.
fn read(dir: &Path, name: &str) -> String {
    std::fs::read_to_string(dir.join(name)).unwrap_or_default()
}

fn exists(dir: &Path, name: &str) -> bool {
    dir.join(name).exists()
}

/// Every file under `root`, recursively.
fn files_under(root: &Path) -> Vec<PathBuf> {
    let mut found = Vec::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(dir) = pending.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                pending.push(path);
            } else {
                found.push(path);
            }
        }
    }
    found.sort();
    found
}

/// A message longer than one MiB, so one record moves a file past a 1 MB threshold.
fn large_message(tag: char) -> String {
    std::iter::repeat_n(tag, MIB + MIB / 8).collect()
}
