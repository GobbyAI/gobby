//! Side-effect captures (design-v2 sections 7.5.6 and 7.6; H44).
//!
//! `files_home_write` snapshots `<home>/files` (each regular file's path relative to `files`, and
//! the SHA-256 of its bytes) before and after a request. Mtime is not used. The captures are the
//! created, modified and removed paths whose relative path matches the case's glob, in path order.
//! `pg_notify` and `ws_publish` captures need a live listener and join the full-daemon leg.

use std::collections::BTreeMap;
use std::io;
use std::path::Path;

use serde_json::Value;

/// Relative path under `files` to the lowercase hex SHA-256 of the file's bytes.
pub type FileSnapshot = BTreeMap<String, String>;

/// Hash every regular file under `<home>/files`. A missing `files` directory is an empty
/// snapshot.
pub fn files_home_snapshot(home: &Path) -> io::Result<FileSnapshot> {
    unimplemented!("not wired: side_effects::files_home_snapshot")
}

/// The created, modified and removed captures between two snapshots whose path matches `glob`.
/// Each capture is `{"change": ..., "path": ...}`, sorted by path.
pub fn files_home_captures(before: &FileSnapshot, after: &FileSnapshot, glob: &str) -> Vec<Value> {
    unimplemented!("not wired: side_effects::files_home_captures")
}

/// Whether `path` matches `pattern`. `*` matches any run of characters within one path segment,
/// and `**` matches any run of characters across segments.
pub fn glob_matches(pattern: &str, path: &str) -> bool {
    unimplemented!("not wired: side_effects::glob_matches")
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
        assert!(glob_matches("notes/*", "notes/a.txt"));
        assert!(!glob_matches("notes/*", "other/a.txt"));
    }

    #[test]
    fn created_modified_and_removed_files_are_captured_by_glob_in_path_order() {
        let before: FileSnapshot = [
            ("a.json", "h1"),
            ("b.json", "h2"),
            ("c.txt", "h3"),
        ]
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
                json!({"change": "modified", "path": "a.json"}),
                json!({"change": "removed", "path": "b.json"}),
                json!({"change": "created", "path": "d.json"}),
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
        assert!(files_home_snapshot(home.path()).expect("snapshot").is_empty());
    }
}
