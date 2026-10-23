use std::collections::{BTreeSet, HashSet};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::Path;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

use anyhow::Context as _;

use crate::index::hasher;

/// Versions of `candidate_paths` that recent history holds at their own path,
/// as `(path, content_hash)` pairs.
///
/// Walks the root tree of every commit inside the retention window, descending
/// only into directories that lead to a candidate. Each blob found at a
/// candidate path is then read through that path's attributes, so the hash is
/// the one the indexer computes from the checkout.
pub(super) fn recent_path_hashes(
    root: &Path,
    retention_days: i32,
    candidate_paths: &BTreeSet<String>,
) -> anyhow::Result<HashSet<(String, String)>> {
    if candidate_paths.is_empty() {
        return Ok(HashSet::new());
    }
    let width = object_id_width(root)?;
    let roots = recent_root_trees(root, retention_days)?;
    let blobs = candidate_blobs(root, width, &roots, candidate_paths)?;
    hash_blobs(root, &blobs)
}

fn object_id_width(root: &Path) -> anyhow::Result<usize> {
    match git_stdout(root, &["rev-parse", "--show-object-format"])?.trim() {
        "sha1" => Ok(20),
        "sha256" => Ok(32),
        other => anyhow::bail!("unsupported git object format {other:?}"),
    }
}

fn recent_root_trees(root: &Path, retention_days: i32) -> anyhow::Result<BTreeSet<String>> {
    // `--since` would stop the walk at the first commit older than the window,
    // hiding recent ancestors of an old tip. `--since-as-filter` keeps walking.
    let since = format!("--since-as-filter={retention_days}.days");
    let trees = git_stdout(root, &["log", "--all", &since, "--format=%T"])?;
    Ok(trees.lines().map(str::to_string).collect())
}

/// Walks each root tree in lockstep, memoized per `(directory, tree)`, and
/// returns the `(path, blob)` pairs at candidate paths. Symlinks, submodules
/// and names that are not UTF-8 are skipped.
fn candidate_blobs(
    root: &Path,
    width: usize,
    roots: &BTreeSet<String>,
    candidate_paths: &BTreeSet<String>,
) -> anyhow::Result<BTreeSet<(String, String)>> {
    let mut directories = BTreeSet::new();
    for path in candidate_paths {
        directories.extend(
            path.match_indices('/')
                .map(|(index, _)| path[..=index].to_string()),
        );
    }

    let mut trees = Batch::spawn(root, &["--batch"], b'\n')?;
    let mut seen = HashSet::new();
    let mut pending: Vec<(String, String)> = roots
        .iter()
        .map(|oid| (String::new(), oid.clone()))
        .collect();
    let mut blobs = BTreeSet::new();
    while let Some((directory, oid)) = pending.pop() {
        if !seen.insert((directory.clone(), oid.clone())) {
            continue;
        }
        let Some(body) = trees.request(oid.as_bytes())? else {
            continue;
        };
        for entry in parse_tree(&body, width)? {
            let Ok(name) = std::str::from_utf8(entry.name) else {
                continue;
            };
            let path = format!("{directory}{name}");
            let child = format!("{path}/");
            match entry.mode {
                b"40000" if directories.contains(&child) => pending.push((child, entry.oid)),
                b"100644" | b"100755" if candidate_paths.contains(&path) => {
                    blobs.insert((path, entry.oid));
                }
                _ => {}
            }
        }
    }
    Ok(blobs)
}

fn hash_blobs(
    root: &Path,
    blobs: &BTreeSet<(String, String)>,
) -> anyhow::Result<HashSet<(String, String)>> {
    // `-Z` frames requests and responses with NUL, so a path containing a
    // newline stays one request and its body end is unambiguous. `--filters`
    // applies the path's attributes, matching the checkout the indexer reads.
    let mut blob_reader = Batch::spawn(root, &["--batch", "-Z", "--filters"], 0)?;
    let mut protected = HashSet::new();
    for (path, blob) in blobs {
        if let Some(body) = blob_reader.request(format!("{blob} {path}").as_bytes())? {
            protected.insert((path.clone(), hasher::content_hash(&body)));
        }
    }
    Ok(protected)
}

struct TreeEntry<'a> {
    mode: &'a [u8],
    name: &'a [u8],
    oid: String,
}

/// Parses a raw tree body: `<mode> SP <name> NUL <raw object id>` repeated.
fn parse_tree(mut body: &[u8], width: usize) -> anyhow::Result<Vec<TreeEntry<'_>>> {
    let mut entries = Vec::new();
    while !body.is_empty() {
        let space = body
            .iter()
            .position(|byte| *byte == b' ')
            .context("tree entry lacks a mode")?;
        let rest = &body[space + 1..];
        let nul = rest
            .iter()
            .position(|byte| *byte == 0)
            .context("tree entry lacks a name")?;
        let name = &rest[..nul];
        let rest = &rest[nul + 1..];
        anyhow::ensure!(rest.len() >= width, "tree entry lacks an object id");
        entries.push(TreeEntry {
            mode: &body[..space],
            name,
            oid: hex(&rest[..width]),
        });
        body = &rest[width..];
    }
    Ok(entries)
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn git_stdout(root: &Path, args: &[&str]) -> anyhow::Result<String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(args)
        .output()
        .with_context(|| format!("run git {args:?} in {}", root.display()))?;
    anyhow::ensure!(
        output.status.success(),
        "git {args:?} failed: {}",
        String::from_utf8_lossy(&output.stderr).trim(),
    );
    String::from_utf8(output.stdout).context("git output is not UTF-8")
}

/// A `git cat-file` process driven in lockstep: each request is written and
/// its response read before the next, so the child never blocks on a full pipe.
struct Batch {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    delimiter: u8,
}

impl Batch {
    fn spawn(root: &Path, options: &[&str], delimiter: u8) -> anyhow::Result<Self> {
        let mut child = Command::new("git")
            .arg("-C")
            .arg(root)
            .arg("cat-file")
            .args(options)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .with_context(|| format!("start git cat-file in {}", root.display()))?;
        let stdin = child.stdin.take().context("open git cat-file stdin")?;
        let stdout = BufReader::new(child.stdout.take().context("open git cat-file stdout")?);
        Ok(Self {
            child,
            stdin,
            stdout,
            delimiter,
        })
    }

    /// Returns the object body, or `None` when git reports it missing.
    fn request(&mut self, line: &[u8]) -> anyhow::Result<Option<Vec<u8>>> {
        self.stdin.write_all(line)?;
        self.stdin.write_all(&[self.delimiter])?;
        self.stdin.flush()?;

        let mut header = Vec::new();
        anyhow::ensure!(
            self.stdout.read_until(self.delimiter, &mut header)? > 0,
            "git cat-file ended before answering",
        );
        anyhow::ensure!(
            header.pop() == Some(self.delimiter),
            "git cat-file header lacked its delimiter",
        );
        let header = String::from_utf8(header).context("git cat-file header is not UTF-8")?;
        let fields = header.split_whitespace().collect::<Vec<_>>();
        if fields.last() == Some(&"missing") {
            return Ok(None);
        }
        let [_object, _kind, size] = fields[..] else {
            anyhow::bail!("unexpected git cat-file header {header:?}");
        };
        let size = size
            .parse::<usize>()
            .with_context(|| format!("parse git object size in {header:?}"))?;
        let mut body = vec![0; size.checked_add(1).context("git object size overflows")?];
        self.stdout.read_exact(&mut body)?;
        anyhow::ensure!(
            body.pop() == Some(self.delimiter),
            "git cat-file object {header:?} lacked its trailing delimiter",
        );
        Ok(Some(body))
    }
}

impl Drop for Batch {
    fn drop(&mut self) {
        // Every response is read before drop, so the child only waits on stdin.
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[cfg(test)]
mod tests;
