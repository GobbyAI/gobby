use std::env;
use std::error::Error as _;
use std::fs;
use std::io;
use std::path::Path;

use tempfile::TempDir;

use super::{SecretError, kek_file_or_create, publish_private_file, publish_with};

fn temp_files(directory: &Path) -> usize {
    fs::read_dir(directory)
        .expect("read the test directory")
        .filter_map(Result::ok)
        .filter(|entry| entry.file_name().to_string_lossy().ends_with(".tmp"))
        .count()
}

// nextest runs each test in its own process, so changing the working directory is contained.
#[test]
fn publish_accepts_a_bare_file_name() {
    let home = TempDir::new().expect("temp dir");
    let previous = env::current_dir().expect("current dir");
    env::set_current_dir(home.path()).expect("enter the temp dir");
    let outcome = publish_private_file(Path::new("secret_kek"), b"key");
    env::set_current_dir(&previous).expect("restore the current dir");

    assert!(outcome.expect("a bare relative name must publish"));
    assert_eq!(
        fs::read(home.path().join("secret_kek")).expect("published file"),
        b"key"
    );
}

#[test]
fn directory_sync_failure_after_publish_still_publishes() {
    let home = TempDir::new().expect("temp dir");
    let path = home.path().join(".secret_kek");

    let outcome = publish_with(&path, b"key", |_| Err(io::Error::other("sync refused")));

    assert!(
        outcome.expect("a file linked before the directory sync stays published"),
        "the publish must report success"
    );
    assert_eq!(fs::read(&path).expect("published file"), b"key");
    assert_eq!(temp_files(home.path()), 0, "the temp file must be removed");
}

#[test]
fn kek_io_failure_keeps_the_io_source() {
    let home = TempDir::new().expect("temp dir");
    let key_path = home.path().join(".secret_kek");
    fs::create_dir(&key_path).expect("a directory where the key file belongs");
    let expected = fs::read(&key_path)
        .expect_err("a directory cannot be read as a key")
        .kind();

    let error = kek_file_or_create(home.path()).expect_err("an unreadable key file must fail");

    assert!(matches!(error, SecretError::KeyFile(_)));
    let source = error
        .source()
        .and_then(|source| source.downcast_ref::<io::Error>())
        .expect("the io error must be the error source");
    assert_eq!(source.kind(), expected);
}
