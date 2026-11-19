//! Side-effect captures.
//!
//! `files_home_write` snapshots `<home>/files` (each regular file's path relative to `files`, and
//! the SHA-256 of its bytes) before and after a request. Mtime is not used. The captures are the
//! created, modified and removed paths whose relative path matches the case's glob, in path order.
//! `pg_notify` and `ws_publish` captures need a live listener and join the full-daemon leg.

use std::collections::{BTreeMap, BTreeSet};
use std::io;
use std::path::Path;

use serde_json::{Value, json};
use sha2::{Digest, Sha256};

/// Walk `dir` (under `root`) and hash every regular file, and every symbolic link to one. A
/// directory reached through a symbolic link is not entered, as pathlib's `glob` does not recurse
/// through one (recurse_symlinks is false by default).
fn collect_files(root: &Path, dir: &Path, out: &mut FileSnapshot) -> io::Result<()> {
    for entry in std::fs::read_dir(dir)? {
        let path = entry?.path();
        let metadata = std::fs::symlink_metadata(&path)?;
        let is_link = metadata.file_type().is_symlink();
        let link_to_file = is_link && std::fs::metadata(&path).is_ok_and(|target| target.is_file());
        if metadata.is_dir() {
            collect_files(root, &path, out)?;
        } else if metadata.is_file() || link_to_file {
            let relative = path.strip_prefix(root).map_err(io::Error::other)?;
            let key = relative
                .components()
                .map(|part| part.as_os_str().to_string_lossy().into_owned())
                .collect::<Vec<_>>()
                .join("/");
            let digest = Sha256::digest(std::fs::read(&path)?);
            out.insert(key, format!("{digest:x}"));
        }
    }
    Ok(())
}

/// Byte-wise glob match: `**` matches any run of bytes, `*` any run within one segment.
fn glob_bytes(pattern: &[u8], path: &[u8]) -> bool {
    match pattern {
        [] => path.is_empty(),
        // `**/` matches zero or more whole segments, as pathlib's glob does: `**/*.json` reaches a
        // file in the root.
        [b'*', b'*', b'/', rest @ ..] => {
            glob_bytes(rest, path)
                || path
                    .iter()
                    .enumerate()
                    .any(|(index, byte)| *byte == b'/' && glob_bytes(pattern, &path[index + 1..]))
        }
        [b'*', b'*', rest @ ..] => (0..=path.len()).any(|skip| glob_bytes(rest, &path[skip..])),
        [b'*', rest @ ..] => {
            let mut skip = 0;
            loop {
                if glob_bytes(rest, &path[skip..]) {
                    return true;
                }
                if skip == path.len() || path[skip] == b'/' {
                    return false;
                }
                skip += 1;
            }
        }
        [literal, rest @ ..] => path.first() == Some(literal) && glob_bytes(rest, &path[1..]),
    }
}

/// Relative path under `files` to the lowercase hex SHA-256 of the file's bytes.
pub type FileSnapshot = BTreeMap<String, String>;

/// Hash every regular file under `<home>/files`. A missing `files` directory is an empty
/// snapshot.
pub fn files_home_snapshot(home: &Path) -> io::Result<FileSnapshot> {
    let files = home.join("files");
    let mut snapshot = FileSnapshot::new();
    if files.is_dir() {
        collect_files(&files, &files, &mut snapshot)?;
    }
    Ok(snapshot)
}

/// The created, modified and removed captures between two snapshots whose path matches `glob`.
/// Each capture is `{"change": ..., "path": ...}`. The change kinds come in the order created,
/// modified, removed, and each kind is sorted by path, as `files_captures` of `recorder_v3` orders
/// them.
pub fn files_home_captures(before: &FileSnapshot, after: &FileSnapshot, glob: &str) -> Vec<Value> {
    let paths: BTreeSet<&String> = before.keys().chain(after.keys()).collect();
    let mut captures = Vec::new();
    for kind in ["created", "modified", "removed"] {
        for path in &paths {
            if glob_matches(glob, path) && change_of(before, after, path) == Some(kind) {
                captures.push(json!({"change": kind, "path": path}));
            }
        }
    }
    captures
}

/// The change kind of one path between two snapshots, or `None` when nothing changed.
fn change_of(before: &FileSnapshot, after: &FileSnapshot, path: &str) -> Option<&'static str> {
    match (before.get(path), after.get(path)) {
        (None, Some(_)) => Some("created"),
        (Some(_), None) => Some("removed"),
        (Some(previous), Some(current)) if previous != current => Some("modified"),
        _ => None,
    }
}

/// Whether `path` matches `pattern`. `*` matches any run of characters within one path segment,
/// and `**` matches any run of characters across segments.
pub fn glob_matches(pattern: &str, path: &str) -> bool {
    glob_bytes(pattern.as_bytes(), path.as_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use sha2::{Digest, Sha256};

    fn digest_of(bytes: &[u8]) -> String {
        format!("{:x}", Sha256::digest(bytes))
    }

    #[test]
    fn star_stays_in_one_segment_and_double_star_crosses_segments() {
        assert!(glob_matches("*.json", "a.json"));
        assert!(!glob_matches("*.json", "x/a.json"));
        assert!(glob_matches("**/*.json", "x/y/a.json"));
        assert!(
            glob_matches("**/*.json", "a.json"),
            "** takes zero segments"
        );
        assert!(!glob_matches("**/*.json", "x/c.txt"));
        assert!(glob_matches("notes/*", "notes/a.txt"));
        assert!(!glob_matches("notes/*", "other/a.txt"));
    }

    #[test]
    fn created_modified_and_removed_files_are_captured_by_glob_change_kind_then_path() {
        let before: FileSnapshot = [("a.json", "h1"), ("b.json", "h2"), ("c.txt", "h3")]
            .map(|(path, hash)| (path.to_owned(), hash.to_owned()))
            .into_iter()
            .collect();
        let after: FileSnapshot = [("a.json", "h1x"), ("c.txt", "h3"), ("d.json", "h4")]
            .map(|(path, hash)| (path.to_owned(), hash.to_owned()))
            .into_iter()
            .collect();
        assert_eq!(
            files_home_captures(&before, &after, "*.json"),
            vec![
                json!({"change": "created", "path": "d.json"}),
                json!({"change": "modified", "path": "a.json"}),
                json!({"change": "removed", "path": "b.json"}),
            ]
        );
    }

    #[test]
    fn captures_follow_the_python_files_captures_golden_for_the_glob_forms() {
        // Parity golden (recorder_v3.snapshot_files and files_captures, one run of the Python
        // module): `**/*.json` over a.json, sub/b.json and sub/deep/c.txt finds a.json and
        // sub/b.json; `*.json` finds only a.json.
        let home = tempfile::tempdir().expect("home");
        let files = home.path().join("files");
        std::fs::create_dir_all(files.join("sub").join("deep")).expect("dirs");
        std::fs::write(files.join("a.json"), b"a").expect("write a");
        std::fs::write(files.join("sub").join("b.json"), b"b").expect("write b");
        std::fs::write(files.join("sub").join("deep").join("c.txt"), b"c").expect("write c");
        let snapshot = files_home_snapshot(home.path()).expect("snapshot");
        let empty = FileSnapshot::new();
        assert_eq!(
            files_home_captures(&empty, &snapshot, "**/*.json"),
            vec![
                json!({"change": "created", "path": "a.json"}),
                json!({"change": "created", "path": "sub/b.json"}),
            ]
        );
        assert_eq!(
            files_home_captures(&empty, &snapshot, "*.json"),
            vec![json!({"change": "created", "path": "a.json"})]
        );
    }

    #[cfg(unix)]
    #[test]
    fn a_link_to_a_file_is_a_file_and_a_link_to_a_directory_is_not_entered() {
        let home = tempfile::tempdir().expect("home");
        let files = home.path().join("files");
        let elsewhere = home.path().join("elsewhere");
        std::fs::create_dir_all(&files).expect("files");
        std::fs::create_dir_all(&elsewhere).expect("elsewhere");
        std::fs::write(elsewhere.join("target.json"), b"t").expect("write target");
        std::fs::write(elsewhere.join("inner.json"), b"i").expect("write inner");
        std::os::unix::fs::symlink(elsewhere.join("target.json"), files.join("link.json"))
            .expect("file link");
        std::os::unix::fs::symlink(&elsewhere, files.join("dirlink")).expect("dir link");
        let snapshot = files_home_snapshot(home.path()).expect("snapshot");
        assert_eq!(
            snapshot.keys().cloned().collect::<Vec<_>>(),
            vec!["link.json".to_owned()]
        );
    }

    /// Golden: `snapshot_files` of `recorder_v3.py` with the glob `**/*` on this tree gives
    /// `a.json`, `link_file.json` and `sub/b.json`. The link to a file is a file, and the link to
    /// the directory `linkdir` is not entered (pathlib's `glob` does not recurse into it).
    #[cfg(unix)]
    #[test]
    fn the_symlink_tree_matches_the_python_snapshot_golden() {
        let home = tempfile::tempdir().expect("home");
        let files = home.path().join("files");
        std::fs::create_dir_all(files.join("sub")).expect("sub");
        std::fs::write(files.join("a.json"), b"a").expect("a");
        std::fs::write(files.join("sub").join("b.json"), b"b").expect("b");
        std::os::unix::fs::symlink(files.join("a.json"), files.join("link_file.json"))
            .expect("file link");
        std::os::unix::fs::symlink(files.join("sub"), files.join("linkdir")).expect("dir link");
        let snapshot = files_home_snapshot(home.path()).expect("snapshot");
        assert_eq!(
            snapshot.keys().cloned().collect::<Vec<_>>(),
            vec![
                "a.json".to_owned(),
                "link_file.json".to_owned(),
                "sub/b.json".to_owned()
            ]
        );
    }

    #[test]
    fn snapshot_hashes_regular_files_under_files_by_relative_path() {
        let home = tempfile::tempdir().expect("home");
        let files = home.path().join("files");
        std::fs::create_dir_all(files.join("sub")).expect("dirs");
        std::fs::write(files.join("x.json"), b"abc").expect("write x");
        std::fs::write(files.join("sub").join("y.json"), b"def").expect("write y");

        let snapshot = files_home_snapshot(home.path()).expect("snapshot");
        let expected: FileSnapshot = [
            ("sub/y.json".to_owned(), digest_of(b"def")),
            ("x.json".to_owned(), digest_of(b"abc")),
        ]
        .into_iter()
        .collect();
        assert_eq!(snapshot, expected);
    }

    #[test]
    fn a_home_without_a_files_directory_has_an_empty_snapshot() {
        let home = tempfile::tempdir().expect("home");
        assert!(
            files_home_snapshot(home.path())
                .expect("snapshot")
                .is_empty()
        );
    }
}
