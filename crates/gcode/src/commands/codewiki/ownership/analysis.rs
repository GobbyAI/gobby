use std::collections::{BTreeMap, btree_map::Entry};
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;
use std::process::{ExitStatus, Stdio};
use std::time::{Duration, Instant};

use anyhow::Context;
use sha2::{Digest, Sha256};
use wait_timeout::ChildExt;

use super::{
    CachedBlameSummary, OwnershipContributor, OwnershipMeta, OwnershipOptions, OwnershipStatus,
};
use crate::index::hasher;

#[derive(Debug)]
enum BlameContributorsOutcome {
    Success(Vec<OwnershipContributor>),
    Timeout,
    Error(anyhow::Error),
}

pub(super) fn derived_owners_for_files(
    project_root: &Path,
    files: &[String],
    meta: &mut OwnershipMeta,
    options: &OwnershipOptions,
    status: &mut OwnershipStatus,
) -> BTreeMap<String, Vec<OwnershipContributor>> {
    let Ok(repo) = gix::discover(project_root) else {
        return BTreeMap::new();
    };
    let Ok(head) = repo.head_id() else {
        return BTreeMap::new();
    };
    let head = head.detach();
    status.blame_available = true;

    let started = Instant::now();
    let mut out = BTreeMap::new();
    let mut blame_misses = 0usize;
    for file in files {
        if started.elapsed() >= options.blame_timeout {
            status.partial = true;
            break;
        }
        let Some(content_hash) = content_hash(project_root, file) else {
            continue;
        };
        if let Some(cached) = meta
            .files
            .get(file)
            .filter(|cached| cached.content_hash == content_hash)
        {
            out.insert(file.clone(), cached.contributors.clone());
            continue;
        }
        if blame_misses >= options.blame_file_cap {
            status.partial = true;
            break;
        }
        blame_misses += 1;
        let remaining_timeout = options.blame_timeout.saturating_sub(started.elapsed());
        match blame_file_contributors_with_timeout(project_root, head, file, remaining_timeout) {
            BlameContributorsOutcome::Success(contributors) => {
                meta.files.insert(
                    file.clone(),
                    CachedBlameSummary {
                        content_hash,
                        contributors: contributors.clone(),
                    },
                );
                out.insert(file.clone(), contributors);
            }
            BlameContributorsOutcome::Timeout => {
                status.partial = true;
                break;
            }
            BlameContributorsOutcome::Error(error) => {
                log::debug!("codewiki ownership git blame failed for `{file}`: {error}");
                status.partial = true;
                status.blame_errors = true;
            }
        }
    }
    out
}

pub(super) fn content_hash(project_root: &Path, file: &str) -> Option<String> {
    hasher::file_content_hash(&project_root.join(file)).ok()
}

fn blame_file_contributors_with_timeout(
    project_root: &Path,
    head: gix::ObjectId,
    file: &str,
    timeout: Duration,
) -> BlameContributorsOutcome {
    match blame_file_contributors(project_root, head, file, timeout) {
        Ok(Some(contributors)) => BlameContributorsOutcome::Success(contributors),
        Ok(None) => BlameContributorsOutcome::Timeout,
        Err(error) => BlameContributorsOutcome::Error(error),
    }
}

pub(super) struct GitBlameOutput {
    status: ExitStatus,
    stdout: String,
    stderr: String,
}

fn blame_file_contributors(
    project_root: &Path,
    head: gix::ObjectId,
    file: &str,
    timeout: Duration,
) -> anyhow::Result<Option<Vec<OwnershipContributor>>> {
    let head = head.to_string();
    let Some(output) =
        git_blame_output_with_timeout(Path::new("git"), project_root, &head, file, timeout)?
    else {
        return Ok(None);
    };
    if !output.status.success() {
        let stderr = output.stderr.trim();
        if stderr.is_empty() {
            anyhow::bail!("git blame failed for `{file}`");
        }
        anyhow::bail!("git blame failed for `{file}`: {stderr}");
    }

    Ok(Some(parse_git_blame_porcelain(&output.stdout)))
}

pub(super) fn git_blame_output_with_timeout(
    git: &Path,
    project_root: &Path,
    head: &str,
    file: &str,
    timeout: Duration,
) -> anyhow::Result<Option<GitBlameOutput>> {
    let mut stdout_file = tempfile::tempfile().context("create git blame stdout temp file")?;
    let mut stderr_file = tempfile::tempfile().context("create git blame stderr temp file")?;
    let mut child = std::process::Command::new(git)
        .arg("-C")
        .arg(project_root)
        .args(["blame", "--line-porcelain", head, "--", file])
        .stdout(Stdio::from(stdout_file.try_clone()?))
        .stderr(Stdio::from(stderr_file.try_clone()?))
        .spawn()
        .with_context(|| format!("spawn git blame for `{file}`"))?;
    let status = match child.wait_timeout(timeout)? {
        Some(status) => status,
        None => {
            let _ = child.kill();
            let _ = child.wait();
            return Ok(None);
        }
    };
    Ok(Some(GitBlameOutput {
        status,
        stdout: read_tempfile(&mut stdout_file)?,
        stderr: read_tempfile(&mut stderr_file)?,
    }))
}

fn read_tempfile(file: &mut std::fs::File) -> anyhow::Result<String> {
    file.seek(SeekFrom::Start(0))?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

fn parse_git_blame_porcelain(raw: &str) -> Vec<OwnershipContributor> {
    let mut line_counts: BTreeMap<String, (String, Option<String>, usize)> = BTreeMap::new();
    let mut current_name: Option<String> = None;
    let mut current_email: Option<String> = None;
    for line in raw.lines() {
        if let Some(name) = line.strip_prefix("author ") {
            current_name = Some(name.to_string());
            continue;
        }
        if let Some(email) = line.strip_prefix("author-mail ") {
            current_email = git_blame_email(email);
            continue;
        }
        if !line.starts_with('\t') {
            continue;
        }

        let name = current_name
            .clone()
            .unwrap_or_else(|| "Unknown".to_string());
        let email = current_email.clone();
        let contributor_id = contributor_id(&name, email.as_deref());
        match line_counts.entry(contributor_id) {
            Entry::Occupied(mut stored) => {
                let (stored_name, stored_email, lines) = stored.get_mut();
                retain_deterministic_identity(stored_name, stored_email, &name, &email);
                *lines += 1;
            }
            Entry::Vacant(stored) => {
                stored.insert((name, email, 1));
            }
        }
    }
    let mut contributors = line_counts
        .into_iter()
        .map(
            |(contributor_id, (name, email, lines))| OwnershipContributor {
                contributor_id,
                name,
                email,
                lines,
            },
        )
        .collect::<Vec<_>>();
    contributors.sort_by(|left, right| {
        right
            .lines
            .cmp(&left.lines)
            .then_with(|| left.name.cmp(&right.name))
            .then_with(|| left.contributor_id.cmp(&right.contributor_id))
    });
    contributors.truncate(5);
    contributors
}

fn git_blame_email(value: &str) -> Option<String> {
    let trimmed = value.trim();
    let trimmed = trimmed
        .strip_prefix('<')
        .and_then(|inner| inner.strip_suffix('>'))
        .unwrap_or(trimmed);
    (!trimmed.is_empty()).then(|| trimmed.to_string())
}

fn contributor_id(name: &str, email: Option<&str>) -> String {
    let identity = email
        .map(str::trim)
        .filter(|email| !email.is_empty())
        .unwrap_or(name)
        .trim()
        .to_ascii_lowercase();
    let digest = Sha256::digest(identity.as_bytes());
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

pub(super) fn retain_deterministic_identity(
    stored_name: &mut String,
    stored_email: &mut Option<String>,
    name: &str,
    email: &Option<String>,
) {
    if name < stored_name.as_str() {
        *stored_name = name.to_string();
    }
    match (stored_email.as_mut(), email) {
        (Some(stored), Some(next)) if next < stored => *stored = next.clone(),
        (None, Some(next)) => *stored_email = Some(next.clone()),
        _ => {}
    }
}
