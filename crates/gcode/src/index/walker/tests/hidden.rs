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
    write_file(root, "gobby-wiki/page.md", b"# Wiki\n");
    write_file(root, ".gobby/screenshots/shot.md", b"# Screenshot\n");
    write_file(root, ".gobby/tasks.jsonl", b"{}\n");
    write_file(root, ".gobby/memories.jsonl", b"{}\n");

    let (ast, content_only) = discover_files(root, &[] as &[&str]);

    assert!(rels(root, ast).is_empty());
    assert_eq!(rels(root, content_only), vec!["gobby-wiki/page.md"]);
}

#[test]
fn discovers_wiki_markdown_and_skips_generated_wiki_metadata() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(root, "gobby-wiki/page.md", b"# Wiki\n");
    write_file(root, "gobby-wiki/nested/page.md", b"# Nested\n");
    write_file(root, "gobby-wiki/_meta/codewiki.json", b"{}\n");
    write_file(root, "gobby-wiki/_meta/readme.md", b"# Generated\n");
    write_file(root, "gobby-wiki/_gwiki/scope.json", b"{}\n");
    write_file(root, "gobby-wiki/wikis.json", b"{}\n");
    write_file(root, "gobby-wiki/.obsidian/app.json", b"{}\n");
    write_file(root, "gobby-wiki/.obsidian/workspace.json", b"{}\n");
    write_file(root, "gobby-wiki/wikis.json.lock", b"lock\n");
    write_file(root, "gobby-wiki/nested/page.lock", b"lock\n");

    let (ast, content_only) = discover_files(root, &[] as &[&str]);

    assert!(rels(root, ast).is_empty());
    assert_eq!(
        rels(root, content_only),
        vec!["gobby-wiki/nested/page.md", "gobby-wiki/page.md"]
    );
    assert_eq!(
        classify_file(
            root,
            &root.join("gobby-wiki/_meta/codewiki.json"),
            &[] as &[&str]
        ),
        None
    );
    assert_eq!(
        classify_file(
            root,
            &root.join("gobby-wiki/wikis.json.lock"),
            &[] as &[&str]
        ),
        None
    );
    assert_eq!(
        classify_file(
            root,
            &root.join("gobby-wiki/.obsidian/app.json"),
            &[] as &[&str]
        ),
        None
    );
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
