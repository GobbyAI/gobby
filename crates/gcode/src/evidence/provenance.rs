//! Recorded Git provenance; never constructs a source checkout or index.
use super::contracts::{
    ChangeStatus, ChangedPath, CommitBinding, ComparisonKind, ExclusionReason, RepositoryBinding,
};
use super::source::{canonical_hash, validate_repo_path};
use super::{EvidenceError, Result};
use std::io::{Read as _, Write as _};
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::Duration;
use wait_timeout::ChildExt;

const MAX_BOUND_BLOB_BYTES: usize = 10 * 1024 * 1024;
const MAX_GIT_OUTPUT_BYTES: usize = 16 * 1024 * 1024;

/// Error for a path whose bytes cannot be read at the binding commit.
pub(super) fn bound_blob_unavailable(
    binding: &RepositoryBinding,
    path: &str,
    detail: String,
) -> EvidenceError {
    EvidenceError::BindingMismatch {
        detail: format!(
            "cannot read {path} at binding commit {} tree {}: {detail}",
            binding.commit_oid, binding.tree_oid
        ),
    }
}

/// Confirms the bound object is a commit whose tree is the bound tree.
pub(super) fn verify_bound_commit(
    repo_root: &Path,
    binding: &RepositoryBinding,
) -> std::result::Result<(), String> {
    let text = |args: &[&str]| git_text(repo_root, args).map_err(|error| error.to_string());
    if text(&["cat-file", "-t", &binding.commit_oid])? != "commit" {
        return Err("bound object is not a commit".into());
    }
    let tree = text(&["rev-parse", &format!("{}^{{tree}}", binding.commit_oid)])?;
    if tree != binding.tree_oid {
        return Err(format!("commit has tree {tree}"));
    }
    Ok(())
}

/// Reads regular-file blobs at a verified binding commit, one result per path in order.
/// One `ls-tree` lists every path and `cat-file --batch` reads the blobs, so the Git
/// process count does not grow with the number of paths.
pub(super) fn read_bound_blobs(
    repo_root: &Path,
    binding: &RepositoryBinding,
    paths: &[&str],
) -> Vec<Result<Vec<u8>>> {
    read_listed_blobs(repo_root, &binding.commit_oid, paths)
        .into_iter()
        .zip(paths)
        .map(|(blob, path)| blob.map_err(|detail| bound_blob_unavailable(binding, path, detail)))
        .collect()
}

type BlobRead = std::result::Result<Vec<u8>, String>;

fn read_listed_blobs(repo_root: &Path, commit_oid: &str, paths: &[&str]) -> Vec<BlobRead> {
    let mut args = vec!["ls-tree", "-z", "-l", commit_oid, "--"];
    args.extend_from_slice(paths);
    let listing = match git(repo_root, &args, None) {
        Ok(listing) => listing,
        Err(error) => return vec![Err(error.to_string()); paths.len()],
    };
    let mut listed = std::collections::BTreeMap::new();
    for record in listing
        .split(|byte| *byte == 0)
        .filter(|record| !record.is_empty())
    {
        let Some(separator) = record.iter().position(|byte| *byte == b'\t') else {
            return vec![Err("invalid Git tree entry".into()); paths.len()];
        };
        listed.insert(&record[separator + 1..], &record[..separator]);
    }
    let located = paths
        .iter()
        .map(|path| listed_blob(listed.get(path.as_bytes()).copied()))
        .collect::<Vec<_>>();
    let mut blobs = located
        .iter()
        .map(|blob| match blob {
            Ok(_) => Err("bound blob was not read".to_string()),
            Err(detail) => Err(detail.clone()),
        })
        .collect::<Vec<BlobRead>>();
    let wanted = located
        .iter()
        .enumerate()
        .filter_map(|(index, blob)| blob.as_ref().ok().map(|(oid, size)| (index, *oid, *size)))
        .collect::<Vec<_>>();
    // Split the reads so each batch's output stays under the Git output bound.
    let mut start = 0;
    while start < wanted.len() {
        let mut end = start;
        let mut output_bytes = 0;
        while end < wanted.len() {
            let (_, oid, size) = wanted[end];
            let next = size + oid.len() + 64;
            if end > start && output_bytes + next > MAX_GIT_OUTPUT_BYTES {
                break;
            }
            output_bytes += next;
            end += 1;
        }
        let chunk = &wanted[start..end];
        for ((index, _, _), blob) in chunk.iter().zip(cat_file_batch(repo_root, chunk)) {
            blobs[*index] = blob;
        }
        start = end;
    }
    blobs
}

/// Object id and size of a listed regular-file blob within the read bound.
fn listed_blob(metadata: Option<&[u8]>) -> std::result::Result<(&str, usize), String> {
    let metadata = metadata.ok_or_else(|| "path is absent from the bound commit".to_string())?;
    let metadata = std::str::from_utf8(metadata).map_err(|error| error.to_string())?;
    let fields = metadata.split_whitespace().collect::<Vec<_>>();
    if fields.len() != 4 || !matches!(fields[0], "100644" | "100755") || fields[1] != "blob" {
        return Err("bound path is not a regular file".into());
    }
    let size = fields[3]
        .parse::<usize>()
        .map_err(|error| error.to_string())?;
    if size > MAX_BOUND_BLOB_BYTES {
        return Err("bound blob exceeds 10 MiB".into());
    }
    Ok((fields[2], size))
}

/// Reads listed blobs through one `cat-file --batch`, checking each listed size.
fn cat_file_batch(repo_root: &Path, blobs: &[(usize, &str, usize)]) -> Vec<BlobRead> {
    let input = blobs
        .iter()
        .map(|(_, oid, _)| format!("{oid}\n"))
        .collect::<String>();
    let output = match git(repo_root, &["cat-file", "--batch"], Some(input.as_bytes())) {
        Ok(output) => output,
        Err(error) => return vec![Err(error.to_string()); blobs.len()],
    };
    let mut rest = output.as_slice();
    let mut reads = Vec::with_capacity(blobs.len());
    for (_, oid, size) in blobs {
        let Some(newline) = rest.iter().position(|byte| *byte == b'\n') else {
            reads.push(Err("Git batch output ended early".into()));
            continue;
        };
        let header = String::from_utf8_lossy(&rest[..newline]).into_owned();
        rest = &rest[newline + 1..];
        let fields = header.split(' ').collect::<Vec<_>>();
        if fields.len() == 2 && fields[0] == *oid && fields[1] == "missing" {
            reads.push(Err("bound blob is missing".into()));
            continue;
        }
        let listed = fields.len() == 3 && fields[0] == *oid && fields[1] == "blob";
        let Some(batch_size) = fields
            .get(2)
            .and_then(|size| size.parse::<usize>().ok())
            .filter(|_| listed)
        else {
            // The remaining stream cannot be framed without a valid header.
            reads.push(Err(format!("unexpected Git batch header {header}")));
            rest = &[];
            continue;
        };
        if rest.len() <= batch_size || rest[batch_size] != b'\n' {
            reads.push(Err("Git batch output ended early".into()));
            rest = &[];
            continue;
        }
        let bytes = rest[..batch_size].to_vec();
        rest = &rest[batch_size + 1..];
        reads.push(if batch_size == *size {
            Ok(bytes)
        } else {
            Err("bound blob size changed".into())
        });
    }
    reads
}

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
    let stdin = child.stdin.take();
    let stdout = child.stdout.take().expect("piped stdout");
    let stderr = child.stderr.take().expect("piped stderr");
    let failure = |message: String| EvidenceError::Git {
        operation: format!("git {}", args.join(" ")),
        message,
    };
    // Stdin, stdout, and stderr are serviced concurrently, and the wait runs under
    // the timeout. Writing all of stdin before draining stdout deadlocks once git
    // fills its stdout pipe while stdin is still unread, and a blocked write never
    // reaches the timeout. Readers also cap retained output bytes.
    std::thread::scope(|scope| -> Result<Vec<u8>> {
        let writer = scope.spawn(move || match stdin {
            Some(mut stdin) => stdin.write_all(input.unwrap_or_default()),
            None => Ok(()),
        });
        let stdout = scope.spawn(move || read_capped(stdout));
        let stderr = scope.spawn(move || read_capped(stderr));
        let status = child.wait_timeout(Duration::from_secs(10));
        if !matches!(status, Ok(Some(_))) {
            let _ = child.kill();
            let _ = child.wait();
        }
        let stdout = stdout
            .join()
            .map_err(|_| failure("stdout reader failed".into()))?
            .map_err(|error| failure(error.to_string()))?;
        let stderr = stderr
            .join()
            .map_err(|_| failure("stderr reader failed".into()))?
            .map_err(|error| failure(error.to_string()))?;
        let written = writer
            .join()
            .map_err(|_| failure("stdin writer failed".into()))?;
        let status = status
            .map_err(|error| failure(error.to_string()))?
            .ok_or_else(|| failure("provenance command exceeded 10 seconds".into()))?;
        if stdout.len() > 16 * 1024 * 1024 || stderr.len() > 16 * 1024 * 1024 {
            return Err(failure("provenance output exceeds its byte bound".into()));
        }
        if !status.success() {
            return Err(failure(String::from_utf8_lossy(&stderr).trim().to_string()));
        }
        written.map_err(|error| failure(error.to_string()))?;
        Ok(stdout)
    })
}

fn read_capped(stream: impl std::io::Read) -> std::io::Result<Vec<u8>> {
    let mut bytes = Vec::new();
    stream.take(16 * 1024 * 1024 + 1).read_to_end(&mut bytes)?;
    Ok(bytes)
}
