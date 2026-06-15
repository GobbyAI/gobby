use super::*;

#[test]
fn classifies_extensionless_text_as_content_only() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(root, "Makefile", b"test:\n\tcargo test\n");
    let excludes: Vec<&str> = Vec::new();

    assert_eq!(
        classify_file(root, &root.join("Makefile"), &excludes),
        Some(FileClassification::ContentOnly)
    );
    assert_eq!(content_language(&root.join("Makefile")), "text");
}

#[test]
fn classifies_markdown_content_language_as_markdown() {
    assert_eq!(content_language(Path::new("README.md")), "markdown");
    assert_eq!(
        content_language(Path::new("docs/guide.markdown")),
        "markdown"
    );
    assert_eq!(
        content_language(Path::new("skills/gcode/SKILL.md")),
        "markdown"
    );
}

#[test]
fn classifies_yaml_content_language_as_yaml() {
    assert_eq!(
        content_language(Path::new(".github/workflows/ci.yml")),
        "yaml"
    );
    assert_eq!(
        content_language(Path::new(".github/workflows/release.yaml")),
        "yaml"
    );
}

#[test]
fn classifies_mjs_as_ast_and_markdown_as_content_only() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(root, "src/module.mjs", b"export const value = 1;\n");
    write_file(root, "README.md", b"# Title\n");
    write_file(root, "docs/guide.markdown", b"# Guide\n");
    let excludes: Vec<&str> = Vec::new();

    assert_eq!(
        classify_file(root, &root.join("src/module.mjs"), &excludes),
        Some(FileClassification::Ast)
    );
    assert_eq!(
        classify_file(root, &root.join("README.md"), &excludes),
        Some(FileClassification::ContentOnly)
    );
    assert_eq!(
        classify_file(root, &root.join("docs/guide.markdown"), &excludes),
        Some(FileClassification::ContentOnly)
    );
}

#[test]
fn classifies_github_workflow_yaml_as_content_only() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(root, ".github/workflows/ci.yml", b"name: ci\n");
    write_file(root, ".github/workflows/release.yaml", b"name: release\n");
    let excludes: Vec<&str> = Vec::new();

    assert_eq!(
        classify_file(root, &root.join(".github/workflows/ci.yml"), &excludes),
        Some(FileClassification::ContentOnly)
    );
    assert_eq!(
        classify_file(
            root,
            &root.join(".github/workflows/release.yaml"),
            &excludes
        ),
        Some(FileClassification::ContentOnly)
    );
}

#[test]
fn classifies_source_build_directory_as_ast_indexable() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(
        root,
        "src/gobby/build/workspaces.py",
        b"class WorkspaceBuilder:\n    pass\n",
    );
    let excludes = vec!["build".to_string(), "dist".to_string()];

    assert_eq!(
        classify_file(root, &root.join("src/gobby/build/workspaces.py"), &excludes),
        Some(FileClassification::Ast)
    );
}

#[test]
fn skips_root_build_directory() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(root, "build/generated.py", b"class Generated:\n    pass\n");
    let excludes = vec!["build".to_string(), "dist".to_string()];

    assert_eq!(
        classify_file(root, &root.join("build/generated.py"), &excludes),
        None
    );
}

#[test]
fn walker_consumes_gobby_core_walker_settings() {
    let source = [
        include_str!("../classification.rs"),
        include_str!("../discovery.rs"),
    ]
    .join("\n");
    let settings = ["gobby_core", "::indexing::WalkerSettings"].concat();
    let direct_builder = ["WalkBuilder", "::new(root)"].concat();

    assert!(source.contains(&settings));
    assert!(!source.contains(&direct_builder));
}
