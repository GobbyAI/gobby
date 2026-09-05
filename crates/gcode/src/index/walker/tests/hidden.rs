use super::*;

#[test]
fn discovers_default_hidden_metadata_allowlist() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(root, "src/lib.rs", b"fn main() {}\n");
    write_file(root, ".gobby/plans/foo.md", b"# Plan\n");
    write_file(root, ".gobby/plans/nested/bar.md", b"# Nested\n");
    write_file(root, ".github/workflows/ci.yml", b"name: ci\n");
    write_file(root, ".github/workflows/release.yaml", b"name: release\n");

    let (ast, content_only) = discover_files(root, &[] as &[&str]);

    assert_eq!(rels(root, ast), vec!["src/lib.rs"]);
    assert_eq!(
        rels(root, content_only),
        vec![
            ".github/workflows/ci.yml",
            ".github/workflows/release.yaml",
            ".gobby/plans/foo.md",
            ".gobby/plans/nested/bar.md",
        ]
    );
}

#[test]
fn skips_non_allowlisted_hidden_metadata_by_default() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(root, ".github/ISSUE_TEMPLATE/bug.md", b"# Bug\n");
    write_file(root, ".gobby/gcode.json", br#"{"id":"project"}"#);
    write_file(root, ".gobby/project.json", br#"{"id":"project"}"#);
    write_file(root, "wiki/page.md", b"# Wiki\n");
    write_file(root, ".gobby/screenshots/shot.md", b"# Screenshot\n");
    write_file(root, ".gobby/tasks.jsonl", b"{}\n");
    write_file(root, ".gobby/memories.jsonl", b"{}\n");

    let (ast, content_only) = discover_files(root, &[] as &[&str]);

    assert!(rels(root, ast).is_empty());
    assert_eq!(rels(root, content_only), vec!["wiki/page.md"]);
}

#[test]
fn discovers_project_hidden_allowlist_from_gcode_json() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(
        root,
        ".gobby/gcode.json",
        br#"{"index":{"hidden_allowlist":[".custom/agent-docs/**/*.md"]}}"#,
    );
    write_file(root, ".custom/agent-docs/guide.md", b"# Guide\n");
    write_file(root, ".custom/agent-docs/nested/runbook.md", b"# Runbook\n");
    write_file(root, ".custom/other.md", b"# Other\n");

    let (ast, content_only) = discover_files(root, &[] as &[&str]);

    assert!(rels(root, ast).is_empty());
    assert_eq!(
        rels(root, content_only),
        vec![
            ".custom/agent-docs/guide.md",
            ".custom/agent-docs/nested/runbook.md",
        ]
    );
}

#[test]
fn excludes_win_over_allowlisted_hidden_paths() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(root, ".gobby/plans/foo.md", b"# Plan\n");
    write_file(root, ".github/workflows/ci.yml", b"name: ci\n");

    let excludes = vec![".gobby".to_string(), "workflows".to_string()];
    let (ast, content_only) = discover_files(root, &excludes);

    assert!(rels(root, ast).is_empty());
    assert!(rels(root, content_only).is_empty());
}

#[test]
#[cfg(not(windows))]
fn discovers_unix_backslash_filename_under_literal_key() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(root, r".gobby/plans/name\plan.md", b"# Plan\n");

    let (ast, content_only) = discover_files(root, &[] as &[&str]);

    assert!(rels(root, ast).is_empty());
    assert_eq!(rels(root, content_only), vec![r".gobby/plans/name\plan.md"]);
}

#[test]
fn explicit_files_respect_ignored_ancestors() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    let status = std::process::Command::new("git")
        .args(["init", "--quiet"])
        .arg(root)
        .status()
        .expect("initialize test repository");
    assert!(status.success());
    write_file(root, ".gitignore", b"/wiki/\n");
    write_file(root, "wiki/_gwiki/scope.json", b"{}\n");
    write_file(root, "wiki/page.md", b"# Retired vault\n");
    write_file(root, "src/lib.rs", b"pub fn surviving_code() {}\n");

    let (ast, content_only) = discover_files(root, &[] as &[&str]);
    assert_eq!(rels(root, ast), vec!["src/lib.rs"]);
    assert!(
        content_only
            .iter()
            .all(|path| !path.starts_with(root.join("wiki")))
    );
    assert_eq!(
        classify_explicit_file_with_options(
            root,
            &root.join("wiki/page.md"),
            &[] as &[&str],
            DiscoveryOptions::default(),
        ),
        None,
    );
    assert_eq!(
        classify_explicit_file_with_options(
            root,
            &root.join("wiki/page.md"),
            &[] as &[&str],
            DiscoveryOptions {
                respect_gitignore: false
            },
        ),
        Some(FileClassification::ContentOnly),
    );
    write_file(
        root,
        ".gobby/gcode.json",
        br#"{"index":{"hidden_allowlist":["wiki/**/*.md"]}}"#,
    );
    assert_eq!(
        classify_explicit_file_with_options(
            root,
            &root.join("wiki/page.md"),
            &[] as &[&str],
            DiscoveryOptions::default(),
        ),
        Some(FileClassification::ContentOnly),
    );
}

#[test]
fn ordinary_wiki_named_directory_uses_normal_classification() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(root, "wiki/_meta/settings.json", b"{\"enabled\":true}\n");
    assert_eq!(
        classify_file(root, &root.join("wiki/_meta/settings.json"), &[] as &[&str]),
        Some(FileClassification::Ast),
    );
}
