use super::super::file::{ExplicitFileRoute, explicit_file_route};
use super::super::pipeline::explicit_route_with_discovery_options;
use super::super::util::DEFAULT_EXCLUDES;
use super::fixtures::write_file;
use crate::index::walker;
use std::path::Path;

#[test]
fn explicit_file_route_sends_unsupported_text_to_content_only() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(root, "src/lib.rs", b"fn main() {}\n");
    write_file(root, "notes.txt", b"plain notes\n");
    write_file(root, "Dockerfile", b"FROM rust:latest\n");
    write_file(root, "api_key.txt", b"secret-ish\n");
    write_file(root, "target/generated.txt", b"generated\n");
    write_file(root, "image.bin", b"PNG\0binary");

    let excludes = DEFAULT_EXCLUDES;

    assert_eq!(
        explicit_file_route(root, &root.join("src/lib.rs"), excludes),
        ExplicitFileRoute::Ast
    );
    assert_eq!(
        explicit_file_route(root, &root.join("notes.txt"), excludes),
        ExplicitFileRoute::ContentOnly
    );
    assert_eq!(
        explicit_file_route(root, &root.join("Dockerfile"), excludes),
        ExplicitFileRoute::ContentOnly
    );
    assert_eq!(
        explicit_file_route(root, &root.join("api_key.txt"), excludes),
        ExplicitFileRoute::Skip
    );
    assert_eq!(
        explicit_file_route(root, &root.join("target/generated.txt"), excludes),
        ExplicitFileRoute::Skip
    );
    assert_eq!(
        explicit_file_route(root, &root.join("image.bin"), excludes),
        ExplicitFileRoute::Skip
    );
}

#[test]
fn explicit_file_routes_follow_respect_gitignore_setting() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    std::fs::create_dir(root.join(".git")).expect("git dir");
    write_file(root, ".gitignore", b"ignored.rs\n");
    write_file(root, "ignored.rs", b"fn ignored() {}\n");
    write_file(root, "src/lib.rs", b"fn visible() {}\n");

    let route = explicit_route_with_discovery_options(
        root,
        &root.join("ignored.rs"),
        DEFAULT_EXCLUDES,
        walker::DiscoveryOptions {
            respect_gitignore: true,
        },
    );
    assert_eq!(route, ExplicitFileRoute::Skip);
    let route = explicit_route_with_discovery_options(
        root,
        &root.join("src/lib.rs"),
        DEFAULT_EXCLUDES,
        walker::DiscoveryOptions {
            respect_gitignore: true,
        },
    );
    assert_eq!(route, ExplicitFileRoute::Ast);

    let route = explicit_route_with_discovery_options(
        root,
        &root.join("ignored.rs"),
        DEFAULT_EXCLUDES,
        walker::DiscoveryOptions {
            respect_gitignore: false,
        },
    );
    assert_eq!(route, ExplicitFileRoute::Ast);
}

#[test]
fn explicit_file_route_applies_parent_gitignore_without_full_discovery() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    std::fs::create_dir(root.join(".git")).expect("git dir");
    write_file(root, "src/.gitignore", b"ignored.rs\n");
    write_file(root, "src/ignored.rs", b"fn ignored() {}\n");
    write_file(root, "src/visible.rs", b"fn visible() {}\n");

    assert_eq!(
        explicit_route_with_discovery_options(
            root,
            &root.join("src/ignored.rs"),
            DEFAULT_EXCLUDES,
            walker::DiscoveryOptions {
                respect_gitignore: true,
            },
        ),
        ExplicitFileRoute::Skip
    );
    assert_eq!(
        explicit_route_with_discovery_options(
            root,
            &root.join("src/visible.rs"),
            DEFAULT_EXCLUDES,
            walker::DiscoveryOptions {
                respect_gitignore: true,
            },
        ),
        ExplicitFileRoute::Ast
    );
}

#[test]
fn explicit_file_route_indexes_mjs_and_routes_markdown_to_content_only() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(root, "src/module.mjs", b"export const value = 1;\n");
    write_file(root, "README.md", b"# Title\n");
    write_file(root, "docs/guide.markdown", b"# Guide\n");
    write_file(root, ".github/workflows/ci.yml", b"name: ci\n");

    let excludes = DEFAULT_EXCLUDES;

    assert_eq!(
        explicit_file_route(root, &root.join("src/module.mjs"), excludes),
        ExplicitFileRoute::Ast
    );
    assert_eq!(
        explicit_file_route(root, &root.join("README.md"), excludes),
        ExplicitFileRoute::ContentOnly
    );
    assert_eq!(
        explicit_file_route(root, &root.join("docs/guide.markdown"), excludes),
        ExplicitFileRoute::ContentOnly
    );
    assert_eq!(
        explicit_file_route(root, &root.join(".github/workflows/ci.yml"), excludes),
        ExplicitFileRoute::ContentOnly
    );
}

#[test]
fn discovered_hidden_workflows_survive_index_path_filter() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(root, ".github/workflows/ci.yml", b"name: ci\n");
    write_file(root, ".gobby/plans/plan.md", b"# Plan\n");

    let excludes = DEFAULT_EXCLUDES;
    let (_, content_only) = walker::discover_files(root, excludes);
    let filtered = super::super::util::filter_discovered_paths(
        root,
        Path::new(".github/workflows"),
        content_only,
    );

    let mut rels: Vec<String> = filtered
        .into_iter()
        .map(|path| {
            path.strip_prefix(root)
                .expect("path under root")
                .to_string_lossy()
                .to_string()
        })
        .collect();
    rels.sort();
    assert_eq!(rels, vec![".github/workflows/ci.yml"]);
}

#[test]
fn explicit_file_route_skips_generated_mjs() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    write_file(
        root,
        "src/setup.mjs",
        b"// Generated by setup. Do not edit.\nexport const value = 1;\n",
    );

    let excludes = DEFAULT_EXCLUDES;

    assert_eq!(
        explicit_file_route(root, &root.join("src/setup.mjs"), excludes),
        ExplicitFileRoute::Skip
    );
}
