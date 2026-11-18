//! Section 8: each retained generation directory and the worker protocol version its manifest
//! records.

use std::io::ErrorKind;
use std::path::{Path, PathBuf};

use super::table;

const HEADING: &str = "## 8. Retained generations";
const DEFAULT_STORE: &str = ".gobby/bin/generations";

pub fn section(requested: Option<&Path>) -> String {
    let store = match requested {
        Some(path) => path.to_path_buf(),
        None => match std::env::var_os("HOME") {
            Some(home) => PathBuf::from(home).join(DEFAULT_STORE),
            None => {
                return format!(
                    "{HEADING}\n\nHOME is not set, so the default generation store is unknown.\n"
                );
            }
        },
    };
    let shown = store.display();
    let entries = match std::fs::read_dir(&store) {
        Ok(entries) => entries,
        Err(error) if error.kind() == ErrorKind::NotFound => {
            return format!(
                "{HEADING}\n\nStore `{shown}` does not exist, so no generations are recorded.\n"
            );
        }
        Err(error) => {
            return format!("{HEADING}\n\nStore `{shown}` cannot be read: {error}.\n");
        }
    };
    let mut dirs: Vec<PathBuf> = entries
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.is_dir())
        .collect();
    dirs.sort();
    let rows: Vec<Vec<String>> = dirs
        .iter()
        .map(|dir| {
            let name = dir
                .file_name()
                .map(|name| name.to_string_lossy().into_owned())
                .unwrap_or_default();
            vec![name, protocol_version(dir)]
        })
        .collect();
    let body = if rows.is_empty() {
        "No generation directory is in the store.\n".to_owned()
    } else {
        table(&["Generation", "Worker protocol version"], &rows)
    };
    format!("{HEADING}\n\nStore `{shown}`.\n\n{body}")
}

/// The `worker_protocol_version` in a generation's manifest, or why it is not recorded.
fn protocol_version(dir: &Path) -> String {
    let Ok(text) = std::fs::read_to_string(dir.join("manifest.json")) else {
        return "not recorded (no readable manifest.json)".to_owned();
    };
    let Ok(manifest) = serde_json::from_str::<serde_json::Value>(&text) else {
        return "not recorded (manifest.json is not JSON)".to_owned();
    };
    match manifest.get("worker_protocol_version") {
        Some(serde_json::Value::String(version)) => version.clone(),
        Some(serde_json::Value::Number(version)) => version.to_string(),
        _ => "not recorded".to_owned(),
    }
}
