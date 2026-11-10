use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::Path;

use super::*;

fn append(path: &Path, text: &str) {
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .expect("open log for append");
    file.write_all(text.as_bytes()).expect("append");
}

#[test]
fn history_present_at_first_poll_is_skipped() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("daemon.log");
    fs::write(&path, "old line\n").expect("seed");

    let mut tail = LogTail::new(&path);
    assert!(tail.poll().expect("first poll").is_empty());

    append(&path, "new one\nnew two\n");
    assert_eq!(
        tail.poll().expect("second poll"),
        vec!["new one", "new two"]
    );
}

#[test]
fn missing_file_yields_nothing_until_it_appears() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("daemon.log");

    let mut tail = LogTail::new(&path);
    assert!(tail.poll().expect("missing poll").is_empty());

    append(&path, "first\n");
    assert_eq!(tail.poll().expect("created poll"), vec!["first"]);
}

#[test]
fn partial_line_waits_for_its_newline() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("daemon.log");
    fs::write(&path, "").expect("seed");

    let mut tail = LogTail::new(&path);
    assert!(tail.poll().expect("first poll").is_empty());

    append(&path, "half");
    assert!(tail.poll().expect("half poll").is_empty());

    append(&path, "-done\r\n");
    assert_eq!(tail.poll().expect("done poll"), vec!["half-done"]);
}

#[test]
fn rotation_drains_the_old_file_then_reads_the_new_one_from_its_start() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("daemon.log");
    fs::write(&path, "").expect("seed");

    let mut tail = LogTail::new(&path);
    assert!(tail.poll().expect("first poll").is_empty());

    append(&path, "a\n");
    assert_eq!(tail.poll().expect("a poll"), vec!["a"]);

    // Written to the old file after the last poll, then rotated away.
    append(&path, "b\n");
    fs::rename(&path, dir.path().join("daemon.log.1")).expect("rotate");
    fs::write(&path, "c\n").expect("new file");

    assert_eq!(tail.poll().expect("rotated poll"), vec!["b", "c"]);
}

#[test]
fn truncation_reads_the_file_from_its_start() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("daemon.log");
    fs::write(&path, "").expect("seed");

    let mut tail = LogTail::new(&path);
    assert!(tail.poll().expect("first poll").is_empty());

    append(&path, "0123456789\n");
    assert_eq!(tail.poll().expect("long poll"), vec!["0123456789"]);

    fs::write(&path, "z\n").expect("truncate and rewrite");
    assert_eq!(tail.poll().expect("truncated poll"), vec!["z"]);
}
