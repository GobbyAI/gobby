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

#[test]
fn write_doc_normalizes_markdown_only() {
    let project = tempfile::tempdir().expect("project tempdir");
    let out_dir = project.path().join("codewiki");

    write_doc(&out_dir, "code/page.md", "# Page\n\n\nBody\n").expect("write markdown");
    write_doc(
        &out_dir,
        "_meta/codewiki.json",
        "{\n\n\n  \"ok\": true\n}\n",
    )
    .expect("write json");

    let markdown = std::fs::read_to_string(out_dir.join("code/page.md")).expect("read markdown");
    let json = std::fs::read_to_string(out_dir.join("_meta/codewiki.json")).expect("read json");

    assert_eq!(markdown, "# Page\n\nBody\n");
    assert_eq!(json, "{\n\n\n  \"ok\": true\n}\n");
}
