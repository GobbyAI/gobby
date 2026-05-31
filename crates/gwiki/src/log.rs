use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use crate::{ScopeIdentity, WikiError};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LogEntry {
    pub timestamp: String,
    pub scope: ScopeIdentity,
    pub action: String,
    pub summary: String,
    pub artifacts: Vec<PathBuf>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LogWriteReport {
    pub scope_log: PathBuf,
    pub global_log: Option<PathBuf>,
}

pub fn append_logs(
    scope_root: &Path,
    global_hub_root: Option<&Path>,
    entry: &LogEntry,
) -> Result<LogWriteReport, WikiError> {
    let scope_log = scope_root.join("log.md");
    append_log(&scope_log, entry)?;

    let global_log = global_hub_root
        .map(|root| root.join("log.md"))
        .map(|path| {
            if !same_log_path(&scope_log, &path) {
                append_log(&path, entry)?;
            }
            Ok::<PathBuf, WikiError>(path)
        })
        .transpose()?;

    Ok(LogWriteReport {
        scope_log,
        global_log,
    })
}

fn append_log(path: &Path, entry: &LogEntry) -> Result<(), WikiError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create log directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }

    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|error| WikiError::Io {
            action: "open log",
            path: Some(path.to_path_buf()),
            source: error,
        })?;

    let write_header = file.metadata().map_or(true, |metadata| metadata.len() == 0);
    if write_header {
        file.write_all(b"# Log\n\n")
            .map_err(|error| WikiError::Io {
                action: "write log",
                path: Some(path.to_path_buf()),
                source: error,
            })?;
    }

    file.write_all(render_entry(entry).as_bytes())
        .map_err(|error| WikiError::Io {
            action: "write log",
            path: Some(path.to_path_buf()),
            source: error,
        })
}

fn render_entry(entry: &LogEntry) -> String {
    let mut rendered = format!(
        "## {} - {}\nScope: {}\n\n{}\n",
        entry.timestamp, entry.action, entry.scope, entry.summary
    );
    if !entry.artifacts.is_empty() {
        rendered.push_str("\nArtifacts:\n");
        for artifact in &entry.artifacts {
            rendered.push_str("- ");
            rendered.push_str(&artifact.display().to_string());
            rendered.push('\n');
        }
    }
    rendered.push('\n');
    rendered
}

fn same_log_path(left: &Path, right: &Path) -> bool {
    // Compare after resolving existing parents; append_logs relies on this
    // before writing so scope/global aliases do not receive duplicate entries.
    resolved_log_path(left) == resolved_log_path(right)
}

fn resolved_log_path(path: &Path) -> PathBuf {
    if let Ok(path) = path.canonicalize() {
        return path;
    }
    if let (Some(parent), Some(file_name)) = (path.parent(), path.file_name())
        && let Ok(parent) = parent.canonicalize()
    {
        return parent.join(file_name);
    }
    path.to_path_buf()
}

#[cfg(test)]
mod tests {
    use std::fs;

    use super::*;

    #[test]
    fn writes_scope_and_global_logs() {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope_root = temp.path().join("scope");
        let hub_root = temp.path().join("hub");
        fs::create_dir_all(&scope_root).expect("scope root");
        fs::create_dir_all(&hub_root).expect("hub root");

        let entry = LogEntry {
            timestamp: "2026-05-29T19:00:00Z".to_string(),
            scope: ScopeIdentity::topic("rust"),
            action: "query".to_string(),
            summary: "Answered ownership question".to_string(),
            artifacts: vec!["outputs/query-ownership.md".into()],
        };

        let report = append_logs(&scope_root, Some(&hub_root), &entry).expect("logs are appended");

        assert_eq!(report.scope_log, scope_root.join("log.md"));
        assert_eq!(report.global_log, Some(hub_root.join("log.md")));

        let scope_log = fs::read_to_string(scope_root.join("log.md")).expect("scope log");
        assert!(scope_log.contains("## 2026-05-29T19:00:00Z - query"));
        assert!(scope_log.contains("Scope: topic:rust"));
        assert!(scope_log.contains("Answered ownership question"));
        assert!(scope_log.contains("outputs/query-ownership.md"));

        let global_log = fs::read_to_string(hub_root.join("log.md")).expect("global log");
        assert_eq!(global_log, scope_log);
    }

    #[test]
    fn does_not_append_twice_when_scope_and_global_logs_match() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path().join("hub");
        fs::create_dir_all(&root).expect("hub root");

        let entry = LogEntry {
            timestamp: "2026-05-29T19:00:00Z".to_string(),
            scope: ScopeIdentity::topic("rust"),
            action: "query".to_string(),
            summary: "Answered ownership question".to_string(),
            artifacts: vec![],
        };

        let report = append_logs(&root, Some(&root), &entry).expect("logs are appended once");

        assert_eq!(report.scope_log, root.join("log.md"));
        assert_eq!(report.global_log, Some(root.join("log.md")));
        let log = fs::read_to_string(root.join("log.md")).expect("log written");
        assert_eq!(log.matches("Answered ownership question").count(), 1);
    }
}
