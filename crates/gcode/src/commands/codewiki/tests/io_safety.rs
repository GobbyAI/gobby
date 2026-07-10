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
fn generated_page_walker_never_visits_meta_dump_artifacts() {
    // Lane B failure dumps default to `_meta/lane_b/` (#17533). The orphan-GC
    // walker is scoped to `code/`, so dumps must never count as generated
    // pages — they can neither be reclaimed as orphans nor surface on the
    // page-count/lint paths that consume this listing.
    let project = tempfile::tempdir().expect("project tempdir");
    let out_dir = project.path().join("codewiki");
    std::fs::create_dir_all(out_dir.join("code/files")).expect("code tree");
    std::fs::create_dir_all(out_dir.join("_meta/lane_b")).expect("dump tree");
    std::fs::write(out_dir.join("code/files/a.md"), "# A\n").expect("page");
    std::fs::write(
        out_dir.join("_meta/lane_b/core_logic_engine.dump.md"),
        "# Lane B curated hard-fail dump\n",
    )
    .expect("dump");

    let pages = super::io::collect_generated_doc_pages(&out_dir).expect("walk generated pages");

    assert_eq!(pages, vec!["code/files/a.md".to_string()]);
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
