use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use crate::sources::SourceRecord;
use crate::{ScopeIdentity, WikiError};

/// Log action names written by production call sites.
pub const ACTION_SOURCE_INGESTED: &str = "source_ingested";
pub const ACTION_PAGE_CREATED: &str = "page_created";
pub const ACTION_PAGE_UPDATED: &str = "page_updated";
pub const ACTION_UPKEEP_COMPLETED: &str = "upkeep_completed";
pub const ACTION_RECAP_COMPLETED: &str = "recap_completed";
pub const ACTION_LIFECYCLE_TRANSITION: &str = "lifecycle_transition";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LogEntry {
    pub timestamp: String,
    pub scope: ScopeIdentity,
    pub action: String,
    pub summary: String,
    pub artifacts: Vec<PathBuf>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub struct LogWriteReport {
    pub scope_log: PathBuf,
    pub global_log: Option<PathBuf>,
}

/// Entry for a source accepted into the manifest. `raw_path` is the
/// vault-relative raw ingested asset path from [`crate::ingest::IngestResult`].
pub fn source_ingested_entry(
    timestamp: &str,
    scope: &ScopeIdentity,
    record: &SourceRecord,
    raw_path: &Path,
) -> LogEntry {
    LogEntry {
        timestamp: timestamp.to_string(),
        scope: scope.clone(),
        action: ACTION_SOURCE_INGESTED.to_string(),
        summary: format!("{} {}", record.id, record.location),
        artifacts: vec![raw_path.to_path_buf()],
    }
}

/// Append one `source_ingested` line per accepted ingest to the vault log.
pub fn append_sources_ingested<'a>(
    vault_root: &Path,
    scope: &ScopeIdentity,
    timestamp: &str,
    results: impl IntoIterator<Item = &'a crate::ingest::IngestResult>,
) -> Result<(), WikiError> {
    let entries = results
        .into_iter()
        .map(|result| source_ingested_entry(timestamp, scope, &result.record, &result.raw_path))
        .collect::<Vec<_>>();
    append_log_entries(vault_root, None, &entries)?;
    Ok(())
}

pub fn append_logs(
    scope_root: &Path,
    global_hub_root: Option<&Path>,
    entry: &LogEntry,
) -> Result<LogWriteReport, WikiError> {
    let scope_log = scope_root.join("log.md");
    append_log(&scope_log, entry)?;

    // Scope log is the source of truth for command chronology; mirror to the
    // global log only after the scope append succeeds.
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

fn append_log_entries(
    scope_root: &Path,
    global_hub_root: Option<&Path>,
    entries: &[LogEntry],
) -> Result<Option<LogWriteReport>, WikiError> {
    if entries.is_empty() {
        return Ok(None);
    }
    let scope_log = scope_root.join("log.md");
    append_log_entries_to_file(&scope_log, entries)?;
    let global_log = global_hub_root
        .map(|root| root.join("log.md"))
        .map(|path| {
            if !same_log_path(&scope_log, &path) {
                append_log_entries_to_file(&path, entries)?;
            }
            Ok::<PathBuf, WikiError>(path)
        })
        .transpose()?;

    let report = LogWriteReport {
        scope_log,
        global_log,
    };
    Ok(Some(report))
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
fn append_log(path: &Path, entry: &LogEntry) -> Result<(), WikiError> {
    append_log_entries_to_file(path, std::slice::from_ref(entry))
}

fn append_log_entries_to_file(path: &Path, entries: &[LogEntry]) -> Result<(), WikiError> {
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

    let write_header = file
        .metadata()
        .map(|metadata| metadata.len() == 0)
        .unwrap_or(false);
    if write_header {
        file.write_all(b"# Log\n\n")
            .map_err(|error| WikiError::Io {
                action: "write log",
                path: Some(path.to_path_buf()),
                source: error,
            })?;
    }

    let rendered = entries.iter().map(render_entry).collect::<String>();
    file.write_all(rendered.as_bytes())
        .map_err(|error| WikiError::Io {
            action: "write log",
            path: Some(path.to_path_buf()),
            source: error,
        })
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
// One entry per line so the log stays greppable (`grep page_created log.md`).
fn render_entry(entry: &LogEntry) -> String {
    let mut rendered = format!(
        "- {} [{}] {}: {}",
        single_line(&entry.timestamp),
        entry.scope,
        single_line(&entry.action),
        single_line(&entry.summary),
    );
    if !entry.artifacts.is_empty() {
        let artifacts = entry
            .artifacts
            .iter()
            .map(|artifact| artifact.display().to_string())
            .collect::<Vec<_>>()
            .join(", ");
        rendered.push_str(" (artifacts: ");
        rendered.push_str(&single_line(&artifacts));
        rendered.push(')');
    }
    rendered.push('\n');
    rendered
}

fn single_line(value: &str) -> String {
    value.split_whitespace().collect::<Vec<_>>().join(" ")
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
fn same_log_path(left: &Path, right: &Path) -> bool {
    // Compare after resolving existing parents; append_logs relies on this
    // before writing so scope/global aliases do not receive duplicate entries.
    let resolved_left = resolved_log_path(left);
    let resolved_right = resolved_log_path(right);
    resolved_left == resolved_right || same_file_identity(&resolved_left, &resolved_right)
}

#[cfg(unix)]
#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
fn same_file_identity(left: &Path, right: &Path) -> bool {
    use std::os::unix::fs::MetadataExt;

    let (Ok(left), Ok(right)) = (std::fs::metadata(left), std::fs::metadata(right)) else {
        return false;
    };
    left.dev() == right.dev() && left.ino() == right.ino()
}

#[cfg(windows)]
#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
fn same_file_identity(left: &Path, right: &Path) -> bool {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Foundation::HANDLE;
    use windows_sys::Win32::Storage::FileSystem::{
        BY_HANDLE_FILE_INFORMATION, GetFileInformationByHandle,
    };

    fn identity(path: &Path) -> Option<(u32, u32, u32)> {
        // The std MetadataExt accessors for volume serial + file index are still
        // unstable (windows_by_handle), so read them off the open file handle
        // via GetFileInformationByHandle to stay on stable Rust.
        let file = std::fs::File::open(path).ok()?;
        let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
        // SAFETY: `file` owns a valid handle for the duration of the call, and
        // `info` is a properly aligned, writable output buffer.
        let ok = unsafe { GetFileInformationByHandle(file.as_raw_handle() as HANDLE, &mut info) };
        if ok == 0 {
            return None;
        }
        if info.nFileIndexHigh == 0 && info.nFileIndexLow == 0 {
            return None;
        }
        Some((
            info.dwVolumeSerialNumber,
            info.nFileIndexHigh,
            info.nFileIndexLow,
        ))
    }

    match (identity(left), identity(right)) {
        (Some(left), Some(right)) => left == right,
        _ => false,
    }
}

#[cfg(not(any(unix, windows)))]
fn same_file_identity(_left: &Path, _right: &Path) -> bool {
    false
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
fn resolved_log_path(path: &Path) -> PathBuf {
    if let Ok(resolved) = path.canonicalize() {
        return resolved;
    }

    resolve_log_path_fallback(path)
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
fn resolve_log_path_fallback(path: &Path) -> PathBuf {
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
        assert_eq!(
            scope_log,
            "# Log\n\n- 2026-05-29T19:00:00Z [topic:rust] query: Answered ownership question \
             (artifacts: outputs/query-ownership.md)\n"
        );

        let global_log = fs::read_to_string(hub_root.join("log.md")).expect("global log");
        assert_eq!(global_log, scope_log);
    }

    #[test]
    fn renders_entries_as_single_greppable_lines() {
        let entry = LogEntry {
            timestamp: "unix-ms:1751500000000".to_string(),
            scope: ScopeIdentity::project("/repo"),
            action: ACTION_PAGE_CREATED.to_string(),
            summary: "Gcode\nmulti  line summary".to_string(),
            artifacts: vec!["knowledge/topics/gcode.md".into()],
        };

        let rendered = render_entry(&entry);

        assert_eq!(
            rendered,
            "- unix-ms:1751500000000 [project:/repo] page_created: Gcode multi line summary \
             (artifacts: knowledge/topics/gcode.md)\n"
        );
        assert_eq!(rendered.matches('\n').count(), 1);
    }

    #[test]
    fn source_ingested_entry_carries_record_identity_and_raw_path() {
        let record = crate::sources::SourceManifest::register(
            tempfile::tempdir().expect("tempdir").path(),
            crate::sources::SourceDraft::new(
                "https://example.com/post",
                crate::sources::SourceKind::Url,
                "unix-ms:1751500000000",
                b"body".to_vec(),
            ),
        )
        .expect("record registered");

        let entry = source_ingested_entry(
            "unix-ms:1751500000001",
            &ScopeIdentity::topic("research"),
            &record,
            Path::new("raw/some-source.md"),
        );

        assert_eq!(entry.action, ACTION_SOURCE_INGESTED);
        assert!(entry.summary.contains(&record.id));
        assert!(entry.summary.contains("https://example.com/post"));
        assert_eq!(entry.artifacts, vec![PathBuf::from("raw/some-source.md")]);
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
