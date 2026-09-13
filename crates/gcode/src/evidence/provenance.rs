//! Recorded Git provenance; never constructs a source checkout or index.
use super::contracts::{ChangeStatus, ChangedPath, CommitBinding, ComparisonKind, ExclusionReason};
use super::source::{canonical_hash, validate_repo_path};
use super::{EvidenceError, Result};
use std::io::{Read as _, Write as _};
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::Duration;
use wait_timeout::ChildExt;

pub(super) fn read_patch(
    repo_root: &Path,
    parent_oid: &str,
    commit_oid: &str,
    path: &ChangedPath,
) -> Result<String> {
    let mut args = vec![
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--no-renames",
        "--diff-algorithm=myers",
        "--no-indent-heuristic",
        "--unified=3",
        parent_oid,
        commit_oid,
        "--",
    ];
    if path.old_exclusion.is_none()
        && let Some(old) = path.old_path.as_deref()
    {
        args.push(old);
    }
    if path.new_exclusion.is_none()
        && let Some(new) = path.new_path.as_deref()
    {
        args.push(new);
    }
    if args.last() == Some(&"--") {
        return Ok(String::new());
    }
    git_text(repo_root, &args)
}

pub(super) fn load_commit_binding(repo_root: &Path, commit_oid: &str) -> Result<CommitBinding> {
    let parents = git_text(repo_root, &["show", "-s", "--format=%P", commit_oid])?;
    let parent_oids = parents
        .split_ascii_whitespace()
        .map(str::to_string)
        .collect::<Vec<_>>();
    let (comparison_parent_oid, comparison_kind) = match parent_oids.first() {
        Some(parent) => (parent.clone(), ComparisonKind::FirstParent),
        None => (
            git_text(repo_root, &["hash-object", "-t", "tree", "--stdin"])?,
            ComparisonKind::EmptyTree,
        ),
    };
    let mut changed_paths = load_changed_paths(repo_root, &comparison_parent_oid, commit_oid)?;
    changed_paths.sort();
    let changed_paths_digest = canonical_hash(&changed_paths)?;
    Ok(CommitBinding {
        parent_oids,
        comparison_parent_oid,
        comparison_kind,
        changed_paths_digest,
        changed_paths,
    })
}

fn load_changed_paths(
    repo_root: &Path,
    base_oid: &str,
    commit_oid: &str,
) -> Result<Vec<ChangedPath>> {
    let bytes = git(
        repo_root,
        &[
            "diff-tree",
            "--raw",
            "-r",
            "-z",
            "--no-abbrev",
            "--no-commit-id",
            "--no-ext-diff",
            "--no-textconv",
            "--find-renames",
            base_oid,
            commit_oid,
        ],
        None,
    )?;
    let fields = bytes.split(|byte| *byte == 0).collect::<Vec<_>>();
    let mut index = 0;
    let mut records = Vec::new();
    while index < fields.len() && !fields[index].is_empty() {
        let header = std::str::from_utf8(fields[index]).map_err(|error| EvidenceError::Git {
            operation: "parse diff-tree".to_string(),
            message: error.to_string(),
        })?;
        index += 1;
        let header = header.strip_prefix(':').ok_or_else(|| EvidenceError::Git {
            operation: "parse diff-tree".to_string(),
            message: format!("invalid raw header {header:?}"),
        })?;
        let parts = header.split_ascii_whitespace().collect::<Vec<_>>();
        if parts.len() != 5 || index >= fields.len() {
            return Err(EvidenceError::Git {
                operation: "parse diff-tree".to_string(),
                message: format!("invalid raw header {header:?}"),
            });
        }
        let status_token = parts[4];
        let status_code = status_token.chars().next().unwrap_or(' ');
        let status = match status_code {
            'A' => ChangeStatus::Added,
            'C' => ChangeStatus::Copied,
            'D' => ChangeStatus::Deleted,
            'M' => ChangeStatus::Modified,
            'R' => ChangeStatus::Renamed,
            'T' => ChangeStatus::TypeChanged,
            _ => {
                return Err(EvidenceError::Git {
                    operation: "parse diff-tree".to_string(),
                    message: format!("unsupported change status {status_token:?}"),
                });
            }
        };
        let (first_path, first_exclusion) = canonical_diff_path(fields[index]);
        index += 1;
        let (old_path, new_path, old_exclusion, new_exclusion) = match status {
            ChangeStatus::Added => (None, Some(first_path), None, first_exclusion),
            ChangeStatus::Deleted => (Some(first_path), None, first_exclusion, None),
            ChangeStatus::Renamed | ChangeStatus::Copied => {
                if index >= fields.len() {
                    return Err(EvidenceError::Git {
                        operation: "parse diff-tree".to_string(),
                        message: "rename/copy record lacks destination".to_string(),
                    });
                }
                let (second_path, second_exclusion) = canonical_diff_path(fields[index]);
                index += 1;
                (
                    Some(first_path),
                    Some(second_path),
                    first_exclusion,
                    second_exclusion,
                )
            }
            ChangeStatus::Modified | ChangeStatus::TypeChanged => (
                Some(first_path.clone()),
                Some(first_path),
                first_exclusion,
                first_exclusion,
            ),
        };
        records.push(ChangedPath {
            status,
            similarity: status_token.get(1..).and_then(|value| value.parse().ok()),
            old_path,
            new_path,
            old_exclusion,
            new_exclusion,
            old_mode: nonzero_mode(parts[0]),
            new_mode: nonzero_mode(parts[1]),
            old_blob_oid: blob_oid(parts[0], parts[2]),
            new_blob_oid: blob_oid(parts[1], parts[3]),
        });
    }
    Ok(records)
}

fn nonzero_mode(mode: &str) -> Option<String> {
    (mode != "000000").then(|| mode.to_string())
}

fn blob_oid(mode: &str, oid: &str) -> Option<String> {
    (mode.starts_with("100") || mode == "120000").then(|| oid.to_string())
}

fn canonical_diff_path(path: &[u8]) -> (String, Option<ExclusionReason>) {
    match std::str::from_utf8(path) {
        Ok(path) if validate_repo_path(path).is_ok() => (path.to_string(), None),
        _ => (escaped_path(path), Some(ExclusionReason::UnsafePath)),
    }
}

fn escaped_path(path: &[u8]) -> String {
    path.iter()
        .map(|byte| {
            if byte.is_ascii_graphic() && *byte != b'\\' {
                char::from(*byte).to_string()
            } else {
                format!("\\x{byte:02x}")
            }
        })
        .collect()
}

fn git_text(repo_root: &Path, args: &[&str]) -> Result<String> {
    let bytes = git(repo_root, args, None)?;
    Ok(String::from_utf8(bytes)
        .map_err(|error| EvidenceError::Git {
            operation: format!("git {}", args.join(" ")),
            message: error.to_string(),
        })?
        .trim()
        .to_string())
}

fn git_command(repo_root: &Path, args: &[&str]) -> Command {
    let mut command = Command::new("git");
    let path = std::env::var_os("PATH");
    let system_root = std::env::var_os("SYSTEMROOT");
    command.env_clear();
    if let Some(path) = path {
        command.env("PATH", path);
    }
    if let Some(system_root) = system_root {
        command.env("SYSTEMROOT", system_root);
    }
    command
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_NO_LAZY_FETCH", "1")
        .env("GIT_NO_REPLACE_OBJECTS", "1")
        .env("GIT_OPTIONAL_LOCKS", "0")
        .env("GIT_TERMINAL_PROMPT", "0")
        .args([
            "--no-replace-objects",
            "--literal-pathspecs",
            "-c",
            "core.useReplaceRefs=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "diff.external=",
        ])
        .arg("-C")
        .arg(repo_root)
        .args(args);
    command
}

fn git(repo_root: &Path, args: &[&str], input: Option<&[u8]>) -> Result<Vec<u8>> {
    let mut command = git_command(repo_root, args);
    if input.is_some() || args.last() == Some(&"--stdin") {
        command.stdin(Stdio::piped());
    }
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = command.spawn().map_err(|error| EvidenceError::Git {
        operation: format!("git {}", args.join(" ")),
        message: error.to_string(),
    })?;
    if let Some(mut stdin) = child.stdin.take()
        && let Err(error) = stdin.write_all(input.unwrap_or_default())
    {
        let _ = child.kill();
        let _ = child.wait();
        return Err(EvidenceError::Git {
            operation: format!("git {}", args.join(" ")),
            message: error.to_string(),
        });
    }
    // Drain both pipes while waiting: waiting before reading deadlocks once a
    // large commit diff fills the pipe. Readers also cap retained output bytes.
    let read = |stream: Box<dyn std::io::Read + Send>| {
        std::thread::spawn(move || {
            let mut bytes = Vec::new();
            stream.take(16 * 1024 * 1024 + 1).read_to_end(&mut bytes)?;
            Ok::<_, std::io::Error>(bytes)
        })
    };
    let stdout = read(Box::new(child.stdout.take().expect("piped stdout")));
    let stderr = read(Box::new(child.stderr.take().expect("piped stderr")));
    let status = child.wait_timeout(Duration::from_secs(10));
    if !matches!(status, Ok(Some(_))) {
        let _ = child.kill();
        let _ = child.wait();
    }
    let failure = |message: String| EvidenceError::Git {
        operation: format!("git {}", args.join(" ")),
        message,
    };
    let stdout = stdout
        .join()
        .map_err(|_| failure("stdout reader failed".into()))?
        .map_err(|error| failure(error.to_string()))?;
    let stderr = stderr
        .join()
        .map_err(|_| failure("stderr reader failed".into()))?
        .map_err(|error| failure(error.to_string()))?;
    let status = status
        .map_err(|error| failure(error.to_string()))?
        .ok_or_else(|| failure("provenance command exceeded 10 seconds".into()))?;
    if stdout.len() > 16 * 1024 * 1024 || stderr.len() > 16 * 1024 * 1024 {
        return Err(failure("provenance output exceeds its byte bound".into()));
    }
    if !status.success() {
        return Err(failure(String::from_utf8_lossy(&stderr).trim().to_string()));
    }
    Ok(stdout)
}
