use super::*;

#[test]
#[cfg(unix)]
fn write_doc_rejects_symlinked_parent() {
    use std::os::unix::fs::symlink;

    let project = tempfile::tempdir().expect("project tempdir");
    let out_dir = project.path().join("codewiki");
    let outside = tempfile::tempdir().expect("outside tempdir");
    std::fs::create_dir_all(&out_dir).expect("out dir");
    symlink(outside.path(), out_dir.join("linked")).expect("symlink parent");

    let err = write_doc(&out_dir, "linked/escape.md", "escaped")
        .expect_err("symlink parent should be rejected");

    assert!(err.to_string().contains("symlinked codewiki path"));
    assert!(!outside.path().join("escape.md").exists());
}

#[test]
#[cfg(unix)]
fn write_doc_rejects_symlinked_target() {
    use std::os::unix::fs::symlink;

    let project = tempfile::tempdir().expect("project tempdir");
    let out_dir = project.path().join("codewiki");
    let outside = tempfile::tempdir().expect("outside tempdir");
    std::fs::create_dir_all(&out_dir).expect("out dir");
    let outside_target = outside.path().join("target.md");
    symlink(&outside_target, out_dir.join("target.md")).expect("symlink target");

    let err = write_doc(&out_dir, "target.md", "escaped").expect_err("symlink target rejected");

    assert!(err.to_string().contains("symlinked codewiki path"));
    assert!(!outside_target.exists());
}
