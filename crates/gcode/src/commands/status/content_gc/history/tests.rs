use std::collections::{BTreeSet, HashSet};
use std::path::Path;
use std::process::Command;
use std::time::Duration;

use super::recent_path_hashes;
use crate::index::hasher;

const RETENTION_DAYS: i32 = 17;
// Author and committer date in 2000, far outside the retention window.
const OLD_DATE: &str = "@946684800 +0000";

struct Repo {
    dir: tempfile::TempDir,
}

impl Repo {
    fn init(extra: &[&str]) -> Self {
        let dir = tempfile::tempdir().expect("create scratch repo");
        let status = Command::new("git")
            .args(["init", "-q"])
            .args(extra)
            .arg(dir.path())
            .status()
            .expect("run git init");
        assert!(status.success(), "git init failed");
        Self { dir }
    }

    fn path(&self) -> &Path {
        self.dir.path()
    }

    fn git(&self, args: &[&str]) -> String {
        self.run(args, &[])
    }

    fn run(&self, args: &[&str], envs: &[(&str, &str)]) -> String {
        let output = Command::new("git")
            .arg("-C")
            .arg(self.path())
            .args([
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
            ])
            .args(args)
            .env("GIT_AUTHOR_NAME", "Gcode Test")
            .env("GIT_AUTHOR_EMAIL", "gcode@example.invalid")
            .env("GIT_COMMITTER_NAME", "Gcode Test")
            .env("GIT_COMMITTER_EMAIL", "gcode@example.invalid")
            .envs(envs.iter().copied())
            .output()
            .expect("run git");
        assert!(
            output.status.success(),
            "git {args:?} failed: {}",
            String::from_utf8_lossy(&output.stderr),
        );
        String::from_utf8(output.stdout)
            .expect("utf-8 git output")
            .trim_end()
            .to_string()
    }

    fn write(&self, relative: &str, body: &str) {
        let file = self.path().join(relative);
        std::fs::create_dir_all(file.parent().expect("fixture file has a parent"))
            .expect("create fixture directories");
        std::fs::write(file, body).expect("write fixture file");
    }

    fn commit(&self, message: &str, date: Option<&str>) {
        self.git(&["add", "-A"]);
        let envs = date.map_or(Vec::new(), |date| {
            vec![("GIT_AUTHOR_DATE", date), ("GIT_COMMITTER_DATE", date)]
        });
        self.run(&["commit", "-q", "-m", message], &envs);
    }
}

fn protected(repo: &Repo, candidate_paths: &[&str]) -> BTreeSet<(String, String)> {
    let candidates = candidate_paths
        .iter()
        .map(|path| (*path).to_string())
        .collect();
    recent_path_hashes(repo.path(), RETENTION_DAYS, &candidates)
        .expect("read recent path hashes")
        .into_iter()
        .collect()
}

fn pair(path: &str, body: &str) -> (String, String) {
    (path.to_string(), hasher::content_hash(body.as_bytes()))
}

#[test]
fn duplicate_blob_protects_only_its_history_path() {
    let repo = Repo::init(&[]);
    repo.write("a.txt", "dup\n");
    repo.commit("seed", None);
    // The same bytes exist on disk at a second path that history never held.
    repo.write("b.txt", "dup\n");

    assert_eq!(
        protected(&repo, &["a.txt", "b.txt"]),
        BTreeSet::from([pair("a.txt", "dup\n")]),
    );
}

#[test]
fn rename_protects_each_path_by_its_own_history() {
    let repo = Repo::init(&[]);
    repo.write("old.txt", "before\n");
    repo.commit("seed", None);
    // The rename also rewrites the content: "before" is held only at the old
    // path, and "after" only at the new one.
    std::fs::remove_file(repo.path().join("old.txt")).expect("remove old path");
    repo.write("new.txt", "after\n");
    repo.commit("rename and edit", None);

    assert_eq!(
        protected(&repo, &["old.txt", "new.txt"]),
        BTreeSet::from([pair("old.txt", "before\n"), pair("new.txt", "after\n")]),
    );
}

#[test]
fn unusual_paths_round_trip() {
    let repo = Repo::init(&[]);
    repo.write("with space.txt", "spaced\n");
    repo.write("dir name/new\nline.txt", "newline\n");
    repo.commit("seed", None);

    assert_eq!(
        protected(&repo, &["with space.txt", "dir name/new\nline.txt"]),
        BTreeSet::from([
            pair("with space.txt", "spaced\n"),
            pair("dir name/new\nline.txt", "newline\n"),
        ]),
    );
}

#[test]
fn filtered_content_hashes_per_path() {
    let repo = Repo::init(&[]);
    repo.git(&["config", "filter.upper.smudge", "tr a-z A-Z"]);
    repo.write(".gitattributes", "filtered.txt filter=upper\n");
    repo.write("filtered.txt", "hello\n");
    repo.write("plain.txt", "hello\n");
    repo.commit("seed", None);
    // Checkout applies the smudge filter, so the file on disk is what the
    // indexer hashes. Protection must match that, not the stored blob.
    std::fs::remove_file(repo.path().join("filtered.txt")).expect("remove filtered file");
    repo.git(&["checkout", "--", "filtered.txt"]);
    let checked_out = std::fs::read(repo.path().join("filtered.txt")).expect("read checkout");

    assert_eq!(
        protected(&repo, &["filtered.txt", "plain.txt"]),
        BTreeSet::from([
            (
                "filtered.txt".to_string(),
                hasher::content_hash(&checked_out),
            ),
            pair("plain.txt", "hello\n"),
        ]),
    );
}

#[test]
fn size_growing_filter_hashes_checkout_bytes() {
    let repo = Repo::init(&[]);
    repo.write(".gitattributes", "crlf.txt text eol=crlf\n");
    repo.write("crlf.txt", "a\nb\n");
    repo.commit("seed", None);
    // The stored blob has LF endings. Checkout converts them to CRLF, so the
    // filtered body is longer than the object size git reports for the blob.
    std::fs::remove_file(repo.path().join("crlf.txt")).expect("remove crlf file");
    repo.git(&["checkout", "--", "crlf.txt"]);
    let checked_out = std::fs::read(repo.path().join("crlf.txt")).expect("read checkout");
    assert_ne!(checked_out, b"a\nb\n");

    assert_eq!(
        protected(&repo, &["crlf.txt"]),
        BTreeSet::from([("crlf.txt".to_string(), hasher::content_hash(&checked_out),)]),
    );
}

#[test]
fn size_shrinking_filter_does_not_hang() {
    let repo = Repo::init(&[]);
    repo.git(&["config", "filter.shrink.smudge", "head -c 3"]);
    repo.write(".gitattributes", "shrink.txt filter=shrink\n");
    repo.write("shrink.txt", "abcdef\n");
    repo.commit("seed", None);
    // The smudge filter keeps three bytes, fewer than the object size git reports.
    std::fs::remove_file(repo.path().join("shrink.txt")).expect("remove shrink file");
    repo.git(&["checkout", "--", "shrink.txt"]);

    let (sender, receiver) = std::sync::mpsc::channel();
    let root = repo.path().to_path_buf();
    std::thread::spawn(move || {
        let candidates = BTreeSet::from(["shrink.txt".to_string()]);
        let _ = sender.send(recent_path_hashes(&root, RETENTION_DAYS, &candidates));
    });
    let received = receiver.recv_timeout(Duration::from_secs(30));
    assert!(
        received.is_ok(),
        "recent_path_hashes hung on a shrinking filter",
    );
    let hashes: BTreeSet<_> = received
        .expect("reader sent a result")
        .expect("read recent path hashes")
        .into_iter()
        .collect();

    assert_eq!(hashes, BTreeSet::from([pair("shrink.txt", "abc")]));
}

#[test]
fn sha256_repository_tree_walk() {
    let repo = Repo::init(&["--object-format=sha256"]);
    assert_eq!(repo.git(&["rev-parse", "--show-object-format"]), "sha256");
    repo.write("src/lib.rs", "sha\n");
    repo.commit("seed", None);

    assert_eq!(
        protected(&repo, &["src/lib.rs"]),
        BTreeSet::from([pair("src/lib.rs", "sha\n")]),
    );
}

#[test]
fn recent_ancestor_behind_old_tip_stays_protected() {
    let repo = Repo::init(&[]);
    repo.write("keep.txt", "kept\n");
    repo.commit("recent ancestor", None);
    repo.write("other.txt", "later\n");
    repo.commit("old tip", Some(OLD_DATE));

    assert_eq!(
        protected(&repo, &["keep.txt"]),
        BTreeSet::from([pair("keep.txt", "kept\n")]),
    );
}

#[test]
fn nested_root_protects_root_relative_paths() {
    let repo = Repo::init(&[]);
    repo.write("sub/lib.rs", "nested\n");
    repo.commit("seed", None);
    // The root is a project below the git top level, as a nested .gobby project is.
    let candidates = BTreeSet::from(["lib.rs".to_string()]);
    let nested = recent_path_hashes(&repo.path().join("sub"), RETENTION_DAYS, &candidates)
        .expect("read recent path hashes");

    assert_eq!(nested, HashSet::from([pair("lib.rs", "nested\n")]));
}

#[test]
fn legacy_file_modes_are_protected() {
    let repo = Repo::init(&[]);
    repo.write("legacy.txt", "legacy\n");
    let blob = repo.git(&["hash-object", "-w", "legacy.txt"]);
    // Current git normalizes modes on every write path, so a 100664 entry exists
    // only in trees older tools wrote. Write the raw tree object to produce one.
    let mut tree = b"100664 legacy.txt\0".to_vec();
    tree.extend(raw_oid(&blob));
    std::fs::write(repo.path().join("legacy-tree.bin"), &tree).expect("write raw tree");
    let tree_oid = repo.git(&[
        "hash-object",
        "-t",
        "tree",
        "--literally",
        "-w",
        "legacy-tree.bin",
    ]);
    let commit = repo.run(
        &["commit-tree", tree_oid.as_str(), "-m", "legacy modes"],
        &[],
    );
    repo.git(&["update-ref", "HEAD", commit.as_str()]);

    assert_eq!(
        protected(&repo, &["legacy.txt"]),
        BTreeSet::from([pair("legacy.txt", "legacy\n")]),
    );
}

fn raw_oid(hex: &str) -> Vec<u8> {
    (0..hex.len())
        .step_by(2)
        .map(|index| u8::from_str_radix(&hex[index..index + 2], 16).expect("hex object id"))
        .collect()
}
