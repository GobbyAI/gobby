use super::*;

#[tokio::test]
async fn new_project_creates_named_directory_and_git_checkout() {
    let parent = tempfile::tempdir().expect("parent");
    let path = parent.path().join("named-project");
    let root = new_checkout(&path, OsStr::new("git"))
        .await
        .expect("create");
    assert_eq!(root, path.canonicalize().expect("canonical checkout"));
    assert!(root.join(".git").is_dir());
    assert_eq!(checkout_root(&root).await.expect("git checkout"), root);
}

#[tokio::test]
async fn new_project_accepts_existing_empty_directory() {
    let directory = tempfile::tempdir().expect("empty directory");
    let root = new_checkout(directory.path(), OsStr::new("git"))
        .await
        .expect("init");
    assert!(root.join(".git").is_dir());
}

#[tokio::test]
async fn nonempty_directory_and_file_are_preserved() {
    let directory = tempfile::tempdir().expect("existing directory");
    let file = directory.path().join("keep.txt");
    fs::write(&file, b"user content")
        .await
        .expect("existing file");
    let error = new_checkout(directory.path(), OsStr::new("git"))
        .await
        .expect_err("nonempty");
    assert!(error.contains("not empty"), "{error}");
    assert!(!directory.path().join(".git").exists());
    assert!(new_checkout(&file, OsStr::new("git")).await.is_err());
    assert_eq!(
        fs::read(&file).await.expect("preserved file"),
        b"user content"
    );
}

#[tokio::test]
async fn git_init_failure_keeps_existing_and_new_directories() {
    let parent = tempfile::tempdir().expect("parent");
    let existing = parent.path().join("existing");
    fs::create_dir(&existing).await.expect("existing directory");
    let missing_git = parent.path().join("missing-git");
    for path in [existing, parent.path().join("new")] {
        let error = new_checkout(&path, missing_git.as_os_str())
            .await
            .expect_err("git missing");
        assert!(error.contains("Git initialization failed"), "{error}");
        assert!(path.is_dir(), "directory kept after failure");
    }
}

#[cfg(unix)]
#[tokio::test]
async fn git_init_nonzero_exit_reports_stderr_and_keeps_directory() {
    use std::os::unix::fs::PermissionsExt;
    let parent = tempfile::tempdir().expect("parent");
    let failing_git = parent.path().join("failing-git");
    fs::write(
        &failing_git,
        "#!/bin/sh\nprintf 'init refused' >&2\nexit 1\n",
    )
    .await
    .expect("fake git");
    fs::set_permissions(&failing_git, std::fs::Permissions::from_mode(0o700))
        .await
        .expect("executable");
    let root = parent.path().join("new");
    let error = new_checkout(&root, failing_git.as_os_str())
        .await
        .expect_err("git init failure");
    assert!(error.contains("init refused"), "{error}");
    assert!(root.is_dir());
    assert!(!root.join(".git").exists());
}

#[tokio::test]
async fn open_project_rejects_non_git_and_missing_paths() {
    let directory = tempfile::tempdir().expect("non-git directory");
    let error = checkout_root(directory.path()).await.expect_err("non-git");
    assert!(error.contains("Cannot verify git checkout"), "{error}");
    assert!(checkout_root(&directory.path().join("missing"))
        .await
        .is_err());
}

#[tokio::test]
async fn open_project_resolves_subdirectory_to_checkout_root() {
    let directory = tempfile::tempdir().expect("checkout");
    let root = new_checkout(directory.path(), OsStr::new("git"))
        .await
        .expect("git init");
    let nested = root.join("nested");
    fs::create_dir(&nested).await.expect("nested");
    assert_eq!(checkout_root(&nested).await.expect("checkout root"), root);
}
