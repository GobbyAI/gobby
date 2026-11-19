use std::io::Write as _;

use super::*;
use crate::logging::writer::{RotationOwner, SizeRotatingFile, identity_of, owner_for};

fn open(path: &Path, max_bytes: u64, backups: u32, owner: RotationOwner) -> SizeRotatingFile {
    SizeRotatingFile::open(path, max_bytes, backups, owner).expect("log file opens")
}

fn write(file: &SizeRotatingFile, line: &str) {
    file.write_line(line.as_bytes()).expect("line is written");
}

#[cfg(unix)]
#[test]
fn worker_owned_rotation_reopens_on_new_inode_and_never_renames() {
    let dir = tempfile::tempdir().expect("temporary logs directory");
    let path = dir.path().join("daemon.log");
    // A threshold far below what is written: the worker owns rotation, so it never applies.
    let file = open(&path, 16, 2, RotationOwner::Worker);

    write(&file, "first line");
    assert_eq!(read(dir.path(), "daemon.log"), "first line\n");
    // The worker's handler rotates by renaming the active file.
    std::fs::rename(&path, dir.path().join("daemon.log.1")).expect("rename as the worker does");
    write(&file, "second line");
    write(&file, "third line, past the threshold");

    assert_eq!(
        read(dir.path(), "daemon.log"),
        "second line\nthird line, past the threshold\n"
    );
    assert_eq!(read(dir.path(), "daemon.log.1"), "first line\n");
    assert!(!exists(dir.path(), "daemon.log.2"));
}

#[test]
fn append_mode_keeps_lines_from_two_writers_in_order() {
    let dir = tempfile::tempdir().expect("temporary logs directory");
    let path = dir.path().join("daemon.log");
    let file = open(&path, 1 << 20, 2, RotationOwner::Gdaemon);

    write(&file, "A");
    let mut other = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .expect("second append handle");
    other.write_all(b"B\n").expect("second writer appends");
    write(&file, "C");

    assert_eq!(read(dir.path(), "daemon.log"), "A\nB\nC\n");
}

#[test]
fn gdaemon_owned_rotation_matches_rotating_handler_threshold_and_numbering() {
    let dir = tempfile::tempdir().expect("temporary logs directory");
    let path = dir.path().join("daemon.log");
    // RotatingFileHandler rolls over before a record when tell() + len(record + "\n")
    // >= maxBytes, and never rolls over an empty file.
    let file = open(&path, 25, 2, RotationOwner::Gdaemon);

    write(&file, "aaaaaaaaa"); // 10 bytes: the file was empty
    write(&file, "bbbb"); // 10 + 5 = 15 < 25
    assert_eq!(read(dir.path(), "daemon.log"), "aaaaaaaaa\nbbbb\n");
    assert!(!exists(dir.path(), "daemon.log.1"));

    write(&file, "ccccccccc"); // 15 + 10 = 25: rotates first
    assert_eq!(read(dir.path(), "daemon.log"), "ccccccccc\n");
    assert_eq!(read(dir.path(), "daemon.log.1"), "aaaaaaaaa\nbbbb\n");

    write(&file, "dddddddddddddd"); // 10 + 15 = 25: rotates; .1 moves to .2
    assert_eq!(read(dir.path(), "daemon.log"), "dddddddddddddd\n");
    assert_eq!(read(dir.path(), "daemon.log.1"), "ccccccccc\n");
    assert_eq!(read(dir.path(), "daemon.log.2"), "aaaaaaaaa\nbbbb\n");

    write(&file, "eeeeeeeeeeeeee"); // rotates again; the oldest backup is dropped
    assert_eq!(read(dir.path(), "daemon.log"), "eeeeeeeeeeeeee\n");
    assert_eq!(read(dir.path(), "daemon.log.1"), "dddddddddddddd\n");
    assert_eq!(read(dir.path(), "daemon.log.2"), "ccccccccc\n");
    assert!(!exists(dir.path(), "daemon.log.3"));

    // A file already at the threshold rotates on the first record after open.
    let seeded = dir.path().join("hooks.log");
    std::fs::write(&seeded, "x".repeat(25)).expect("seed file");
    let file = open(&seeded, 25, 2, RotationOwner::Gdaemon);
    write(&file, "y");
    assert_eq!(read(dir.path(), "hooks.log"), "y\n");
    assert_eq!(read(dir.path(), "hooks.log.1"), "x".repeat(25));
}

#[cfg(unix)]
#[test]
fn identity_uses_dev_and_ino_on_unix() {
    let dir = tempfile::tempdir().expect("temporary logs directory");
    let path = dir.path().join("daemon.log");
    let moved = dir.path().join("daemon.log.1");
    std::fs::write(&path, "first").expect("first file");
    let first = identity_of(&path).expect("first identity");

    std::fs::rename(&path, &moved).expect("rename");
    std::fs::write(&path, "second").expect("second file");

    assert_ne!(identity_of(&path).expect("second identity"), first);
    assert_eq!(identity_of(&moved).expect("moved identity"), first);
}

#[cfg(windows)]
#[test]
fn identity_uses_creation_time_and_share_delete_on_windows() {
    const FILE_SHARE_DELETE: u32 = 0x0000_0004;
    let dir = tempfile::tempdir().expect("temporary logs directory");
    let path = dir.path().join("daemon.log");
    let moved = dir.path().join("daemon.log.1");
    std::fs::write(&path, "first").expect("first file");
    let first = identity_of(&path).expect("first identity");

    std::fs::rename(&path, &moved).expect("rename");
    std::fs::write(&path, "second").expect("second file");

    assert_ne!(identity_of(&path).expect("second identity"), first);
    assert_ne!(crate::logging::writer::SHARE_MODE & FILE_SHARE_DELETE, 0);
}

#[test]
fn rotation_owner_follows_configured_backend() {
    assert_eq!(owner_for(None), RotationOwner::Gdaemon);
    assert_eq!(
        owner_for(Some("python -m gobby.runner")),
        RotationOwner::Worker
    );
    assert_eq!(
        owner_for(Some("/usr/local/bin/python3.13 -m gobby.runner")),
        RotationOwner::Worker
    );
}
